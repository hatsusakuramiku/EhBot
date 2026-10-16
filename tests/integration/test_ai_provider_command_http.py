"""One provider command crosses JSON, WS and native drafts without rewriting."""
from html import unescape
import re

from fastapi.testclient import TestClient
import pytest

from app.main import create_app
from tests.ai_commands import observed_provider, observed_catalogue
from tests.integration.test_manual_add import _settings, _authenticate
from tests.integration.test_realtime import hello, call
from tests.integration.test_settings_command_http import fields
from tests.integration.test_work_enqueue_http import stop_workers
from tests.integration.test_work_review_http import Forms


def test_shared_provider_receipt_and_native_confirmation(tmp_path):
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False) as client:
        _authenticate(client, settings)
        stop_workers(client)
        form = fields(client, 'ai', '/settings/ai/providers')
        headers = {'X-CSRF-Token': form['csrf_token']}
        path = '/api/v1/settings/ai/providers'
        assert client.post(path, headers=headers, json={'name': 'fixture'}).status_code == 428
        body = {**form, 'expected_revision': int(form['expected_revision']), 'name': 'fixture',
                'base_url': 'https://fixture.invalid/v1', 'enabled': True,
                'timeout_seconds': 30, 'max_retries': 1}
        first = client.post(path, headers=headers, json=body)
        assert first.status_code == 200, first.text
        receipt = first.json()
        assert 'base_url' not in receipt
        with client.websocket_connect('/ws/ui', headers={'Origin': 'http://testserver'}) as ws:
            ready = hello(client, ws)
            name = next(name for name, entry in ready['commands'].items()
                        if entry['path'] == path and entry['method'] == 'POST')
            replay, _ = call(ws, name, {'body': body})
            assert replay['type'] == 'command.result' and replay['data'] == receipt
        native = {**body, 'enabled': 'on', 'timeout_seconds': '30', 'max_retries': '1'}
        accepted = client.post('/settings/ai/providers', data=native)
        assert accepted.status_code == 303, accepted.text
        assert 'provider_uuid=' + receipt['provider_uuid'] in accepted.headers['location']
        provider_id = receipt['provider_id']
        edit = {**body, **observed_provider(client, provider_id), 'provider_id': provider_id}
        assert client.post(path, headers=headers, json={**edit, 'name': 'winner'}).status_code == 200
        draft = {**edit, 'name': 'draft', 'idempotency_key': 'loser', 'csrf_token': form['csrf_token']}
        stale = client.post('/settings/ai/providers', data=draft)
        assert stale.status_code == 409, stale.text
        saved_form = next(row['fields'] for row in Forms(stale.text).forms
                          if row['fields'].get('provider_id') == str(provider_id))
        assert 'name="name" value="draft"' in stale.text
        assert saved_form['provider_uuid'] == receipt['provider_uuid']
        assert saved_form['expected_revision'] == str(edit['expected_revision'])
        assert saved_form['idempotency_key'] == 'loser' and 'data-dirty="true"' in stale.text
        confirm = unescape(re.search(r'name="confirmed_command" value="([^"]+)"', stale.text)[1])
        for _ in range(2):
            accepted = client.post('/settings/ai/providers', data={**draft, 'confirmed_command': confirm})
            assert accepted.status_code == 303, accepted.text
        current = client.get('/api/v1/settings/ai').json()['providers'][0]
        assert current['name'] == 'draft' and current['revision'] == edit['expected_revision'] + 2
        assert client.post(path, headers=headers, json=body).json() == receipt
        assert client.get('/api/v1/settings/ai').json()['providers'][0]['name'] == 'draft'

        deletion = {**observed_provider(client, provider_id), 'csrf_token': form['csrf_token']}
        # Another endpoint adds a child without changing the endpoint editor's version.
        assert client.post(f'{path}/{provider_id}/models', headers=headers,
                           json={**observed_catalogue(client, 'models-add', provider_id), 'model_name': ['new-child']}).status_code == 200
        delete_url = f'/settings/ai/providers/{provider_id}/delete'
        refused = client.post(delete_url, data=deletion)
        assert refused.status_code == 409
        confirm = unescape(re.search(r'name="confirmed_command" value="([^"]+)"', refused.text)[1])
        for _ in range(2):
            assert client.post(delete_url, data={**deletion, 'confirmed_command': confirm}).status_code == 303
        assert client.get('/api/v1/settings/ai').json()['providers'] == []


@pytest.mark.parametrize('replacement', [False, True])
def test_deleted_provider_draft_does_not_adopt_replacement(tmp_path, replacement):
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False) as client:
        _authenticate(client, settings)
        stop_workers(client)
        form = fields(client, 'ai', '/settings/ai/providers')
        headers = {'X-CSRF-Token': form['csrf_token']}
        body = {**observed_provider(client), 'name': 'original', 'base_url': 'https://fixture.invalid/v1'}
        original = client.post('/api/v1/settings/ai/providers', json=body, headers=headers).json()
        provider_id = original['provider_id']
        draft = {**body, **observed_provider(client, provider_id), 'provider_id': provider_id,
                 'name': 'retained draft', 'csrf_token': form['csrf_token']}
        removed = client.request('DELETE', f'/api/v1/settings/ai/providers/{provider_id}', headers=headers,
                                json=observed_provider(client, provider_id))
        assert removed.status_code == 200
        if replacement:
            newer = client.post('/api/v1/settings/ai/providers', headers=headers,
                                json={**body, **observed_provider(client)}).json()
            assert newer['provider_id'] == provider_id and newer['provider_uuid'] != original['provider_uuid']
        for _ in range(2):
            failed = client.post('/settings/ai/providers', data=draft)
            assert failed.status_code == 410, failed.text
            saved_form = next(row['fields'] for row in Forms(failed.text).forms
                             if row['fields'].get('provider_id') == str(provider_id))
            assert saved_form['provider_uuid'] == original['provider_uuid']
            assert 'name="name" value="retained draft"' in failed.text and 'confirmed_command' not in failed.text
        old_link = client.get(f'/settings/ai?provider={provider_id}&provider_uuid={original["provider_uuid"]}')
        assert old_link.context['selected_provider'] is None
