"""Which messages are allowed to become candidates at all.

The default is the operator's own rule for this deployment: **only a message
that carries an ExHentai/e-hentai gallery link is parsed**. Everything else --
a bare photo, a `.zip` upload, a telegra.ph preview -- is ignored before any
metadata is fetched, any source row is discovered and (when AI admission is on)
before the model is paid for. It is the cheapest possible filter and it is the
one that matches the way the channels the bot watches actually post.

The rules are deliberately few and blunt. This is a page an operator opens once,
reads carefully and leaves alone, so it holds the *policy* -- may a message
without a link in be a candidate at all -- and not a general rule language. The
per-source filters (tags, language, size) stay on 来源规则 where they belong,
because those are about one channel; these are about the whole deployment.

Reads never raise and never return a partial shape: a value written by an older
build, or edited in the database by hand, falls back to the default. Writes are
strict (`validate_parse_rules`) and say which key was wrong, because a save is
the one moment the operator can fix it.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from app.candidates.models import ParsedSourceMessage

#: Where the rules live in `system_settings`. One JSON document rather than six
#: scalar keys: the six values are one decision, and six rows would let a
#: half-applied save exist.
PARSE_RULES_KEY = "parse_rules_json"

#: Every archive format the ingestor knows how to download. The parser's
#: `archive_formats` is a subset of this; a format outside it would be offered
#: by the form and then never matched by `mtproto.ARCHIVE_SUFFIXES`.
ARCHIVE_FORMATS: tuple[str, ...] = ("zip", "rar", "7z", "cbz")

#: The shipped scheme: gallery links only. See the module docstring.
DEFAULT_PARSE_RULES: dict[str, Any] = {
    "require_gallery_link": True,
    "accept_photo": False,
    "accept_archive": False,
    "archive_formats": list(ARCHIVE_FORMATS),
    "accept_preview": False,
    "title_required": False,
}

_BOOLEAN_KEYS: tuple[str, ...] = (
    "require_gallery_link",
    "accept_photo",
    "accept_archive",
    "accept_preview",
    "title_required",
)


class ParseRulesError(ValueError):
    """A parse-rules document an operator may not save."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message


def default_parse_rules() -> dict[str, Any]:
    """A fresh copy of the default scheme.

    A copy, not the module constant: the page hands the dict to a template and
    the ingestor may keep it, and one caller mutating the shared default would
    change the default for every other.
    """
    return json.loads(json.dumps(DEFAULT_PARSE_RULES))


def parse_rules_view(stored: str | None) -> dict[str, Any]:
    """The stored rules, read leniently, always in the full shape.

    A missing row means the default. Anything unreadable falls back to the
    default too: the alternative is an ingest loop that refuses to run because
    one settings row is corrupt, which is a worse failure than admitting a
    message the operator might not have wanted.
    """
    if not stored:
        return default_parse_rules()
    try:
        raw = json.loads(stored)
    except (TypeError, ValueError):
        return default_parse_rules()
    if not isinstance(raw, Mapping):
        return default_parse_rules()
    rules = default_parse_rules()
    for key in _BOOLEAN_KEYS:
        value = raw.get(key)
        if isinstance(value, bool):
            rules[key] = value
    formats = raw.get("archive_formats")
    if isinstance(formats, list):
        cleaned = [
            str(item).strip().lower()
            for item in formats
            if str(item).strip().lower() in ARCHIVE_FORMATS
        ]
        if cleaned:
            rules["archive_formats"] = list(dict.fromkeys(cleaned))
    return rules


def validate_parse_rules(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Normalise a submitted scheme, refusing anything that is not one.

    Strict on purpose: the form is the only caller, so an unknown key or a
    non-boolean toggle means the request was not built by this page, and
    storing it silently would leave the operator believing a rule was set that
    the ingestor will never read.
    """
    if not isinstance(raw, Mapping):
        raise ParseRulesError("PARSE_RULES_INVALID", "解析规则必须是键值集合")
    unknown = sorted(set(raw) - set(DEFAULT_PARSE_RULES))
    if unknown:
        raise ParseRulesError(
            "PARSE_RULES_UNKNOWN_KEY",
            f"未知的解析规则：{', '.join(unknown)}",
        )
    rules = default_parse_rules()
    for key in _BOOLEAN_KEYS:
        value = raw.get(key)
        if value is None:
            continue
        if not isinstance(value, bool):
            raise ParseRulesError(
                "PARSE_RULES_INVALID_VALUE", f"解析规则「{key}」必须是布尔值"
            )
        rules[key] = value
    formats = raw.get("archive_formats")
    if formats is not None:
        if isinstance(formats, str):
            formats = [chunk for chunk in formats.replace("\n", ",").split(",")]
        if not isinstance(formats, (list, tuple)):
            raise ParseRulesError(
                "PARSE_RULES_INVALID_VALUE", "允许的压缩格式必须是列表"
            )
        cleaned: list[str] = []
        for item in formats:
            token = str(item).strip().lower()
            if not token:
                continue
            if token not in ARCHIVE_FORMATS:
                raise ParseRulesError(
                    "PARSE_RULES_INVALID_FORMAT",
                    f"不支持的压缩格式：{token}",
                )
            if token not in cleaned:
                cleaned.append(token)
        if not cleaned:
            raise ParseRulesError(
                "PARSE_RULES_INVALID_FORMAT", "至少要允许一种压缩格式"
            )
        rules["archive_formats"] = cleaned
    return rules


def dump_parse_rules(rules: Mapping[str, Any]) -> str:
    """The stored form: compact JSON, keys in the canonical order."""
    return json.dumps(
        {key: rules[key] for key in DEFAULT_PARSE_RULES},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _archive_name(message: ParsedSourceMessage) -> str:
    for attachment in message.attachments:
        if str(attachment.get("type")) == "archive":
            return str(attachment.get("file_name") or "")
    return ""


def _archive_suffix(name: str) -> str:
    lowered = name.strip().lower()
    if "." not in lowered:
        return ""
    return lowered.rsplit(".", 1)[-1]


def message_qualifies(
    rules: Mapping[str, Any], message: ParsedSourceMessage
) -> tuple[bool, str]:
    """Whether one parsed message passes the scheme, and why.

    A gallery link always qualifies -- it is the canonical candidate and a rule
    set that rejected one would be a trap rather than a filter.
    `require_gallery_link` therefore only ever *adds* a requirement: with it on,
    a message without a link is refused before the other toggles are consulted,
    which is what 「只解析含 Eh 链接的消息」 means.
    """
    if message.ex_gid is not None:
        return True, "包含 ExHentai 画廊链接"
    if rules.get("require_gallery_link", True):
        return False, "解析规则要求消息包含 ExHentai 画廊链接"
    for attachment in message.attachments:
        if str(attachment.get("type")) != "archive":
            continue
        allowed = {
            str(item).strip().lower() for item in rules.get("archive_formats", ())
        }
        suffix = _archive_suffix(_archive_name(message))
        if rules.get("accept_archive") and suffix in allowed:
            return True, "包含压缩包附件"
        return False, f"解析规则不接受压缩格式 {suffix.upper() or '未知'}"
    if message.attachments and rules.get("accept_photo"):
        # Reached only when there is no archive attachment: the loop above
        # returned for one.
        return True, "包含图片预览"
    if message.preview_urls and rules.get("accept_preview"):
        return True, "包含预览页链接"
    if rules.get("title_required") and not message.title:
        return False, "解析规则要求标题，但消息没有可识别标题"
    return False, "按解析规则不接受此类消息"


def title_forces_needs_info(
    rules: Mapping[str, Any], message: ParsedSourceMessage
) -> bool:
    """Whether a missing title must be marked 待补充 even if the source accepts.

    The default (`title_required` off) leaves the decision to the source rules,
    which already answer 待补充 for a missing title; this is the explicit
    override for an operator who wants it to be the same everywhere.
    """
    return bool(rules.get("title_required")) and not message.title


__all__ = [
    "ARCHIVE_FORMATS",
    "DEFAULT_PARSE_RULES",
    "PARSE_RULES_KEY",
    "ParseRulesError",
    "default_parse_rules",
    "dump_parse_rules",
    "message_qualifies",
    "parse_rules_view",
    "title_forces_needs_info",
    "validate_parse_rules",
]
