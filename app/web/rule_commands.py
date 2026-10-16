"""Plain rule forms retain exactly the version and draft the operator saw."""
import json

from app.auto_approval.rules import RuleValidationError
from app.review.commands import WorkCommandError
from app.settings.forms import checkbox, form_values
from app.settings.rule_commands import RuleCommands
from app.web import deps
from app.web.rule_forms import parse_rule_condition
from app.web.settings_view import render_settings, settings_redirect


def rule_draft(form, domain, action='save', rule_id=None):
    values = form_values(form)
    values['enabled'] = checkbox(form.get('enabled'))
    values['case_sensitive'] = checkbox(form.get('case_sensitive'))
    values['rows'] = [{'field': field,
                       'operator': (form.getlist('condition_operator') + [''] * (index + 1))[index],
                       'value': (form.getlist('condition_value') + [''] * (index + 1))[index]}
                      for index, field in enumerate(form.getlist('condition_field'))]
    values['rule_id'] = rule_id if rule_id is not None else form.get('rule_id' if domain == 'auto-approval' else 'path_rule_id')
    values.setdefault('group_operator', 'AND')
    values.setdefault('action', 'APPROVE')
    return {'domain': domain, 'action': action, 'values': values}


async def rule_form(request, domain, action, *, rule_id=None):
    redirect = deps.require_authenticated(request)
    if redirect:
        return redirect
    form = await request.form()
    deps.validate_csrf(request, str(form.get('csrf_token') or ''))
    raw = form_values(form)
    draft = rule_draft(form, domain, action, rule_id)
    try:
        if raw.get('confirmed_command'):
            try:
                confirmed = json.loads(str(raw['confirmed_command']))
                raw.update({name: confirmed[name] for name in ('expected_revision', 'idempotency_key')})
                draft['values'].update({name: raw[name] for name in ('expected_revision', 'idempotency_key')})
            except (ValueError, KeyError, TypeError):
                raise WorkCommandError('CONFIRMATION_INVALID', '保存确认无效，请重新核对草稿') from None
        try:
            raw['expected_revision'] = int(str(raw.get('expected_revision') or ''))
        except ValueError:
            raw['expected_revision'] = None
        if action == 'save':
            raw['condition'] = parse_rule_condition(form)
        await RuleCommands(deps.database(request)).execute(domain, action, raw, rule_id=rule_id)
    except (WorkCommandError, RuleValidationError, ValueError) as exc:
        return await render_settings(request, domain,
            error=exc.public_message if isinstance(exc, WorkCommandError) else str(exc),
            status_code=exc.status_code if isinstance(exc, WorkCommandError) else 400,
            rule_draft=draft, rule_retry_url=request.url.path if action != 'save' else None)
    return settings_redirect(request, domain)
