"""The AI series decision: one question, one name (R83).

ComicInfo's `<Series>` field is what a library groups a book's volumes under.
Metadata sometimes carries it; when it does not, this asks the archive-path
model chain for a name -- reusing the same 「主力 + 备用」 list the path decision
uses, because the two are one operator preference about which model reads
metadata, not two.

The answer is written back to the work's `Series` metadata by the caller, so a
later repack reads the stored value instead of asking the model a second time.
That is deliberate: the model is not deterministic, and a field that changed
between two packs of the same book would be a fact that moved.

Unlike the path decision there is no cache table and no fingerprint: the field
is a short, cheap answer and the metadata write *is* the cache. An empty
`series` is a real answer -- 「this work has no series」 -- and is not an error.
"""

from __future__ import annotations

from app.ai.errors import AI_SERIES_INVALID, AiError
from app.ai.models import CHAIN_SCOPE_ARCHIVE_PATH
from app.ai.prompt import build_messages, metadata_payload
from app.ai.service import extract_json_object
from app.conversion.naming import safe_library_name

_FIELD_SERIES = "series"

DEFAULT_SERIES_PROMPT = r"""你是 EhBot 的书库整理助手。你会收到一部作品的元数据 JSON，请判断它属于哪个系列。

只输出一个 JSON 对象，不要输出解释、Markdown 代码块或任何其它文字：
{"series": "<系列名>"}

做法：去掉标题里下面这些部分，剩下的主干就是系列名
- 作者/社团的方括号标注，如 [ぽりうれたん]
- 翻译语言标注，如 [中文翻譯]、[中国翻訳]、[English]
- 活动名，如 (C108)
- 卷/话/文件号与标记，如 2、第7話、-File.7-、上/下、前編/後編
例：[ぽりうれたん] 隣の喘ぎ声がうるさい2[中文翻譯] -> 系列名 "隣の喘ぎ声がうるさい"
杂志例外：系列名 = 年月 + 特刊信息，如 "2026年6月号"。

系列名里不要出现刊发形式、作者、翻译语言，也不要出现作品名本身。
原文照抄：用元数据原文，不要翻译、不要音译、不要缩写、不要自己创作。
判断不了（例如单篇作品、没有可辨认的系列）就输出空字符串：
{"series": ""}
"""


def answer_series(text: str) -> str | None:
    """Parse one assistant message into a series name, or None for 「没有系列」.

    JSON first, then a `series` string. A missing key and an empty string both
    mean 「no series」 -- the caller then leaves ComicInfo on its fallback. A
    non-string value or a body with no JSON object at all is `AI_SERIES_INVALID`,
    which walks the model chain like any other failure.
    """
    payload = extract_json_object(text)
    value = payload.get(_FIELD_SERIES, "")
    if value is None:
        return None
    if not isinstance(value, str):
        raise AiError(AI_SERIES_INVALID, "AI 返回的系列名不是字符串")
    name = value.strip()
    if not name:
        return None
    cleaned = safe_library_name(name, fallback="")
    return cleaned or None


class AiSeriesService:
    """Ask the archive-path model chain for one book's series name."""

    def __init__(self, ai_service) -> None:
        self._ai = ai_service

    async def chain(self):
        """The effective chain, for a caller that only wants 「配没配」."""
        return await self._ai.effective_chain(CHAIN_SCOPE_ARCHIVE_PATH)

    async def resolve(self, metadata) -> str | None:
        """The series name, or None when the model says there is none.

        Raises `AiError` when the whole chain fails or answers unusably; the
        packing job turns that into a 需干预 card rather than a missing field.
        """
        payload = metadata_payload(metadata)
        answer = await self._ai.complete(
            build_messages(DEFAULT_SERIES_PROMPT, payload),
            validate=answer_series,
        )
        return answer_series(answer.text)


__all__ = ["AiSeriesService", "DEFAULT_SERIES_PROMPT", "answer_series"]
