"""Public path AI edits share receipts and preserve plain-form conflicts."""
from html import unescape
import re

from fastapi.testclient import TestClient
import pytest

from app.main import create_app
from tests.integration.test_manual_add import _settings, _authenticate
from tests.integration.test_path_ai_commands import PANEL
from tests.integration.test_realtime import hello, call
from tests.integration.test_settings_command_http import fields
from tests.integration.test_work_enqueue_http import stop_workers
from tests.integration.test_work_review_http import Forms


ENDPOINTS = [
    ('paths', '/settings/paths/ai', '/archive-settings/paths/ai', PANEL),
    ('paths', '/settings/paths/ai/prompt', '/archive-settings/paths/ai/prompt-default', {'prompt': ''}),
    ('paths', '/settings/paths/ai/models', '/archive-settings/paths/ai/models', {'model_source': 'custom'}),
    ('parse', '/settings/parse/model-source', '/settings/parse/model-source', {'model_source': 'custom'}),
]


def html_form(values):
    return {('ai_model_source' if key == 'model_source' else key): 'on' if value is True else value
            for key, value in values.items() if value is not False}


@pytest.mark.parametrize('section,api_path,web_path,values', ENDPOINTS)
def test_http_ws_native_form_share_fixed_receipt_and_require_preconditions(tmp_path, section, api_path, web_path, values):
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False) as client:
        _authenticate(client, settings)
        stop_workers(client)
        form = fields(client, section, web_path)
        headers = {'X-CSRF-Token': form['csrf_token']}
        assert client.put('/api/v1' + api_path, headers=headers, json=values).status_code == 428
        assert client.post(web_path, data={'csrf_token': form['csrf_token'], **html_form(values)}).status_code == 428
        body = {**values, 'expected_revision': int(form['expected_revision']), 'idempotency_key': form['idempotency_key']}
        first = client.put('/api/v1' + api_path, headers=headers, json=body)
        assert first.status_code == 200, first.text
        with client.websocket_connect('/ws/ui', headers={'Origin': 'http://testserver'}) as ws:
            ready = hello(client, ws)
            name = next(name for name, entry in ready['commands'].items() if entry['path'] == '/api/v1' + api_path)
            replay, _ = call(ws, name, {'body': body})
            assert replay['type'] == 'command.result' and replay['data'] == first.json()
        assert client.post(web_path, data={**form, **html_form(values)}).status_code == 303
        assert client.put('/api/v1' + api_path, headers=headers, json={**body, 'idempotency_key': 'stale'}).status_code == 409


@pytest.mark.parametrize('section,api_path,web_path,values', ENDPOINTS)
def test_native_conflict_retains_all_values_and_requires_explicit_confirmation(tmp_path, section, api_path, web_path, values):
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False) as client:
        _authenticate(client, settings)
        stop_workers(client)
        form = fields(client, section, web_path)
        headers = {'X-CSRF-Token': form['csrf_token']}
        winner = client.put('/api/v1' + api_path, headers=headers, json={**values,
            'expected_revision': int(form['expected_revision']), 'idempotency_key': 'winner'})
        assert winner.status_code == 200, winner.text
        response = client.post(web_path, data={**form, **html_form(values)})
        assert response.status_code == 409, response.text
        retained = next(row['fields'] for row in Forms(response.text).forms if row['action'].endswith(web_path))
        assert retained['expected_revision'] == form['expected_revision']
        assert retained['idempotency_key'] == form['idempotency_key']
        assert 'data-dirty="true"' in response.text
        if 'ai_prompt' in values:
            assert '>draft {{metadata}}</textarea>' in response.text
            assert 'value="31"' in response.text and 'value="3"' in response.text
            for name in ('ai_stream', 'ai_fallback_to_rules', 'ai_default_include_current'):
                assert re.search(r'name="' + name + r'"[^>]*checked', response.text)
            assert re.search(r'name="path_source"[^>]*value="ai"[^>]*checked', response.text)
        elif 'model_source' in values:
            assert re.search(r'name="ai_model_source"[^>]*value="custom"[^>]*checked', response.text)
        confirmation = unescape(re.search(r'name="confirmed_command" value="([^"]+)"', response.text)[1])
        for _ in range(2):
            accepted = client.post(web_path, data={**retained, **html_form(values), 'confirmed_command': confirmation})
            assert accepted.status_code == 303, accepted.text


def test_prompt_reset_uses_own_form_and_shared_panel_revision_without_erasing_unsubmitted_fields(tmp_path):
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False) as client:
        _authenticate(client, settings)
        stop_workers(client)
        panel_path = '/archive-settings/paths/ai'
        reset_path = panel_path + '/prompt-default'
        panel = fields(client, 'paths', panel_path)
        reset = fields(client, 'paths', reset_path)
        assert panel['expected_revision'] == reset['expected_revision']
        assert panel['idempotency_key'] != reset['idempotency_key']
        accepted = client.post(panel_path, data={**panel, **html_form(PANEL)})
        assert accepted.status_code == 303, accepted.text
        rejected = client.post(reset_path, data=reset)
        assert rejected.status_code == 409, rejected.text
        # A reset rejection owns no panel draft and must display the stored panel.
        assert '>draft {{metadata}}</textarea>' in rejected.text
        assert 'value="31"' in rejected.text and 'value="3"' in rejected.text
        assert 'formaction=' not in next(row for row in re.findall(r'<form.*?</form>', rejected.text, re.S)
                                         if 'data-settings-action="path-ai"' in row)


def test_series_refusal_keeps_checkbox_editable_and_does_not_partially_save(tmp_path):
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False) as client:
        _authenticate(client, settings)
        stop_workers(client)
        path = '/archive-settings/paths/ai'
        form = fields(client, 'paths', path)
        before = client.get('/api/v1/settings/paths').json()
        response = client.post(path, data={**form, **html_form({**PANEL, 'auto_series': True})})
        assert response.status_code == 400, response.text
        checkbox = re.search(r'<input[^>]*name="auto_series"[^>]*>', response.text)[0]
        assert 'checked' in checkbox and 'disabled' not in checkbox
        assert '>draft {{metadata}}</textarea>' in response.text
        assert client.get('/api/v1/settings/paths').json()['settings_versions'] == before['settings_versions']
