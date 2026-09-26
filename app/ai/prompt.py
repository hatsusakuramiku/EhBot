"""What the model is asked, and the metadata it is asked about.

Two halves, both pure functions of a book's stored metadata:

* the payload -- one JSON document built from the fields a decision actually
  needs. 作者/社团/原作 are arrays rather than pre-joined strings because
  「一共几个人合作」 is itself the judge of 合作本 / 同人志, and a joined
  string would have thrown that away. Tags are capped (`MAX_TAGS_IN_PROMPT`)
  because the whole list is expensive and the tail never changes a decision.
* the prompt -- the operator's own text, with the payload delivered either
  inline (a `{{metadata}}` placeholder) or as a following user message. The
  default text below is the proposal's §6.1 reference prompt.

The prompt is *not* a security boundary. It is an editable setting, so the
guarantee that the model's answer is a usable path comes from the parsing and
sanitising in `app.ai.paths`, never from a sentence asking the model to behave.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping, Sequence

from app.ai.models import MAX_TAGS_IN_PROMPT

DEFAULT_AI_PROMPT = r"""你是 EhBot 的书库整理助手。你会收到一部作品的元数据 JSON，请为它决定归档到书库中的相对路径。

只输出一个 JSON 对象，不要输出解释、Markdown 代码块或任何其它文字：
{"directory": "<相对书库根目录的目录，可多级，用 / 分隔>", "filename": "<不含扩展名的文件名>"}

归档层级固定为：刊发形式 / 社团 / 作者 / 系列名 / 作品名
其中社团、作者两层可以省略（缺失就整层不要，不要留空、不要写「未知」），
系列名与作品名两层一定存在。作品名是文件名，其余是目录。

判断线索一般就写在标题里：先读标题，artists 的人数、groups、category_raw、tags 只用于确认。
按下面顺序判断，命中即停。

一、刊发形式（第一层，必有一层）
  1. 活动发刊：活动名/届数一般写在标题里（如 "(C108)"、"[C108]"、コミケ108、例大祭、
     コミティア 等），照抄该活动名，例如 "C108"
  2. 杂志：标题里有刊名（快楽天、快楽天ビースト、COMIC 快楽天、メガストア 等）就用刊名
  3. 商业志：元数据表明是商业出版（category_raw 为 Manga 等非同人分类，或标题/标签注明）用 "商业志"
  4. 单行本：标题或标签表明是单行本（"単行本"、"tankoubon" 等）用 "单行本"
  5. 多作者的非单行本：用 "合作本"
  6. 单作者的非单行本：用 "同人志"
  7. 实在判断不了：单作者用 "同人志"，多作者用 "合作本"

二、社团（第二层，可省略）
  groups 恰好 1 个才加这一层；0 个或 2 个以上都省略（与作者同一规则）。多值不要写进目录，
  它们仍保留在 tags 与文件名里。

三、作者（第三层，可省略）
  artists 恰好 1 位才加这一层；0 位或 2 位以上都省略（合作本、合刊不列作者，多值保留在
  tags 与文件名里）。

四、系列名（第四层，必有）
  做法：去掉标题里下面这些部分，剩下的主干就是系列名
  - 作者/社团的方括号标注，如 [ぽりうれたん]
  - 翻译语言标注，如 [中文翻譯]、[中国翻訳]、[English]
  - 活动名，如 (C108)
  - 卷/话/文件号与标记，如 2、第7話、-File.7-、上/下、前編/後編
  例：[ぽりうれたん] 隣の喘ぎ声がうるさい2[中文翻譯] -> 系列名 "隣の喘ぎ声がうるさい"
  杂志例外：系列名 = 年月 + 特刊信息（刊名已经占了第一层，不要重复），
  如 "2026年6月号"、"2026年6月号 増刊"。
  系列名里不要出现刊发形式、作者、翻译语言。

五、作品名（文件名）
  保留完整原标题（文件名要能独立说明这是什么作品：分享或单独取用时不依赖目录信息）；
  把不能用于路径的字符换成空格：< > : " / \ | ? * 以及控制字符，
  去掉首尾的点与空格，不要用保留名（CON、PRN、AUX、NUL、COM1–COM9、LPT1–LPT9）。

通用规则：
1. 不要输出扩展名：.cbz 由程序追加。
2. 原文照抄：刊发形式、社团、作者、系列名、年月、活动名、作品名都用元数据原文，不要翻译、
   不要音译、不要缩写、不要自己创作。
3. 缺失的字段不要猜、不要编造；不确定就省略那一层，宁可路径短，也不要写不确定的信息。
4. 每一段尽量不超过 60 个字符，整条路径尽量不超过 180 个字符。
5. directory 不能以 / 开头，不能出现 . 或 .. 这两段；不要输出绝对路径、盘符、URL 或文件系统路径。

元数据字段：japanese_title（日文原名）、english_title、artists（作者数组）、groups（社团数组）、
parody（原作，仅作系列名的参考）、category、category_raw（上游分类原文，如 Doujinshi / Manga）、
language、tags（数组）、page_count。

示例：
输入：{"japanese_title": "[ぽりうれたん] 隣の喘ぎ声がうるさい2[中文翻譯]", "artists": ["ぽりうれたん"],
       "groups": [], "category": "同人志", "category_raw": "Doujinshi"}
输出：{"directory": "同人志/ぽりうれたん/隣の喘ぎ声がうるさい", "filename": "[ぽりうれたん] 隣の喘ぎ声がうるさい2[中文翻譯]"}

输入：{"japanese_title": "催眠性指導 -File.7-", "artists": ["左藤空気"], "groups": ["G-Power!"],
       "category": "同人志", "category_raw": "Doujinshi"}
输出：{"directory": "同人志/G-Power!/左藤空気/催眠性指導", "filename": "催眠性指導 -File.7-"}

输入：{"japanese_title": "快楽天 2026年6月号 増刊", "artists": [], "groups": [],
       "category": "漫画", "category_raw": "Manga"}
输出：{"directory": "快楽天/2026年6月号 増刊", "filename": "快楽天 2026年6月号 増刊"}

输入：{"japanese_title": "(C108) [サークルA] 夏の合同本", "artists": ["作者A", "作者B"],
       "groups": ["サークルA"], "category": "同人志", "category_raw": "Doujinshi"}
输出：{"directory": "C108/サークルA/夏の合同本", "filename": "(C108) [サークルA] 夏の合同本"}

输入：{"japanese_title": "○○の単行本", "artists": ["作者名"], "groups": [],
       "category": "漫画", "category_raw": "Manga"}
输出：{"directory": "商业志/作者名/○○の単行本", "filename": "○○の単行本"}"""


#: Where the payload goes when the operator writes it into the prompt. Absent,
#: the payload is appended as the user message instead -- the arrangement the
#: default prompt uses, because it keeps the instructions and the data visually
#: separate and a model that is asked to answer "the JSON above" has one thing
#: to read either way.
METADATA_PLACEHOLDER = "{{metadata}}"

_FIELD_JAPANESE_TITLE = "JapaneseTitle"
_FIELD_ENGLISH_TITLE = "Title"
_FIELD_CATEGORY = "Category"
_FIELD_CATEGORY_RAW = "CategoryRaw"
_FIELD_LANGUAGE = "Language"
_FIELD_TAGS = "Tags"
_FIELD_TAGS_RAW = "TagsRaw"
_FIELD_PAGES = "Pages"

#: Multi-valued identity fields, each with a `*Raw` sibling holding the upstream
#: original. The raw value is preferred when it exists: the prompt asks for 原文
#: 照抄, and `Artist` may already be a translation.
_IDENTITY_FIELDS: tuple[str, ...] = ("Artist", "Group", "Parody")

_DIGITS = re.compile(r"\d+")


def fields_from_metadata(metadata: Sequence[Any]) -> dict[str, str | None]:
    """The first value for each field name, the way the packer reads metadata.

    First match wins for the same reason `_metadata_lookup` does: the row list
    arrives in precedence order from the enricher, and picking any other row
    would make the model see a different title from the one the book is filed
    under.
    """
    fields: dict[str, str | None] = {}
    for entry in metadata:
        name = getattr(entry, "field_name", None)
        if name is None and isinstance(entry, Mapping):
            name = entry.get("field_name")
        if not name:
            continue
        value = getattr(entry, "field_value", None)
        if value is None and isinstance(entry, Mapping):
            value = entry.get("field_value")
        fields.setdefault(str(name), None if value is None else str(value))
    return fields


def split_values(value: str | None) -> list[str]:
    """A comma-joined metadata value back into its items, order kept.

    The stored form is what `", ".join(...)` produced, so a comma is the
    separator; newlines appear in the raw tag blobs from a few sources and are
    split too. Duplicates are dropped because a translated and a raw list
    concatenated by a stale row would otherwise send the same name twice.
    """
    if not value:
        return []
    items: list[str] = []
    for chunk in value.replace("\n", ",").split(","):
        item = chunk.strip()
        if item and item not in items:
            items.append(item)
    return items


def _identity(fields: Mapping[str, str | None], name: str) -> list[str]:
    return split_values(fields.get(f"{name}Raw") or fields.get(name))


def _page_count(fields: Mapping[str, str | None]) -> int | None:
    raw = fields.get(_FIELD_PAGES)
    if not raw:
        return None
    found = _DIGITS.search(raw)
    return int(found.group()) if found else None


def build_metadata_payload(
    fields: Mapping[str, str | None],
) -> dict[str, Any]:
    """The JSON document one request carries.

    Deliberately no path, no candidate id, no provider and no account detail:
    the model is given the book, not the deployment.
    """
    return {
        "japanese_title": fields.get(_FIELD_JAPANESE_TITLE),
        "english_title": fields.get(_FIELD_ENGLISH_TITLE),
        "artists": _identity(fields, "Artist"),
        "groups": _identity(fields, "Group"),
        "parody": _identity(fields, "Parody"),
        "category": fields.get(_FIELD_CATEGORY),
        "category_raw": fields.get(_FIELD_CATEGORY_RAW),
        "language": fields.get(_FIELD_LANGUAGE),
        "tags": split_values(fields.get(_FIELD_TAGS_RAW) or fields.get(_FIELD_TAGS))[
            :MAX_TAGS_IN_PROMPT
        ],
        "page_count": _page_count(fields),
    }


def metadata_payload(metadata: Sequence[Any]) -> dict[str, Any]:
    """The payload for a book, straight from its stored metadata rows."""
    return build_metadata_payload(fields_from_metadata(metadata))


def build_messages(
    prompt: str, payload: Mapping[str, Any]
) -> list[dict[str, str]]:
    """The chat messages for one book.

    A `{{metadata}}` placeholder puts the JSON where the operator wrote it; no
    placeholder appends it as the user message. Either way the model receives
    exactly one copy of the payload, and the system message is the operator's
    own words rather than a string this module assembled.
    """
    blob = json.dumps(payload, ensure_ascii=False)
    text = prompt or ""
    if METADATA_PLACEHOLDER in text:
        return [
            {"role": "system", "content": text.replace(METADATA_PLACEHOLDER, blob)}
        ]
    return [
        {"role": "system", "content": text},
        {"role": "user", "content": blob},
    ]


__all__ = [
    "DEFAULT_AI_PROMPT",
    "METADATA_PLACEHOLDER",
    "build_metadata_payload",
    "build_messages",
    "fields_from_metadata",
    "metadata_payload",
    "split_values",
]
