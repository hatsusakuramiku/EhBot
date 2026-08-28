"""The auto-approval rule forms, behind the 自动通过 settings tab.

The rules themselves live in `app.auto_approval`; this is only the form that
builds one and the dry run that shows what it would have matched.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.api.serializers import auto_approval_dry_run
from app.api.status import SETTINGS_AUTO_APPROVAL
from app.auto_approval.rules import (
    RuleValidationError,
    render_rule_dsl,
    validate_rule_ast,
)
from app.auto_approval.service import AutomaticApprovalService
from app.web import deps
from app.web.settings_view import render_settings, settings_redirect

router = APIRouter()


def _parse_rule_condition(form) -> dict:
    """Build one automatic-approval AST from the editor's submitted rows.

    The editor submits parallel lists -- `condition_kind`, `condition_field`,
    `condition_operator`, `condition_value` -- because that is what repeated
    form field names give natively, so the rows survive with JavaScript off.
    A row whose field is blank is skipped, which is how the spare empty row
    the page always renders costs nothing.

    One row becomes that row's node rather than a group of one, matching what
    `render_rule_dsl` prints and what the browser previews: a simple rule
    should read simply in the stored DSL.

    A form carrying no rows at all falls back to the single `field`/`pattern`
    pair the page used before the editor existed. That shape is exactly one
    regex row, so nothing is lost by keeping it accepted.
    """
    kinds = form.getlist("condition_kind")
    fields = form.getlist("condition_field")
    operators = form.getlist("condition_operator")
    values = form.getlist("condition_value")

    def at(items: list, index: int, default: str = "") -> str:
        """One row's value from a parallel list, or the default.

        The lists can be short of each other: a browser omits an unchecked
        control, and a hand-built request may send fewer of one name than
        another. Reading by index with a default keeps that a missing value
        rather than an IndexError.
        """
        return str(items[index]) if index < len(items) else default

    children: list[dict] = []
    for index, raw_field in enumerate(fields):
        field = str(raw_field or "").strip()
        if not field:
            continue
        raw_value = at(values, index).strip()
        if at(kinds, index, "condition") == "regex":
            children.append(
                {"kind": "regex", "field": field, "pattern": raw_value}
            )
            continue
        operator = at(operators, index).upper()
        node: dict = {
            "kind": "condition",
            "field": field,
            "operator": operator,
        }
        if operator in {"HAS_ANY", "HAS_ALL"}:
            # A list operator gets a list, split the way `settings.js`
            # previews it, so 「chinese, futa」 means two tags in both places.
            node["value"] = [
                item.strip() for item in raw_value.split(",") if item.strip()
            ]
        elif operator not in {"EXISTS", "NOT_EXISTS"}:
            node["value"] = raw_value
        children.append(node)
    if not children:
        return {
            "kind": "regex",
            "field": str(form.get("field") or "").strip(),
            "pattern": str(form.get("pattern") or ""),
        }
    if len(children) == 1:
        return children[0]
    return {
        "kind": "group",
        "operator": str(form.get("group_operator") or "AND").upper(),
        "children": children,
    }


@router.get("/auto-approval-rules")
async def auto_approval_rules_page(request: Request):
    """Retired: 自动审批 is a tab of `/settings`.

    307 rather than 301 so no browser caches the move, and a redirect rather
    than a deletion so a bookmark still lands on the page that replaced it.
    """
    return RedirectResponse(
        request.url_for(
            "settings_section", section=SETTINGS_AUTO_APPROVAL
        ).path,
        status_code=307,
    )


@router.post("/auto-approval-rules")
async def save_auto_approval_rule(request: Request):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    form = await request.form()
    deps.validate_csrf(request, str(form.get("csrf_token") or ""))
    try:
        name = str(form.get("name") or "").strip()
        if not name:
            raise RuleValidationError("规则名称不能为空")
        priority = int(str(form.get("priority") or "100"))
        # `validate_rule_ast` is the gate, not the editor: every pattern is
        # compiled here, so a regex the browser accepted and Python does not
        # is refused at the moment it would be stored.
        ast = validate_rule_ast(_parse_rule_condition(form))
        await deps.database(request).save_auto_approval_rule(
            rule_id=None,
            name=name,
            enabled=form.get("enabled") == "on",
            priority=priority,
            condition=ast,
            dsl_snapshot=render_rule_dsl(ast),
        )
    except (RuleValidationError, ValueError, json.JSONDecodeError) as exc:
        return await render_settings(
            request,
            SETTINGS_AUTO_APPROVAL,
            error=str(exc),
            status_code=400,
        )
    return settings_redirect(request, SETTINGS_AUTO_APPROVAL)


@router.post("/auto-approval-rules/dry-run")
async def dry_run_auto_approval_rule(request: Request):
    """Report what the edited rule would match. Writes nothing.

    The same fields the save button submits, sent to a different endpoint by
    the same form, so what was tried is what gets saved. The condition is
    validated first: a trial run of an unusable rule would report 「命中 0」
    and read as「这条规则没用」rather than 「这条规则写错了」.
    """
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    form = await request.form()
    deps.validate_csrf(request, str(form.get("csrf_token") or ""))
    try:
        condition = validate_rule_ast(_parse_rule_condition(form))
    except (RuleValidationError, ValueError) as exc:
        return await render_settings(
            request,
            SETTINGS_AUTO_APPROVAL,
            error=str(exc),
            status_code=400,
        )
    result = await AutomaticApprovalService(deps.database(request)).dry_run(condition)
    return await render_settings(
        request,
        SETTINGS_AUTO_APPROVAL,
        dry_run=auto_approval_dry_run(result),
    )


@router.post("/auto-approval-rules/{rule_id}/toggle")
async def toggle_auto_approval_rule(rule_id: int, request: Request):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    form = await request.form()
    deps.validate_csrf(request, str(form.get("csrf_token") or ""))
    await deps.database(request).set_auto_approval_rule_enabled(
        rule_id, form.get("enabled") == "on"
    )
    return settings_redirect(request, SETTINGS_AUTO_APPROVAL)


@router.post("/auto-approval-rules/{rule_id}/preview")
async def preview_auto_approval_rule(rule_id: int, request: Request):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    form = await request.form()
    deps.validate_csrf(request, str(form.get("csrf_token") or ""))
    rule = await deps.database(request).get_auto_approval_rule(rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="规则不存在")
    return await render_settings(
        request,
        SETTINGS_AUTO_APPROVAL,
        preview_ids=await AutomaticApprovalService(deps.database(request)).preview(rule),
        preview_rule_id=rule_id,
    )
