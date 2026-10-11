"""Reading the 路径与命名 tab's writes, once, for the page and the JSON API.

The routing-rule reader is the auto-approval reader's twin -- same rule engine,
same validation, same refusal sentences -- with one difference that is the whole
reason it is a second function rather than a parameter: a path rule also carries
the template it routes to, validated by the archive settings service, so a rule
that could not render is refused while the operator is watching rather than at
pack time.

The condition itself arrives the same way it does on the auto-approval tab: the
page builds it from its row editor, a JSON caller sends the AST.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.auto_approval.rules import RuleValidationError, validate_rule_ast
from app.settings.forms import checkbox


def path_rule_payload(
    values: Mapping[str, Any], *, condition: Any
) -> dict[str, Any]:
    """The arguments `save_path_rule` takes.

    Create and update are one shape because they are one editor: the only
    difference on the wire is `path_rule_id`, and an unparsable one is a refusal
    rather than a fallback to create -- silently inserting a second rule when an
    edit was meant is how an operator ends up with two rules routing everything.
    """
    name = str(values.get("name") or "").strip()
    if not name:
        raise RuleValidationError("规则名称不能为空")
    priority = int(str(values.get("priority") or "100"))
    raw_rule_id = str(values.get("path_rule_id") or "").strip()
    rule_id = int(raw_rule_id) if raw_rule_id else None
    if condition is None:
        raise RuleValidationError("请至少填写一个条件")
    return {
        "rule_id": rule_id,
        "name": name,
        "enabled": checkbox(values.get("enabled")),
        "priority": priority,
        "condition": validate_rule_ast(condition),
        "path_template": str(values.get("path_template") or ""),
        "case_sensitive": checkbox(values.get("case_sensitive")),
    }


def template_payload(values: Mapping[str, Any]) -> dict[str, Any]:
    """What the layout-template form writes.

    `title_source` is only present when the operator touched the radio, so an
    absent key means 「leave the preference alone」 rather than 「clear it」.
    """
    payload: dict[str, Any] = {
        "library_template": str(values.get("library_template") or "")
    }
    if "title_source" in values:
        payload["title_source"] = str(values.get("title_source") or "")
    return payload


__all__ = ["path_rule_payload", "template_payload"]
