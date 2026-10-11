"""Reading an 自动审批 rule, once, for the page and the JSON API.

Only the *validation* is shared, not the parsing of the condition editor: the
page builds its AST from parallel `condition_field` / `condition_operator` /
`condition_value` lists (`app/web/rule_forms.py`), while a JSON caller sends the
AST it means. Both hand the result to the same readers here, so a rule the page
would refuse cannot be stored through the API -- an empty name, an unknown
action, a condition the engine cannot evaluate, or a `rule_id` that no longer
exists all fail in one place.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.auto_approval.models import RULE_ACTION_APPROVE, RULE_ACTIONS
from app.auto_approval.rules import (
    RuleValidationError,
    render_rule_dsl,
    validate_rule_ast,
)
from app.settings.forms import checkbox


def _validated_condition(condition: Any) -> dict[str, Any]:
    if condition is None:
        raise RuleValidationError("请至少填写一个条件")
    return validate_rule_ast(condition)


def _action_of(values: Mapping[str, Any]) -> str:
    # Checked here rather than left to the column CHECK: an unknown action is a
    # form the operator cannot produce, and a 400 naming it beats a 500 from a
    # constraint violation.
    action = str(values.get("action") or RULE_ACTION_APPROVE)
    if action not in RULE_ACTIONS:
        raise RuleValidationError("未知的规则动作")
    return action


def rule_payload(
    values: Mapping[str, Any], *, condition: Any
) -> dict[str, Any]:
    """The arguments `save_auto_approval_rule` takes.

    One shape for create and update because it is one editor: the only
    difference on the wire is `rule_id`, and the database layer already branches
    on `rule_id is None`. An unparsable `rule_id` is a refusal rather than a
    fallback to create -- silently inserting a second rule when an edit was
    meant is how an operator ends up with two rules approving everything.
    """
    name = str(values.get("name") or "").strip()
    if not name:
        raise RuleValidationError("规则名称不能为空")
    priority = int(str(values.get("priority") or "100"))
    raw_rule_id = str(values.get("rule_id") or "").strip()
    rule_id = int(raw_rule_id) if raw_rule_id else None
    ast = _validated_condition(condition)
    return {
        "rule_id": rule_id,
        "name": name,
        "enabled": checkbox(values.get("enabled")),
        "priority": priority,
        "condition": ast,
        # Rendered here so every stored rule carries the sentence it read as at
        # the version that fired; the browser previews the same rendering.
        "dsl_snapshot": render_rule_dsl(ast),
        "case_sensitive": checkbox(values.get("case_sensitive")),
        "action": _action_of(values),
    }


def dry_run_payload(
    values: Mapping[str, Any], *, condition: Any
) -> dict[str, Any]:
    """What `AutomaticApprovalService.dry_run` needs.

    The condition is validated first, and for the page's reason: a trial run of
    an unusable rule would report 「命中 0」 and read as 「这条规则没用」rather
    than 「这条规则写错了」.
    """
    return {
        "condition": _validated_condition(condition),
        "case_sensitive": checkbox(values.get("case_sensitive")),
        "action": _action_of(values),
    }


__all__ = ["dry_run_payload", "rule_payload"]
