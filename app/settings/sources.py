"""Reading a 来源规则 submission, once, for the page and the JSON API.

The sources tab has two forms that carry the same numbers and lists -- one for a
single source, one that applies a rule set to a selection -- and R74 added a
third caller, `PUT /api/v1/settings/sources`. The validation lives here rather
than in either route so the three cannot disagree about what an empty box means
(「清掉那条规则」) or about which chat ids are a channel and which are a private
chat.

Each reader returns `(payload, error)` instead of raising, for the page's reason:
it renders the sentence next to the form rather than turning a typo into a 500.
`SourceFieldError` carries the same sentence **and** a stable code, so the JSON
layer can put both into the error envelope without inventing a second wording.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.settings.forms import checkbox, items_of

#: The archive formats a source may admit, in the order the form lists them.
ARCHIVE_FORMAT_CHOICES: tuple[str, ...] = ("zip", "rar", "7z", "cbz")

#: The two kinds of chat id the whitelist distinguishes. The sign of the id *is*
#: the kind in Telegram, which is why the identity check tests both together.
SOURCE_TYPE_CHANNEL = "CHANNEL"
SOURCE_TYPE_PRIVATE_CHAT = "PRIVATE_CHAT"


class SourceFieldError(ValueError):
    """A submission the operator has to fix, with the sentence and a code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message


def parse_tags(raw: object) -> tuple[str, ...]:
    """A comma- or newline-separated tag list, lowercased and de-blanked."""
    cleaned: list[str] = []
    for value in items_of(raw):
        for item in str(value).replace("\n", ",").split(","):
            token = item.strip().lower()
            if token:
                cleaned.append(token)
    return tuple(cleaned)


def parse_source_rules(
    values: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, SourceFieldError | None]:
    """One source's filter rules.

    Shared by the per-row 更新规则 form, the batch 套用规则 action and the JSON
    layer, so the three cannot disagree about what an empty field means -- it
    clears that rule, which is the only reading an operator can predict from an
    empty box.
    """
    try:
        max_attachment_size_mb = int(
            str(values.get("max_attachment_size_mb") or "0")
        )
    except ValueError:
        return None, SourceFieldError("SIZE_INVALID", "附件上限格式无效")
    if max_attachment_size_mb < 0:
        return None, SourceFieldError("SIZE_INVALID", "附件上限无效")
    min_rating_raw = str(values.get("min_rating") or "").strip()
    min_rating: float | None = None
    if min_rating_raw:
        try:
            min_rating = float(min_rating_raw)
        except ValueError:
            return None, SourceFieldError("RATING_INVALID", "最低评分格式无效")
        if min_rating < 0:
            return None, SourceFieldError("RATING_INVALID", "最低评分格式无效")
    submitted_formats = {
        str(item) for item in items_of(values.get("allowed_archive_formats"))
    }
    return (
        {
            "allowed_archive_formats": [
                archive_format
                for archive_format in ARCHIVE_FORMAT_CHOICES
                if archive_format in submitted_formats
            ],
            "max_attachment_size_mb": max_attachment_size_mb,
            "required_tags": list(parse_tags(values.get("required_tags"))),
            "forbidden_tags": list(parse_tags(values.get("forbidden_tags"))),
            "allowed_languages": list(parse_tags(values.get("allowed_languages"))),
            "allowed_categories": list(parse_tags(values.get("allowed_categories"))),
            "min_rating": min_rating,
        },
        None,
    )


def parse_source(
    values: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, SourceFieldError | None]:
    """Identity plus rules, as `configure_telegram_source` takes them.

    The chat id's sign is the kind: a channel's id is negative, a private chat's
    is positive. A submission that disagrees is the operator having picked the
    wrong row in the type select, and the message says so once instead of letting
    the insert fail on a constraint.
    """
    source_type = str(values.get("source_type") or "")
    display_name = str(values.get("display_name") or "").strip()
    try:
        chat_id = int(str(values.get("chat_id") or ""))
        max_attachment_size_mb = int(
            str(values.get("max_attachment_size_mb") or "0")
        )
    except ValueError:
        chat_id = 0
        max_attachment_size_mb = -1
    valid_identity = (
        source_type == SOURCE_TYPE_CHANNEL and chat_id < 0
    ) or (
        source_type == SOURCE_TYPE_PRIVATE_CHAT and chat_id > 0
    )
    if not valid_identity or not display_name or max_attachment_size_mb < 0:
        return None, SourceFieldError(
            "SOURCE_INVALID", "来源类型、ID、名称或附件上限无效"
        )
    rules, error = parse_source_rules(values)
    if error is not None:
        return None, error
    assert rules is not None
    return (
        {
            "source_type": source_type,
            "chat_id": chat_id,
            "display_name": display_name,
            "enabled": checkbox(values.get("enabled")),
            **rules,
        },
        None,
    )


def selected_source_ids(values: Mapping[str, Any]) -> list[int]:
    """The `source_ids` of a batch submission, de-duplicated and in order."""
    ids: list[int] = []
    for value in items_of(values.get("source_ids")):
        try:
            number = int(str(value))
        except ValueError:
            continue
        if number not in ids:
            ids.append(number)
    return ids


__all__ = [
    "ARCHIVE_FORMAT_CHOICES",
    "SOURCE_TYPE_CHANNEL",
    "SOURCE_TYPE_PRIVATE_CHAT",
    "SourceFieldError",
    "parse_source",
    "parse_source_rules",
    "parse_tags",
    "selected_source_ids",
]
