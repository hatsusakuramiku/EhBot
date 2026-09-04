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
    editor_rows,
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
    """Create a rule, or overwrite the one `rule_id` names.

    One endpoint for both because it is one form: the editor renders with the
    fields filled when editing and blank when creating, and the only difference
    on the wire is a hidden `rule_id`. Splitting it would put the same
    validation in two routes, and `save_auto_approval_rule` in the database
    layer already branches on `rule_id is None` -- this passes that decision
    through rather than making a second one.

    An update bumps `version`, which is why the editor is safe to reuse: the
    rule snapshot stored on every past automatic approval names the version it
    fired as, so editing a rule cannot rewrite the history of what it did.
    """
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
        # Absent means create. An unparsable one is a refusal rather than a
        # fallback to create: silently inserting a second rule when an edit was
        # meant is how an operator ends up with two rules approving everything.
        raw_rule_id = str(form.get("rule_id") or "").strip()
        rule_id = int(raw_rule_id) if raw_rule_id else None
        # `validate_rule_ast` is the gate, not the editor: every pattern is
        # compiled here, so a regex the browser accepted and Python does not
        # is refused at the moment it would be stored.
        ast = validate_rule_ast(_parse_rule_condition(form))
        await deps.database(request).save_auto_approval_rule(
            rule_id=rule_id,
            name=name,
            enabled=form.get("enabled") == "on",
            priority=priority,
            condition=ast,
            dsl_snapshot=render_rule_dsl(ast),
        )
    except LookupError:
        # The rule was deleted between the page render and the save. Reported as
        # a 404 rather than re-created under its old id, which would resurrect a
        # rule the operator had removed.
        raise HTTPException(status_code=404, detail="规则不存在") from None
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


@router.get("/auto-approval-rules/{rule_id}/edit")
async def edit_auto_approval_rule(rule_id: int, request: Request):
    """Render the 自动审批 tab with this rule loaded into the editor.

    A GET, so 编辑 is a link an operator can open in a new tab and the URL says
    what is being edited. It renders the same tab through `render_settings`
    rather than a form of its own -- there is one editor, and a second copy
    filled from a stored rule is how the two would drift.

    `editor_rows` returns None for a nested condition group, which the flat
    editor cannot represent. That is passed through as `edit_unsupported` rather
    than as an error: the tab still renders, the rule is still listed, and the
    page explains that this one has to be replaced rather than edited.
    """
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    rule = await deps.database(request).get_auto_approval_rule(rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="规则不存在")
    decomposed = editor_rows(rule.condition)
    if decomposed is None:
        return await render_settings(
            request,
            SETTINGS_AUTO_APPROVAL,
            edit_rule_id=rule_id,
            edit_unsupported=True,
        )
    group_operator, rows = decomposed
    return await render_settings(
        request,
        SETTINGS_AUTO_APPROVAL,
        edit_rule_id=rule_id,
        edit_rule={
            "rule_id": rule.rule_id,
            "name": rule.name,
            "priority": rule.priority,
            "enabled": rule.enabled,
            "group_operator": group_operator,
            "rows": list(rows),
        },
    )


@router.post("/auto-approval-rules/{rule_id}/delete")
async def delete_auto_approval_rule(rule_id: int, request: Request):
    """Delete a rule for good.

    A POST behind `ui.confirm`, because it is the one action on this tab that
    cannot be undone from the interface -- 停用 is the reversible half and is
    deliberately still its own button, so an operator parking a rule for an
    afternoon is never pushed toward deleting it.
    """
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    form = await request.form()
    deps.validate_csrf(request, str(form.get("csrf_token") or ""))
    try:
        await deps.database(request).delete_auto_approval_rule(rule_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="规则不存在") from None
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
