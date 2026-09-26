"""The AI path decision: prompt in, a library-relative path out, cached.

The one place that answers 「AI 说这本书放哪」. It sits above the provider layer
(`app.ai.service` knows *who* answers) and below `ConversionService` (which knows
*where* the library root is), so the packing path and the operator-facing plan
share exactly one implementation -- the proposal's §3 chain is 「接一个分支，不各
写一份」.

Three ideas carry the file:

* **The answer is cached, and the cache is semantics.** The model is not
  deterministic and one decision walks 供应商 → Key → 模型, so asking again for
  every page render, pack and re-file would make those three disagree. The
  fingerprint covers the metadata, the prompt, the model chain and each
  provider's base URL -- **not** the API keys, because rotating a key must not
  invalidate the library.
* **The model's answer is cleaned, and cleaning is not refusal.** Illegal
  punctuation goes through `safe_library_name` (the function the packer has
  always used); only `..`, an absolute path, an empty filename or a path past
  the ceiling is refused. A model that returns 「同人志/社團: A」 should produce a
  folder, not a 需干预 card.
* **Rendering never asks the model.** `cached` / `is_current` are pure reads of
  the table; only `resolve` calls out, and it is reached from a packing job.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import PurePosixPath
from typing import Any, Sequence

from app.ai.errors import AI_PATH_INVALID, AiError
from app.ai.models import (
    AiModelChainEntry,
    AiPathOutcome,
    AiPathSuggestion,
)
from app.ai.prompt import build_messages, metadata_payload
from app.ai.service import extract_json_object
from app.conversion.naming import (
    MAX_RELATIVE_PATH_LENGTH,
    safe_library_name,
)

_LOG = logging.getLogger(__name__)

#: `C:` or `C:\\` -- a drive-qualified answer is an absolute path wearing a
#: relative one's clothes, and joining it onto the library root would either
#: escape or silently drop a level.
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")

#: Field names the model is asked for. Anything else in its object is ignored
#: rather than refused: a model that adds `"reason"` has still answered, and
#: rejecting it would turn a usable path into 需干预 over a courtesy field.
_FIELD_DIRECTORY = "directory"
_FIELD_FILENAME = "filename"


def prompt_hash_of(prompt: str) -> str:
    """Stable hash of the prompt text, for the §4 「prompt 没变吗」 question."""
    return hashlib.sha256((prompt or "").encode("utf-8")).hexdigest()


def chain_signature(chain: Sequence[AiModelChainEntry]) -> str:
    """The model chain as one string for the fingerprint.

    Ordered, because 「主力 + 备用」 is an ordering and swapping two entries can
    change which model answers. Deliberately built from the base URL and the
    model name rather than the provider's display name or id: renaming a
    provider in the settings page is cosmetic and must not invalidate the whole
    library, while pointing it at a different address or model genuinely might.
    """
    return "\n".join(
        f"{entry.position}|{entry.provider.base_url}|{entry.model.name}"
        for entry in chain
    )


def fingerprint_of(
    payload: dict[str, Any], prompt: str, signature: str
) -> str:
    """One hash over everything that could change the answer.

    `sort_keys=True` so the JSON spelling is a function of the values and not of
    dict insertion order -- a fingerprint that changed when a helper was
    refactored would silently re-ask the model for the whole library.
    """
    document = json.dumps(
        {"metadata": payload, "prompt": prompt or "", "chain": signature},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


def clean_ai_path(directory: str, filename: str) -> tuple[PurePosixPath, list[str]]:
    """The model's two strings as a checked, sanitised library-relative path.

    Returns the path *with* `.cbz` appended and the list of segment values that
    had to be sanitised (empty when the model was already clean), so the caller
    can log `ai_path_sanitized` without comparing the strings itself.

    Raises `AI_PATH_INVALID` for the four cases that are refusals rather than
    punctuation problems: an absolute path or drive, a `.`/`..` segment, an
    empty filename after cleaning, and a whole path past the ceiling. Everything
    else -- `<>:"/\\|?*`, control characters, a Windows reserved name, a
    leading/trailing dot or space, an over-long segment -- is repaired by
    `safe_library_name`, because 「把这个题目变成合法段」 is a solved problem and
    failing a book over a colon helps nobody.
    """
    def refuse(reason: str) -> AiError:
        return AiError(AI_PATH_INVALID, f"AI 返回的路径不可用：{reason}")

    text_directory = "" if directory is None else str(directory)
    text_filename = "" if filename is None else str(filename)
    # A model that mirrored a Windows path's backslashes gets the same treatment
    # as one that used `/`; leaving `\` in place would sanitise a separator into
    # a space and collapse two levels into one odd folder name.
    text_directory = text_directory.replace("\\", "/")
    text_filename = text_filename.replace("\\", "/")
    if text_filename.strip() in {".", ".."}:
        raise refuse("文件名不能是 . 或 ..")
    if text_directory.strip().startswith("/") or _WINDOWS_DRIVE.match(
        text_directory.strip()
    ):
        raise refuse("目录不能是绝对路径")
    if text_filename.strip().startswith("/") or _WINDOWS_DRIVE.match(
        text_filename.strip()
    ):
        raise refuse("文件名不能是绝对路径")

    sanitised: list[str] = []
    levels: list[str] = []
    for raw in text_directory.split("/"):
        segment = raw.strip()
        if segment in {"", ".", ".."}:
            if segment:
                raise refuse("目录不能包含 . 或 ..")
            # An empty level is 「这一层没有值」 -- the model wrote
            # `社团//作者` because it had no name for the middle, which is
            # exactly the 「缺失就整层省略」 instruction. Omit it.
            continue
        cleaned = safe_library_name(segment, fallback="")
        if not cleaned:
            # Sanitising left nothing (a segment of nothing but illegal
            # characters). The level has no name, so it is omitted rather than
            # fabricated -- the same answer as an empty segment.
            continue
        if cleaned != segment:
            sanitised.append(segment)
        levels.append(cleaned)

    name = text_filename.strip()
    if not name:
        raise refuse("文件名为空")
    cleaned_name = safe_library_name(name, fallback="")
    if not cleaned_name:
        raise refuse("文件名清洗后为空")
    if cleaned_name != name:
        sanitised.append(name)

    relative = PurePosixPath(*levels, f"{cleaned_name}.cbz")
    if len(str(relative)) > MAX_RELATIVE_PATH_LENGTH:
        raise refuse(
            f"路径长度 {len(str(relative))} 超过上限 {MAX_RELATIVE_PATH_LENGTH}"
        )
    return relative, sanitised


def answer_path(text: str) -> tuple[PurePosixPath, list[str]]:
    """Parse one assistant message into a checked path.

    JSON first, then the two fields, then `clean_ai_path`. A message that is not
    JSON, or whose fields are not strings, is a failure like any other: the
    caller walks to the next model.
    """
    payload = extract_json_object(text)
    directory = payload.get(_FIELD_DIRECTORY, "")
    filename = payload.get(_FIELD_FILENAME, "")
    if not isinstance(directory, str) or not isinstance(filename, str):
        raise AiError(AI_PATH_INVALID, "AI 返回的路径字段不是字符串")
    return clean_ai_path(directory, filename)


class AiPathService:
    """The AI answer for one book, cached, with the fingerprint that keys it."""

    def __init__(self, database: Any, ai_service: Any, settings: Any) -> None:
        self._database = database
        self._ai = ai_service
        self._settings = settings

    # ------------------------------------------------------------------
    #  Configuration, read through the settings service so one place owns it
    # ------------------------------------------------------------------
    async def source(self) -> str:
        return await self._settings.path_source()

    async def prompt(self) -> str:
        return await self._settings.ai_prompt()

    async def fallback_to_rules(self) -> bool:
        return await self._settings.ai_fallback_to_rules()

    async def fingerprint(self, metadata: Sequence[Any]) -> str:
        """The current fingerprint for this book, without calling anything."""
        prompt = await self.prompt()
        chain = await self._ai.chain()
        return fingerprint_of(
            metadata_payload(metadata), prompt, chain_signature(chain)
        )

    # ------------------------------------------------------------------
    #  Reads: never call the model
    # ------------------------------------------------------------------
    async def suggestion(self, candidate_id: int) -> AiPathSuggestion | None:
        """The raw cached row, whatever its fingerprint. For the badge."""
        return await self._database.get_ai_path_suggestion(candidate_id)

    async def current(
        self, candidate_id: int, metadata: Sequence[Any]
    ) -> AiPathSuggestion | None:
        """The cached answer *if* it was produced from today's inputs.

        A stale row is as good as no row here: its path was decided from a
        different prompt, chain or metadata, and showing it would be the one
        thing an operator-facing read must not do -- present an answer the
        packer will not produce.
        """
        cached = await self._database.get_ai_path_suggestion(candidate_id)
        if cached is None:
            return None
        if cached.fingerprint != await self.fingerprint(metadata):
            return None
        return cached

    async def is_current(
        self, candidate_id: int, metadata: Sequence[Any], recorded_path: str | None
    ) -> bool:
        """The proposal's §4 predicate: 「重新问一遍也还是这个答案」.

        Cache exists, was produced from today's prompt / chain / metadata, and
        its path is the one recorded for the book. All three, because the point
        is that re-asking cannot change anything -- the recorded path matters
        because an operator may have renamed the book, and the fingerprint
        matters because a new prompt or model chain can.
        """
        if not recorded_path:
            return False
        cached = await self.current(candidate_id, metadata)
        return cached is not None and cached.relative_path == recorded_path

    async def badge(self, candidate_id: int, recorded_path: str | None) -> bool:
        """Whether the recorded path is an AI path. Pure read, no fingerprint.

        The proposal's §11 rule, and deliberately looser than `is_current`: a
        book whose prompt has since changed still *was* named by the model, and
        a rename by hand makes the two disagree so the badge turns off by
        itself. That is the whole labelling mechanism -- no column.
        """
        if not recorded_path:
            return False
        cached = await self._database.get_ai_path_suggestion(candidate_id)
        return cached is not None and cached.relative_path == recorded_path

    # ------------------------------------------------------------------
    #  The one call that asks
    # ------------------------------------------------------------------
    async def resolve(
        self,
        candidate_id: int,
        metadata: Sequence[Any],
        *,
        refresh: bool = False,
    ) -> AiPathOutcome:
        """The path for this book: the cache when it is current, else the model.

        Reached only from a packing job, so a slow or failing provider parks one
        task rather than blocking a page. On success the answer is written down;
        on failure nothing is written -- a network outage must not be
        fossilised into a book's name.

        `refresh` is 「重新询问」 as an explicit instruction rather than as a
        consequence: it skips the cache shortcut even when the fingerprint still
        matches. Only the re-archive sweep's 强制 and its 「重新计算路径」 job set
        it, and only because the operator said so -- re-asking a question the
        cache already answers is a spend, and the answer is written back over
        the cached row either way.
        """
        prompt = await self.prompt()
        payload = metadata_payload(metadata)
        chain = await self._ai.chain()
        fingerprint = fingerprint_of(
            payload, prompt, chain_signature(chain)
        )
        cached = await self._database.get_ai_path_suggestion(candidate_id)
        if (
            not refresh
            and cached is not None
            and cached.fingerprint == fingerprint
        ):
            return AiPathOutcome(cached, from_cache=True)

        try:
            # `validate` is what makes 「模型答了但不是路径」 walk the chain like
            # any other failure: the primary may return prose while a fallback
            # answers properly, and that is the whole point of a fallback.
            answer = await self._ai.complete(
                build_messages(prompt, payload), validate=answer_path
            )
            relative, sanitised = answer_path(answer.text)
        except AiError as exc:
            _LOG.warning(
                "ai_path_failed",
                extra={
                    "candidate_id": candidate_id,
                    "error_code": exc.code,
                    "error_message": exc.public_message,
                },
            )
            raise
        if sanitised:
            _LOG.info(
                "ai_path_sanitized",
                extra={
                    "candidate_id": candidate_id,
                    "segments": sanitised,
                },
            )
        suggestion = AiPathSuggestion(
            candidate_id=candidate_id,
            fingerprint=fingerprint,
            prompt_hash=prompt_hash_of(prompt),
            relative_path=relative.as_posix(),
            directory=str(relative.parent) if len(relative.parts) > 1 else "",
            filename=relative.stem,
            provider_id=answer.provider_id,
            model_name=answer.model_name,
        )
        await self._database.save_ai_path_suggestion(suggestion)
        _LOG.info(
            "ai_path_suggested",
            extra={
                "candidate_id": candidate_id,
                "provider_id": answer.provider_id,
                "model": answer.model_name,
                "relative_path": suggestion.relative_path,
            },
        )
        return AiPathOutcome(suggestion, from_cache=False)


__all__ = [
    "AiPathService",
    "answer_path",
    "chain_signature",
    "clean_ai_path",
    "fingerprint_of",
    "prompt_hash_of",
]
