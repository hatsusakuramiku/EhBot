"""The auto-approval rule forms, behind the 自动通过 settings tab.

The rules themselves live in `app.auto_approval`; this is only the form that
builds one and the dry run that shows what it would have matched.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.api.serializers import auto_approval_dry_run
from app.api.status import SETTINGS_AUTO_APPROVAL
from app.auto_approval.rules import (
    RuleValidationError,
    editor_rows,
)
from app.auto_approval.service import AutomaticApprovalService
from app.settings.auto_approval import dry_run_payload
from app.settings.forms import form_values
from app.web import deps
# The row -> AST parser is shared with the paths tab's rule editor, so both
# rule forms mean the same things on the wire.
from app.web.rule_forms import parse_rule_condition as _parse_rule_condition
from app.web.settings_view import render_settings
from app.web.rule_commands import rule_form, rule_draft

router = APIRouter()


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
    """Apply the shared rule command, preserving the original draft."""
    return await rule_form(request, 'auto-approval', 'save')


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
        payload = dry_run_payload(
            form_values(form), condition=_parse_rule_condition(form)
        )
    except (RuleValidationError, ValueError) as exc:
        return await render_settings(
            request,
            SETTINGS_AUTO_APPROVAL,
            error=str(exc),
            status_code=400,
            rule_draft=rule_draft(form, 'auto-approval'),
        )
    result = await AutomaticApprovalService(deps.database(request)).dry_run(
        payload["condition"],
        case_sensitive=payload["case_sensitive"],
        action=payload["action"],
    )
    return await render_settings(
        request,
        SETTINGS_AUTO_APPROVAL,
        dry_run=auto_approval_dry_run(result),
        rule_draft=rule_draft(form, 'auto-approval'),
    )


@router.post("/auto-approval-rules/{rule_id}/toggle")
async def toggle_auto_approval_rule(rule_id: int, request: Request):
    """Apply the shared rule command, preserving the original draft."""
    return await rule_form(request, 'auto-approval', 'toggle', rule_id=rule_id)


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
            "rule_uuid": rule.rule_uuid,
            "expected_revision": rule.version,
            "name": rule.name,
            "priority": rule.priority,
            "enabled": rule.enabled,
            "group_operator": group_operator,
            "case_sensitive": rule.case_sensitive,
            "action": rule.action,
            "rows": list(rows),
        },
    )


@router.post("/auto-approval-rules/{rule_id}/delete")
async def delete_auto_approval_rule(rule_id: int, request: Request):
    """Apply the shared rule command, preserving the original draft."""
    return await rule_form(request, 'auto-approval', 'delete', rule_id=rule_id)


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
