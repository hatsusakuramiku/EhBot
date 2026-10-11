"""Reading the AI tab's submissions, once, for the page and the JSON API.

Every refusal lives in `AiService`, so this module is a **thin translation and
nothing else** -- the page said that in a comment about `_ai_form_values` before
this file existed, and it is the reason there is not much here: a provider
without a name, a base URL that is not a URL, a model the deployment cannot
reach, are the service's words in both worlds.

What the translation does have to get right, and would drift if written twice:
`enabled` is a checkbox (absence means off), the key field is a textarea whose
value is never echoed back, and a chain entry must name a model.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.ai.errors import AI_CHAIN_ENTRY_MISSING, AiError
from app.ai.models import CHAIN_SCOPES
from app.settings.forms import checkbox, items_of

#: The ordered-list verbs the chain editors offer, one word each.
CHAIN_ACTIONS: tuple[str, ...] = ("primary", "append", "shift", "remove")

#: The chain-editing scopes, re-exported so a caller validates against the
#: vocabulary the service stores rather than a spelling of its own.
CHAIN_SCOPE_CODES: tuple[str, ...] = CHAIN_SCOPES


def provider_submission(values: Mapping[str, Any]) -> dict[str, Any]:
    """The provider form as the service's validator wants it.

    The refusals live in the service so the page, the JSON client and every
    chain editor enforce one set of rules.
    """
    return {
        "provider_id": values.get("provider_id"),
        "name": values.get("name"),
        "code": values.get("code"),
        "base_url": values.get("base_url"),
        "timeout_seconds": values.get("timeout_seconds"),
        "max_retries": values.get("max_retries"),
        "enabled": checkbox(values.get("enabled")),
        "custom_headers": values.get("custom_headers"),
        "default_params": values.get("default_params"),
    }


def key_text(values: Mapping[str, Any]) -> str:
    """The API keys textarea, one key per line, as the service splits it."""
    return str(values.get("api_keys") or "")


def model_names(values: Mapping[str, Any]) -> list[str]:
    """The model names to add.

    Repeated `model_name` fields because the 「拉取模型」 result arrives as a
    checkbox list and the manual field is the same form: ticking five models is
    one request, not five.
    """
    return [str(raw) for raw in items_of(values.get("model_name"))]


def chain_model_id(values: Mapping[str, Any]) -> int:
    """The model a chain verb acts on.

    A missing or non-numeric id is an `AiError` rather than a `ValueError` so
    both callers report it the same way -- the page in its error slot, the API in
    the error envelope.
    """
    raw = str(values.get("model_id") or "").strip()
    if not raw.isdigit():
        raise AiError(AI_CHAIN_ENTRY_MISSING, "请选择一个模型")
    return int(raw)


def chain_scope(raw: str) -> str:
    """One of the chain scopes, or a refusal naming them all."""
    code = raw.strip().lower()
    if code not in CHAIN_SCOPE_CODES:
        raise AiError(
            "CHAIN_SCOPE_UNKNOWN",
            f"未知的模型链作用域：{raw}",
        )
    return code


def chain_action(raw: str) -> str:
    """One of the four ordered-list verbs, or a refusal naming them all."""
    action = raw.strip().lower()
    if action not in CHAIN_ACTIONS:
        raise AiError(
            "CHAIN_ACTION_UNKNOWN",
            f"未知的模型链动作：{raw}",
        )
    return action


__all__ = [
    "CHAIN_ACTIONS",
    "CHAIN_SCOPE_CODES",
    "chain_action",
    "chain_model_id",
    "chain_scope",
    "key_text",
    "model_names",
    "provider_submission",
]
