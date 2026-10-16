"""Credential writes through actual HTTP, WS and native forms."""
from html import unescape
import re

from fastapi.testclient import TestClient
import pytest

from app.main import create_app
from tests.credential_commands import observed_credentials
from tests.integration.test_manual_add import _settings, _authenticate
from tests.integration.test_realtime import hello, call
from tests.integration.test_settings_command_http import fields
from tests.integration.test_work_enqueue_http import stop_workers
from tests.integration.test_work_review_http import Forms


API = '/api/v1/settings/passwords'
CASES = [('add', '/archive-settings/passwords', {'name': 'fixture', 'password': 'fixture-only-password', 'enabled': True}),
         ('delete', '/archive-settings/passwords/{password_id}/delete', {}),
         ('api-key-generate', '/settings/api-keys/generate', {}),
         ('api-key-revoke', '/settings/api-keys/revoke', {})]


@pytest.fixture
def credentials(tmp_path):
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False) as client:
        _authenticate(client, settings)
        stop_workers(client)
        page = client.get('/settings/passwords')
        yield client, {'X-CSRF-Token': page.context['csrf_token']}, settings


@pytest.mark.parametrize('action,path,values', CASES)
def test_http_ws_native_share_receipt_and_require_observed_conditions(credentials, action, path, values):
    client, headers, _ = credentials
    password_id = None
    if action == 'api-key-revoke':
        seed = client.put(API, headers=headers, json={**observed_credentials(client, 'api-key-generate'), 'action': 'api-key-generate'})
        assert seed.status_code == 200
    if action == 'delete':
        seed = client.put(API, headers=headers, json={**observed_credentials(client, 'add'),
            'action': 'add', 'name': 'fixture', 'password': 'fixture-only-password'})
        assert seed.status_code == 200, seed.text
        password_id = seed.json()['password_id']
    path = path.format(password_id=password_id)
    form = fields(client, 'passwords', path)
    body = {**observed_credentials(client, action, password_id), **values, 'action': action}
    if password_id:
        body['password_id'] = password_id
    assert client.put(API, headers=headers, json={'action': action}).status_code == 428
    assert client.post(path, data={'csrf_token': headers['X-CSRF-Token'], **values}).status_code == 428
    for name in ('expected_revision', 'password_uuid'):
        if name in body:
            assert form[name] == str(body[name])
    first = client.put(API, headers=headers, json=body)
    assert first.status_code == 200, first.text
    assert first.headers['cache-control'] == 'no-store'
    safe = {key: value for key, value in first.json().items() if key != 'api_key_plaintext'}
    with client.websocket_connect('/ws/ui', headers={'Origin': 'http://testserver'}) as ws:
        ready = hello(client, ws)
        command = next(name for name, row in ready['commands'].items() if row['path'] == API and row['method'] == 'PUT')
        replay, broadcasts = call(ws, command, {'body': body})
        assert replay['type'] == 'command.result' and replay['data'] == safe
        assert 'fixture-only-password' not in str(broadcasts)
        if 'api_key_plaintext' in first.json():
            assert first.json()['api_key_plaintext'] not in str(broadcasts)
    native = client.post(path, data={**body, 'csrf_token': headers['X-CSRF-Token'],
                                    **({'enabled': 'on'} if action == 'add' else {})})
    assert native.status_code == (200 if action == 'api-key-generate' else 303), native.text
    if action == 'api-key-generate':
        assert first.json()['api_key_plaintext'] not in native.text
        assert '明文不会再次返回' in native.text
    assert client.put(API, headers=headers, json=body).json() == safe


def test_minted_plaintext_is_response_only_across_two_password_sessions(credentials):
    client, headers, settings = credentials
    # A genuine second login creates another device session, rather than copying cookies.
    access = client.post('/api/v1/auth/login', json={'username': 'admin', 'password': 'new-password-with-12-characters'})
    assert access.status_code == 200, access.text
    second_headers = {'Authorization': 'Bearer ' + access.json()['access_token']}
    form = fields(client, 'passwords', '/settings/api-keys/generate')
    response = client.post('/settings/api-keys/generate', data=form)
    assert response.status_code == 200, response.text
    token = re.search(r'ehk_[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', response.text).group()
    for read in (client.get('/settings/passwords?reveal=1'), client.get(API, headers=second_headers)):
        assert read.status_code == 200 and token not in read.text and token.split('.')[1] not in read.text
    replay = client.put(API, headers=second_headers, json={'action': 'api-key-generate',
        'expected_revision': int(form['expected_revision']), 'idempotency_key': form['idempotency_key']})
    assert replay.status_code == 200 and 'api_key_plaintext' not in replay.json()
    assert replay.json()['public_id'] == token.split('.')[0]
    # A leaked API Key cannot mint its own successor or revoke the administrator's key.
    for action in ('api-key-generate', 'api-key-revoke'):
        refused = client.put(API, headers={'Authorization': 'Bearer ' + token},
                             json={**observed_credentials(client, action), 'action': action})
        assert refused.status_code == 403 and refused.json()['error']['code'] == 'PASSWORD_LOGIN_REQUIRED'
    fresh = client.put(API, headers=second_headers, json={**observed_credentials(client, 'api-key-generate'), 'action': 'api-key-generate'})
    assert fresh.status_code == 200 and fresh.json()['api_key_plaintext'] != token


def test_conflict_retains_plain_values_and_explicit_confirmation_without_echoing_password(credentials):
    client, headers, _ = credentials
    form = fields(client, 'passwords', '/archive-settings/passwords')
    winner = client.put(API, headers=headers, json={**observed_credentials(client, 'add'),
        'action': 'add', 'name': 'winner', 'password': 'winner-only-password'})
    assert winner.status_code == 200
    response = client.post('/archive-settings/passwords', data={**form, 'name': 'draft',
        'password': 'browser-only-secret', 'priority': '9', 'enabled': 'on'})
    assert response.status_code == 409, response.text
    assert 'browser-only-secret' not in response.text
    retained = next(row['fields'] for row in Forms(response.text).forms if row['action'].endswith('/archive-settings/passwords'))
    assert 'name="name" value="draft"' in response.text and 'value="9"' in response.text
    assert retained.get('password', '') == ''
    assert retained['expected_revision'] == form['expected_revision'] and retained['idempotency_key'] == form['idempotency_key']
    confirmation = unescape(re.search(r'name="confirmed_command"\s+value="([^"]+)"', response.text).group(1))
    retried = client.post('/archive-settings/passwords', data={**retained, 'name': 'draft', 'priority': '9',
        'enabled': 'on', 'password': 'browser-only-secret', 'confirmed_command': confirmation})
    assert retried.status_code == 303, retried.text
    snapshot = client.get(API).json()
    assert {row['name'] for row in snapshot['passwords']} == {'winner', 'draft'}


def test_deleted_native_draft_never_adopts_reused_numeric_password_id(credentials):
    client, headers, _ = credentials
    added = client.put(API, headers=headers, json={**observed_credentials(client, 'add'),
        'action': 'add', 'name': 'original', 'password': 'fixture-only-password'}).json()
    path = '/archive-settings/passwords/' + str(added['password_id']) + '/delete'
    original = fields(client, 'passwords', path)
    body = {**observed_credentials(client, 'delete', added['password_id']), 'action': 'delete', 'password_id': added['password_id']}
    assert client.put(API, headers=headers, json=body).status_code == 200
    replacement = client.put(API, headers=headers, json={**observed_credentials(client, 'add'),
        'action': 'add', 'name': 'replacement', 'password': 'replacement-only-password'}).json()
    assert replacement['password_id'] == added['password_id'] and replacement['password_uuid'] != added['password_uuid']
    response = client.post(path, data=original)
    assert response.status_code == 410, response.text
    assert added['password_uuid'] in response.text and 'name="confirmed_command"' not in response.text
    assert client.get(API).json()['passwords'][0]['name'] == 'replacement'
