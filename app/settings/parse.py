"""Reading the 解析规则 tab's two writes, once, for the page and the JSON API.

Both are small and both are the kind of small that drifts: a checkbox whose
absence has to mean off, a format list filtered against the deployment's own
vocabulary, and one rule with real content behind it -- a prompt submitted
byte-identical to the shipped default is stored as 「use the default」 rather than
pinned as an override. Written here so `PUT /api/v1/settings/parse` (R75) and the
page cannot disagree about any of the three.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.candidates.parse_rules import ARCHIVE_FORMATS
from app.settings.forms import checkbox, items_of

#: The archive formats the admission parser recognises. The vocabulary is the
#: deployment's, so an unlisted value in a submission is dropped rather than
#: stored: a typo must not become a rule that can never match. Re-exported from
#: the parser's own table rather than respelled, so a format added there is
#: offered here without a second edit.
PARSE_ARCHIVE_FORMATS: tuple[str, ...] = ARCHIVE_FORMATS

#: The AI candidate gate's switches. `ai_candidate_fallback` is one of these
#: words; the values live in `app/ai/models.py` and are validated by the service,
#: not here.
CANDIDATE_ADMISSION_FIELDS: tuple[str, ...] = (
    "ai_candidate_enabled",
    "ai_candidate_override_parse_rules",
    "ai_candidate_prompt",
    "ai_candidate_fallback",
)


def parse_rules_submission(values: Mapping[str, Any]) -> dict[str, Any]:
    """The candidate-admission parse scheme.

    Every checkbox is read explicitly rather than defaulted through: an
    unchecked box sends nothing, so absence means off, and letting the validator
    fill in a default for an omitted key would make unchecking a rule silently
    keep it on.
    """
    submitted = {
        str(item) for item in items_of(values.get("archive_formats"))
    }
    return {
        "require_gallery_link": checkbox(values.get("require_gallery_link")),
        "accept_photo": checkbox(values.get("accept_photo")),
        "accept_archive": checkbox(values.get("accept_archive")),
        "accept_preview": checkbox(values.get("accept_preview")),
        "archive_formats": [
            archive_format
            for archive_format in PARSE_ARCHIVE_FORMATS
            if archive_format in submitted
        ],
    }


def candidate_admission(
    values: Mapping[str, Any], *, default_prompt: str
) -> dict[str, Any]:
    """The AI candidate gate's switches and prompt.

    A prompt submitted byte-identical to the shipped default is stored as an
    empty string, i.e. 「use the default」: otherwise virtually every save of this
    form would pin the default text as an override, and the page could never say
    whether the operator had actually customised it.
    """
    prompt = str(values.get("ai_candidate_prompt") or "").strip()
    if prompt == default_prompt.strip():
        prompt = ""
    return {
        "ai_candidate_enabled": checkbox(values.get("ai_candidate_enabled")),
        "ai_candidate_override_parse_rules": checkbox(
            values.get("ai_candidate_override_parse_rules")
        ),
        "ai_candidate_prompt": prompt,
        "ai_candidate_fallback": str(values.get("ai_candidate_fallback") or ""),
    }


__all__ = [
    "CANDIDATE_ADMISSION_FIELDS",
    "PARSE_ARCHIVE_FORMATS",
    "candidate_admission",
    "parse_rules_submission",
]
