"""All scoped chain editors share CAS, fixed receipts and native conflict drafts."""
from html import unescape
import re
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
import pytest

from app.main import create_app
from tests.ai_commands import observed_chain
from tests.integration.test_ai_catalogue_command_http import seed
from tests.integration.test_manual_add import _settings, _authenticate
from tests.integration.test_realtime import hello, call
from tests.integration.test_work_enqueue_http import stop_workers
from tests.integration.test_work_review_http import Forms


SCOPES = [('default', 'ai'), ('archive_path', 'paths'), ('candidate_admission', 'parse')]


@pytest.mark.parametrize('scope,section', SCOPES)
@pytest.mark.parametrize('action', ['primary', 'append', 'shift', 'remove'])
def test_all_chain_actions_share_fixed_receipt_http_ws_and_native(tmp_path, scope, section, action):
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False) as client:
        _authenticate(client, settings)
        stop_workers(client)
        headers, _, _, model_id = seed(client)
        if action in ('shift', 'remove'):
            client.portal.call(client.app.state.database.save_ai_model_chain, [model_id], scope)
        path = '/api/v1/settings/ai/chain/' + scope
        assert client.put(path, headers=headers, json={'action': action, 'model_id': model_id}).status_code == 428
        body = {**observed_chain(client, scope, model_id), 'model_id': model_id, 'action': action, 'delta': -1}
        first = client.put(path, headers=headers, json=body)
        assert first.status_code == 200, first.text
        assert first.json()['revision'] > body['expected_revision'] and 'default_chain' not in first.json()
        with client.websocket_connect('/ws/ui', headers={'Origin': 'http://testserver'}) as ws:
            ready = hello(client, ws)
            name = next(name for name, entry in ready['commands'].items() if entry['path'] == '/api/v1/settings/ai/chain/{scope}')
            replay, _ = call(ws, name, {'params': {'scope': scope}, 'body': body})
            assert replay['type'] == 'command.result', replay
            assert replay['data'] == first.json()
        response = client.post(f'/settings/{section}/chain/{action}', data={**body, 'csrf_token': headers['X-CSRF-Token']})
        assert response.status_code == 303, response.text
        assert client.put(path, headers=headers, json=body).json() == first.json()


@pytest.mark.parametrize('scope,section', SCOPES)
def test_real_append_form_identity_and_conflict_confirmation(tmp_path, scope, section):
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False) as client:
        _authenticate(client, settings)
        stop_workers(client)
        headers, provider_id, _, model_id = seed(client)
        database = client.app.state.database
        other = client.portal.call(database.add_ai_provider_model, provider_id, 'other-model')
        path = f'/settings/{section}/chain/append'
        page = client.get('/settings/' + section)
        assert page.status_code == 200, page.text
        original = next(row['fields'] for row in Forms(page.text).forms if urlsplit(row['action']).path == path)
        identity = observed_chain(client, scope, model_id)
        choice = str(model_id) + ':' + identity['model_uuid']
        assert 'name="model_identity"' in page.text and 'value="' + choice + '"' in page.text
        assert original['expected_revision'] == str(identity['expected_revision'])
        body = dict(original, model_identity=choice)
        api = '/api/v1/settings/ai/chain/' + scope
        winner = client.put(api, headers=headers, json={**observed_chain(client, scope, other.model_id),
                           'action': 'primary', 'model_id': other.model_id})
        assert winner.status_code == 200, winner.text
        conflict = client.post(path, data=body)
        assert conflict.status_code == 409, conflict.text
        draft = next(row['fields'] for row in Forms(conflict.text).forms if row['fields'].get('idempotency_key') == original['idempotency_key'])
        assert draft['expected_revision'] == original['expected_revision'] and draft['model_uuid'] == identity['model_uuid']
        confirmation = unescape(re.search(r'name="confirmed_command" value="([^"]+)"', conflict.text)[1])
        for _ in range(2):
            response = client.post(path, data={**draft, 'confirmed_command': confirmation})
            assert response.status_code == 303, response.text
        assert [entry.model.model_id for entry in client.portal.call(database.list_ai_model_chain, scope)] == [other.model_id, model_id]
        # Primary and remove forms also carry their own original scope revision and identity.
        page = client.get('/settings/' + section)
        for row in Forms(page.text).forms:
            if urlsplit(row['action']).path == f'/settings/{section}/chain/remove':
                assert row['fields']['model_uuid'] and row['fields']['expected_revision'] != original['expected_revision']


@pytest.mark.parametrize('scope,section', SCOPES)
def test_deleted_model_draft_cannot_confirm_replacement(tmp_path, scope, section):
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False) as client:
        _authenticate(client, settings)
        stop_workers(client)
        headers, provider_id, _, model_id = seed(client)
        body = {**observed_chain(client, scope, model_id), 'model_id': model_id, 'model_label': 'original draft',
                'csrf_token': headers['X-CSRF-Token']}
        database = client.app.state.database
        client.portal.call(database.delete_ai_provider_model, model_id)
        replacement = client.portal.call(database.add_ai_provider_model, provider_id, 'replacement')
        assert replacement.model_id == model_id
        response = client.post(f'/settings/{section}/chain/primary', data=body)
        assert response.status_code == 410, response.text
        draft = next(row['fields'] for row in Forms(response.text).forms if row['fields'].get('idempotency_key') == body['idempotency_key'])
        assert draft['model_uuid'] == body['model_uuid'] and draft['expected_revision'] == str(body['expected_revision'])
        assert 'original draft' in response.text and 'confirmed_command' not in response.text
