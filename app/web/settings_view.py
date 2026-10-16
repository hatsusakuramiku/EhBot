"""The settings page, rendered from the same snapshot the JSON layer serves.

Not a router: four route modules answer with a settings tab -- the settings pages
themselves, the auto-approval forms, the connection forms and the password change
-- and a rejected save has to come back as the tab it was submitted from. Sharing
the renderer is what keeps those four from drifting into four settings pages.
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import Request
from fastapi.responses import RedirectResponse

from app.api.settings import settings_snapshot
from app.settings.forms import checkbox
from app.web import deps


async def render_settings(
    request: Request,
    section: str,
    *,
    error: str | None = None,
    notice: str | None = None,
    status_code: int = 200,
    **extra: object,
):
    """Render one settings tab from the shared snapshot.

    Every settings response comes through here -- the seven GETs, and each of
    the POSTs that refuses something -- so a rejected save re-renders the
    page the operator was looking at rather than a reduced copy of it. R5's
    lesson, applied to the last domain that still had two renderings of one
    page.

    The context is `settings_snapshot`, the same coroutine
    `GET /api/v1/settings/{section}` serves. Anything a template needed that
    the snapshot does not carry would be invisible to the endpoint, so there
    is nothing computed here except the three things that belong to this one
    request: the CSRF token and the outcome message.

    `extra` is for the result of an action rather than a stored setting -- a
    trial run, a rendered path preview. Those exist for exactly one response
    and have no place in a snapshot of what is saved.
    """
    context = await settings_snapshot(request, section)
    context.update(
        {
            "csrf_token": request.session["csrf_token"],
            "error": error,
            "notice": notice,
            "source_command_key": str(uuid4()),
        }
    )
    context.update(extra)
    credential_draft = extra.get('credential_draft')
    if credential_draft:
        action = credential_draft['action']
        if action == 'delete':
            current = next((item for item in context['passwords']
                            if item['password_uuid'] == credential_draft.get('password_uuid')), None)
            context['credential_current_revision'] = current['revision'] if current else None
        else:
            scope = 'api-key' if action.startswith('api-key-') else 'vault'
            context['credential_current_revision'] = context['settings_versions'][scope]
    def ai_network_conditions(action, provider=None, model=None):
        if action == 'all':
            return {'expected_revision': context['settings_versions']['ai-catalogue']}
        values = {'provider_uuid': provider['provider_uuid'], 'keys_revision': provider['keys_revision']}
        if action == 'model':
            values.update(model_uuid=model['model_uuid'], expected_revision=model['revision'], provider_revision=provider['revision'])
        else:
            values['expected_revision'] = provider['revision']
            if action == 'provider':
                values['models_revision'] = provider['models_revision']
        return values

    context['ai_network_conditions'] = ai_network_conditions
    network_draft = extra.get('ai_network_draft')
    if network_draft:
        action, values = network_draft['action'], network_draft['values']
        provider = next((row for row in context.get('providers', []) if row['provider_uuid'] == values.get('provider_uuid')), None)
        model = next((row for row in provider['models'] if row['model_uuid'] == values.get('model_uuid')), None) if provider else None
        context['ai_network_current'] = ai_network_conditions(action, provider, model) if (
            action == 'all' or (provider and (action != 'model' or model))) else None
    operation_id = request.query_params.get('ai_operation')
    if operation_id and section == 'ai':
        from app.review.commands import WorkCommandError
        try:
            operation = await request.app.state.ai_network_commands.result(operation_id)
            context['ai_operation'] = operation
            if operation['action'] == 'fetch' and operation['status'] == 'COMPLETED':
                target = operation['target']
                provider = next((row for row in context['providers'] if row['provider_uuid'] == target['provider_uuid']), None)
                if provider and provider['revision'] == target['expected_revision'] and provider['keys_revision'] == target['keys_revision']:
                    context['selected_provider'] = provider
                    context['discovered'] = {'provider_id': provider['provider_id'], 'provider_uuid': provider['provider_uuid'],
                                             'operation_id': operation_id, 'names': operation['result']['names']}
        except WorkCommandError as exc:
            context['error'] = exc.public_message
    catalogue_draft = extra.get('ai_catalogue_draft')
    if catalogue_draft:
        action, values = catalogue_draft['action'], catalogue_draft['values']
        kind = 'provider' if action.endswith('-add') else action.split('-')[0]
        candidates = context.get('providers', []) if kind == 'provider' else [
            item for provider in context.get('providers', [])
            for item in provider['api_keys' if kind == 'key' else 'models']]
        current = next((item for item in candidates
                        if item[kind + '_uuid'] == values.get(kind + '_uuid')), None)
        context['ai_catalogue_current'] = current
        context['ai_catalogue_revision'] = (
            current['keys_revision' if action == 'keys-add' else 'models_revision']
            if current and kind == 'provider' else current['revision'] if current else None)
        if values.get('discovery_operation_id'):
            from app.review.commands import WorkCommandError
            try:
                operation = await request.app.state.ai_network_commands.result(values['discovery_operation_id'])
                target = operation['target']
                context['ai_catalogue_discovery_stale'] = not (
                    current and operation['action'] == 'fetch' and operation['status'] == 'COMPLETED'
                    and current['provider_uuid'] == target['provider_uuid']
                    and current['revision'] == target['expected_revision'] and current['keys_revision'] == target['keys_revision'])
            except WorkCommandError:
                context['ai_catalogue_discovery_stale'] = True
    chain_draft = extra.get('ai_chain_draft')
    if chain_draft:
        candidates = [*context.get('selectable_models', []),
                      *context.get('ai', {}).get('selectable_models', []),
                      *context.get('candidate_ai', {}).get('selectable_models', [])]
        context['ai_chain_model'] = next((row for row in candidates
            if row['model_uuid'] == chain_draft['values'].get('model_uuid')), None)
    provider_draft = extra.get('ai_provider_draft')
    if provider_draft and provider_draft['values'].get('provider_id'):
        values = provider_draft['values']
        current = next((row for row in context.get('providers', [])
                        if row['provider_uuid'] == values.get('provider_uuid')), None)
        context['ai_provider_current'] = current
        # A deleted/replaced provider remains a view-only original draft.
        context['selected_provider'] = current or {
                'provider_id': values['provider_id'], 'provider_uuid': values.get('provider_uuid', ''),
                'revision': values.get('expected_revision', ''), 'name': values.get('name', '原供应商'),
                'base_url': '', 'code': '', 'code_label': '', 'enabled': False, 'draft_only': True,
                'enablement': {}, 'api_keys': [], 'models': [],
        }
    profile_draft = extra.get('profile_draft')
    if profile_draft and not any(profile['name'] == profile_draft['name'] for profile in context.get('profiles', [])):
        # Render the rejected draft even when its stored profile no longer exists.
        # This view-only row cannot adopt a replacement identity or confirm a revision.
        values = profile_draft['values']
        context['profiles'] = [*context.get('profiles', []), {
            'name': profile_draft['name'], 'profile_id': values.get('profile_uuid', ''),
            'profile_uuid': values.get('profile_uuid', ''), 'revision': values.get('expected_revision', ''),
            'kind': 'CLI', 'backend': '', 'supported_formats': [], 'draft_only': True,
        }]
    rule_draft = extra.get('rule_draft')
    if rule_draft:
        values = rule_draft['values']
        context['rule_current'] = next((rule for rule in context.get('rules' if section == 'auto-approval' else 'path_rules', [])
                                        if rule['rule_uuid'] == values.get('rule_uuid')), None)
        if rule_draft['action'] == 'save':
            context['edit_rule'] = values
            context['edit_rule_id'] = values.get('rule_id')
    draft = extra.get('settings_draft')

    def preference_value(scope, name, current, *, check=False, action=None):
        if draft and draft['scope'] == scope and (action is None or draft['action'] == action):
            value = draft['values'].get(name, False if check else '')
            if check:
                return value is True or value == 'on'
            return value
        return current

    context['preference_value'] = preference_value

    def provider_draft_value(provider_id, name, current, *, check=False):
        if provider_draft and provider_draft['action'] == 'save' and str(
                provider_draft['values'].get('provider_id') or '') == str(provider_id or ''):
            value = provider_draft['values'].get(name, False if check else '')
            return checkbox(value) if check else value
        return current

    context['provider_draft_value'] = provider_draft_value
    return deps.templates(request).TemplateResponse(
        request=request,
        name="settings.html",
        context=context,
        status_code=status_code,
    )


def settings_redirect(request: Request, section: str) -> RedirectResponse:
    """See-other back to the tab a save was submitted from.

    Every settings POST keeps its own path -- an operator's bookmarked form
    action still works -- and lands the browser back on the tab that owns the
    form, so a save never navigates away from what was being edited.
    """
    return RedirectResponse(
        request.url_for("settings_section", section=section).path,
        status_code=303,
    )
