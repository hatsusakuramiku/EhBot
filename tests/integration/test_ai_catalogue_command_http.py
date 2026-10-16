"""Catalogue CAS, secret-safe drafts and fixed replay through all transports."""
from html import unescape
from functools import partial
from urllib.parse import urlsplit
import re

from fastapi.testclient import TestClient
import pytest

from app.main import create_app
from tests.ai_commands import observed_catalogue, observed_provider
from tests.integration.test_manual_add import _settings, _authenticate
from tests.integration.test_realtime import hello, call
from tests.integration.test_settings_command_http import fields
from tests.integration.test_work_enqueue_http import stop_workers
from tests.integration.test_work_review_http import Forms


def seed(client):
    csrf = fields(client, 'ai', '/settings/ai/providers')['csrf_token']
    headers = {'X-CSRF-Token': csrf}
    response = client.post('/api/v1/settings/ai/providers', headers=headers, json={
        **observed_provider(client), 'name': 'fixture', 'base_url': 'https://fixture.invalid/v1'})
    assert response.status_code == 200, response.text
    provider = response.json()
    database = client.app.state.database
    key = client.portal.call(partial(database.add_ai_provider_key, provider['provider_id'], label='', cipher='fixture-envelope'))
    model = client.portal.call(database.add_ai_provider_model, provider['provider_id'], 'fixture-model')
    return headers, provider['provider_id'], key.key_id, model.model_id


CASES = [
    ('keys-add', 'POST', 'providers/{provider}/keys', 'providers/{provider}/keys', {'api_keys': 'fixture-secret'}),
    ('models-add', 'POST', 'providers/{provider}/models', 'providers/{provider}/models', {'model_name': ['first', 'second']}),
    ('key-toggle', 'PUT', 'keys/{key}/toggle', 'keys/{key}/toggle', {'enabled': False}),
    ('key-reset', 'POST', 'keys/{key}/reset', 'keys/{key}/reset', {}),
    ('key-delete', 'DELETE', 'keys/{key}', 'keys/{key}/delete', {}),
    ('model-toggle', 'PUT', 'models/{model}/toggle', 'models/{model}/toggle', {'enabled': False}),
    ('model-params', 'PUT', 'models/{model}/params', 'models/{model}/params', {'params': '{"max_tokens":64}'}),
    ('model-delete', 'DELETE', 'models/{model}', 'models/{model}/delete', {}),
]


@pytest.mark.parametrize('action,method,api,native,values', CASES)
def test_rendered_native_forms_carry_observed_conditions(tmp_path, action, method, api, native, values):
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False) as client:
        _authenticate(client, settings)
        stop_workers(client)
        headers, provider, key, model = seed(client)
        if action == 'key-reset':
            client.portal.call(partial(client.app.state.database.record_ai_key_failure, key, cooldown_minutes=10))
        native_path = '/settings/ai/' + native.format(provider=provider, key=key, model=model)
        page = client.get('/settings/ai')
        assert page.status_code == 200
        body = next(row['fields'] for row in Forms(page.text).forms if urlsplit(row['action']).path == native_path)
        target = provider if action.endswith('-add') else key if action.startswith('key-') else model
        observed = observed_catalogue(client, action, target)
        for name, value in observed.items():
            if name != 'idempotency_key':
                assert body[name] == str(value)
        assert body['idempotency_key']
        response = client.post(native_path, data={**body, **values})
        assert response.status_code == 303, response.text


@pytest.mark.parametrize('action,method,api,native,values', CASES)
def test_same_receipt_http_ws_native_and_required_preconditions(tmp_path, action, method, api, native, values):
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False) as client:
        _authenticate(client, settings)
        stop_workers(client)
        headers, provider, key, model = seed(client)
        params = dict(provider=provider, key=key, model=model)
        path = '/api/v1/settings/ai/' + api.format(**params)
        native_path = '/settings/ai/' + native.format(**params)
        target = provider if action.endswith('-add') else key if action.startswith('key-') else model
        assert client.request(method, path, headers=headers, json=values).status_code == 428
        body = {**observed_catalogue(client, action, target), **values}
        first = client.request(method, path, headers=headers, json=body)
        assert first.status_code == 200, first.text
        assert 'fixture-secret' not in first.text and 'params' not in first.json()
        with client.websocket_connect('/ws/ui', headers={'Origin': 'http://testserver'}) as ws:
            ready = hello(client, ws)
            name, entry = next((name, entry) for name, entry in ready['commands'].items()
                               if entry['path'] == '/api/v1/settings/ai/' + api.replace('{provider}', '{provider_id}').replace('{key}', '{key_id}').replace('{model}', '{model_id}')
                               and entry['method'] == method)
            replay, _ = call(ws, name, {'body': body, 'params': {entry['params'][0]: target}})
            assert replay['type'] == 'command.result', replay
            assert replay['data'] == first.json()
        native_body = {**body, 'csrf_token': headers['X-CSRF-Token']}
        if 'enabled' in body:
            native_body['enabled'] = 'on' if body['enabled'] else 'off'
        accepted = client.post(native_path, data=native_body)
        assert accepted.status_code == 303, accepted.text
        assert 'provider_uuid=' + first.json()['provider_uuid'] in accepted.headers['location']


@pytest.mark.parametrize('action', ['keys-add', 'models-add', 'model-params', 'key-reset'])
def test_native_conflict_draft_confirmation_and_safe_replay(tmp_path, action):
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False) as client:
        _authenticate(client, settings)
        stop_workers(client)
        headers, provider, key, model = seed(client)
        target = provider if action.endswith('-add') else key if action == 'key-reset' else model
        _, method, api, native, _ = next(row for row in CASES if row[0] == action)
        params = dict(provider=provider, key=key, model=model)
        path = '/api/v1/settings/ai/' + api.format(**params)
        native_path = '/settings/ai/' + native.format(**params)
        body = {**observed_catalogue(client, action, target), 'csrf_token': headers['X-CSRF-Token']}
        values = {'api_keys': 'fixture-secret'} if action == 'keys-add' else (
            {'model_name': ['draft-a', 'draft-b']} if action == 'models-add' else {'params': '{"max_tokens":32}'} if action == 'model-params' else {})
        if action == 'key-reset':
            async def cool():
                await client.app.state.database.record_ai_key_failure(key, cooldown_minutes=10)
            client.portal.call(cool)
        else:
            winner = client.request(method, path, headers=headers,
                                    json={**body, **values, 'idempotency_key': 'winner'})
            assert winner.status_code == 200, winner.text
        rejected = client.post(native_path, data={**body, **values})
        assert rejected.status_code == 409, rejected.text
        draft = next(row['fields'] for row in Forms(rejected.text).forms
                     if row['fields'].get('idempotency_key') == body['idempotency_key'])
        identity = 'provider_uuid' if action.endswith('-add') else 'key_uuid' if action == 'key-reset' else 'model_uuid'
        assert draft[identity] == body[identity] and draft['expected_revision'] == str(body['expected_revision'])
        assert 'fixture-secret' not in rejected.text
        if action == 'models-add':
            assert 'value="draft-a"' in rejected.text and 'value="draft-b"' in rejected.text
        if action == 'model-params':
            assert '{&#34;max_tokens&#34;:32}' in rejected.text
        confirmation = unescape(re.search(r'name="confirmed_command" value="([^"]+)"', rejected.text)[1])
        for _ in range(2):
            response = client.post(native_path, data={**body, **values, 'confirmed_command': confirmation})
            assert response.status_code == 303, response.text
        if action == 'key-reset':
            client.portal.call(cool)
            assert client.post(native_path, data={**body, **values, 'confirmed_command': confirmation}).status_code == 303
            assert client.get('/api/v1/settings/ai').json()['providers'][0]['api_keys'][0]['cooldown_until']


@pytest.mark.parametrize('kind', ['key', 'model', 'provider'])
def test_deleted_and_reused_identity_keeps_native_draft(tmp_path, kind):
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False) as client:
        _authenticate(client, settings)
        stop_workers(client)
        headers, provider, key, model = seed(client)
        database = client.app.state.database
        action = {'key': 'key-toggle', 'model': 'model-params', 'provider': 'keys-add'}[kind]
        target = {'key': key, 'model': model, 'provider': provider}[kind]
        body = {**observed_catalogue(client, action, target), 'csrf_token': headers['X-CSRF-Token'],
                'enabled': 'off', 'params': '{"max_tokens":32}', 'api_keys': 'fixture-secret'}
        if kind == 'key':
            client.portal.call(database.delete_ai_provider_key, key)
            replacement = client.portal.call(partial(database.add_ai_provider_key, provider, label='', cipher='replacement-envelope'))
            assert replacement.key_id == key
            path = f'/settings/ai/keys/{key}/toggle'
        elif kind == 'model':
            client.portal.call(database.delete_ai_provider_model, model)
            replacement = client.portal.call(database.add_ai_provider_model, provider, 'replacement')
            assert replacement.model_id == model
            path = f'/settings/ai/models/{model}/params'
        else:
            client.portal.call(database.delete_ai_provider, provider)
            response = client.post('/api/v1/settings/ai/providers', headers=headers, json={
                **observed_provider(client), 'name': 'replacement', 'base_url': 'https://fixture.invalid/v1'})
            assert response.status_code == 200 and response.json()['provider_id'] == provider
            path = f'/settings/ai/providers/{provider}/keys'
        rejected = client.post(path, data=body)
        assert rejected.status_code == 410, rejected.text
        draft = next(row['fields'] for row in Forms(rejected.text).forms
                     if row['fields'].get('idempotency_key') == body['idempotency_key'])
        assert draft[kind + '_uuid'] == body[kind + '_uuid']
        assert 'confirmed_command' not in rejected.text and 'fixture-secret' not in rejected.text
