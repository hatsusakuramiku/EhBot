"""Versioned task URLs reuse durable v1 command outcomes across transports."""
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.integration.test_api_domains import seed, seed_downloaded, csrf_token
from tests.integration.test_api_v1 import log_in, make_settings
from tests.integration.test_realtime import hello, call
from tests.integration.test_work_enqueue_http import stop_workers
from tests.work_commands import observed_review


@pytest.fixture
def task_client(tmp_path):
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False) as client:
        stop_workers(client)
        seed(settings, [(1, 'APPROVED', 'V2 fixture')])
        log_in(client, settings)
        yield client, {'X-CSRF-Token': csrf_token(client)}, settings


def enqueue(client, headers, **values):
    response = client.post('/api/v2/works/1/downloads', headers=headers,
        json={**observed_review(client), 'provider': 'EXHENTAI', **values})
    assert response.status_code == 200, response.text
    return response.json()


def conditions(task, key=None):
    return {'expected_revision': task['revision'], 'idempotency_key': key or uuid4().hex}


def test_capabilities_directory_and_account_alias_do_not_expose_secrets(task_client):
    client, headers, _ = task_client
    response = client.get('/api/v2/capabilities')
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    capability = response.json()
    assert capability['tasks']['pause_running'] is False
    assert capability['administrator']['password_login_requires_username'] is True
    assert capability['realtime']['accepted_frame_is_durable'] is False
    sources = {row['provider']: row for row in capability['sources']}
    assert sources['TELEGRAM']['configured'] is False
    assert sources['EXHENTAI']['available'] is False
    assert sources['EH_TORRENT']['configured'] is False
    assert sources['TELEGRAPH']['supported'] is True
    assert client.get('/api/v2/accounts').json() == client.get('/api/v1/accounts').json()
    with client.websocket_connect('/ws/ui', headers={'Origin': 'http://testserver'}) as ws:
        ready = hello(client, ws)
        assert ready['commands']['v2.downloads.cancel']['encoding'] == 'json'
        assert all(not row['path'].endswith('/reveal') for row in ready['commands'].values())
    client.cookies.clear()
    assert client.get('/api/v2/capabilities').status_code == 401


def test_v2_v1_ws_share_receipts_and_task_reads(task_client):
    client, headers, _ = task_client
    work = observed_review(client)
    response = client.post('/api/v2/works/1/downloads', headers={**headers, 'Idempotency-Key': work['idempotency_key']},
                           json={key: value for key, value in {**work, 'provider': 'EXHENTAI'}.items() if key != 'idempotency_key'})
    assert response.status_code == 200, response.text
    task = response.json()
    assert client.post('/api/v1/works/1/sources/EXHENTAI', headers=headers, json=work).json() == task
    snapshot = client.get('/api/v2/downloads/' + task['task_id'])
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.json()['actions']['pause'] is True
    listing = client.get('/api/v2/downloads?limit=1').json()
    assert listing['total'] == 1 and listing['items'][0] == snapshot.json()
    assert 'details_json' not in snapshot.json()
    body = conditions(task)
    path = '/api/v2/downloads/' + task['task_id'] + '/pause'
    paused = client.post(path, headers=headers, json=body)
    assert paused.status_code == 200, paused.text
    assert client.post('/api/v1/jobs/' + str(task['job_id']) + '/pause', headers=headers,
        json={**body, 'task_id': task['task_id']}).json() == paused.json()
    with client.websocket_connect('/ws/ui', headers={'Origin': 'http://testserver'}) as ws:
        hello(client, ws)
        replay, _ = call(ws, 'v2.downloads.pause', {'params': {'task_id': task['task_id']}, 'body': body})
        assert replay['type'] == 'command.result' and replay['data'] == paused.json()
    assert client.post(path, headers=headers, json={**body, 'idempotency_key': 'stale'}).status_code == 409


def test_task_commands_require_conditions_and_keep_route_identity(task_client):
    client, headers, _ = task_client
    task = enqueue(client, headers)
    path = '/api/v2/downloads/' + task['task_id']
    assert client.post(path + '/cancel', headers=headers, json={}).status_code == 428
    assert client.post(path + '/cancel', headers=headers, json={'expected_revision': task['revision']}).status_code == 428
    assert client.post(path + '/cancel', json=conditions(task)).status_code == 403
    assert client.post(path + '/cancel', headers=headers,
                       json={**conditions(task), 'task_id': 'another-task'}).status_code == 409
    assert client.patch(path, headers=headers, json={**conditions(task), 'priority': 4}).status_code == 200
    fresh = client.get(path).json()
    mismatch = client.post(path + '/cancel', headers={**headers, 'Idempotency-Key': 'header'},
                           json={**conditions(fresh), 'idempotency_key': 'body'})
    assert mismatch.status_code == 409
    assert client.get(path).json()['status'] == 'PENDING'


def test_stop_acceptance_and_operation_remain_queryable_after_purge(task_client):
    client, headers, _ = task_client
    task = enqueue(client, headers)
    with client.app.state.database.connection() as connection:
        connection.execute("UPDATE download_jobs SET state = 'DOWNLOADING' WHERE id = ?", (task['job_id'],))
    path = '/api/v2/downloads/' + task['task_id']
    running = client.get(path).json()
    assert client.post(path + '/pause', headers=headers, json=conditions(running)).status_code == 400
    stopped = client.post(path + '/cancel', headers=headers, json=conditions(running))
    assert stopped.status_code == 202, stopped.text
    operation = stopped.json()['operation_id']
    result = client.get('/api/v2/operations/' + operation)
    assert result.status_code == 200 and result.json()['status'] == 'PENDING'
    purge_body = {**observed_review(client), 'delete_files': False}
    purge = client.post('/api/v2/works/1/purge', headers=headers, json=purge_body)
    assert purge.status_code == 202, purge.text
    assert client.get('/api/v2/operations/' + operation).status_code == 200
    assert client.get('/api/v2/operations/' + purge.json()['removal_id']).status_code == 200
    assert client.get(path).json()['status'] == 'REMOVED'
    assert client.post('/api/v2/works/1/purge', headers=headers, json=purge_body).json() == purge.json()


def test_pack_requires_exact_artifact_and_shares_v1_receipt(task_client):
    client, headers, settings = task_client
    seed_downloaded(settings, 1)
    work = observed_review(client)
    assert client.post('/api/v2/works/1/pack', headers=headers, json=work).status_code == 428
    with client.app.state.database.connection() as connection:
        artifact_id = connection.execute("SELECT artifact_id FROM artifact_versions WHERE artifact_type = 'ARCHIVE'").fetchone()[0]
    body = {**work, 'artifact_id': artifact_id}
    response = client.post('/api/v2/works/1/pack', headers=headers, json=body)
    assert response.status_code == 200, response.text
    replay = client.post('/api/v1/works/1/convert', headers=headers, json={**work, 'input_artifact_id': artifact_id})
    assert replay.json() == response.json()


def test_download_pack_intent_is_atomic_and_part_of_idempotency_digest(task_client):
    client, headers, _ = task_client
    body = {**observed_review(client), 'provider': 'EXHENTAI', 'pack_requested': False}
    first = client.post('/api/v2/works/1/downloads', headers=headers, json=body)
    assert first.status_code == 200, first.text
    task = first.json()
    assert task['pack_requested'] is False
    read = client.get('/api/v2/downloads/' + task['task_id']).json()
    assert read['pack_intent']['pack_requested'] == 0
    replay = client.post('/api/v1/works/1/sources/EXHENTAI', headers=headers, json=body)
    assert replay.json() == task
    changed = client.post('/api/v2/works/1/downloads', headers=headers, json={**body, 'pack_requested': True})
    assert changed.status_code == 409


def test_renamed_administrator_is_used_in_bearer_audit_and_cookie_audit(task_client):
    client, headers, settings = task_client
    from tests.integration.test_api_auth import NEW_PASSWORD, bearer
    tokens = client.post('/api/v1/auth/login', json={'username': 'admin', 'password': NEW_PASSWORD}).json()
    profile = client.get('/api/v1/auth/profile').json()
    assert client.patch('/api/v1/auth/profile', headers=headers, json={
        'username': 'Owner', 'current_password': NEW_PASSWORD, 'expected_revision': profile['revision']}).status_code == 200
    seed(settings, [(2, 'PENDING_REVIEW', 'Bearer audit'), (3, 'PENDING_REVIEW', 'Cookie audit')])
    body = observed_review(client, 2)
    client.cookies.clear()
    response = client.post('/api/v1/works/2/review', headers=bearer(tokens['access_token']), json={**body, 'action': 'reject'})
    assert response.status_code == 200, response.text
    client.post('/api/v1/auth/login', json={'username': 'Owner', 'password': NEW_PASSWORD})
    page = client.get('/login')
    logged = client.post('/login', data={'username': 'Owner', 'password': NEW_PASSWORD, 'csrf_token': page.context['csrf_token']})
    assert logged.status_code == 303
    csrf = csrf_token(client)
    body = observed_review(client, 3)
    response = client.post('/api/v1/works/3/review', headers={'X-CSRF-Token': csrf}, json={**body, 'action': 'reject'})
    assert response.status_code == 200, response.text
    with client.app.state.database.connection() as connection:
        assert connection.execute('SELECT operator_name FROM review_actions WHERE candidate_id IN (2, 3)').fetchall() == [('Owner',), ('Owner',)]


def test_header_only_settings_accounts_and_work_commands_share_body_receipts(task_client):
    client, headers, _ = task_client
    snapshot = client.get('/api/v1/settings/system').json()
    body = {'expected_revision': snapshot['settings_versions']['system'], 'timezone': 'UTC'}
    response = client.put('/api/v1/settings/system', headers={**headers, 'Idempotency-Key': 'settings-header'}, json=body)
    assert response.status_code == 200, response.text
    assert client.put('/api/v1/settings/system', headers=headers,
        json={**body, 'idempotency_key': 'settings-header'}).json() == response.json()
    body = {'action': 'create', 'kind': 'EH', 'label': 'Header fixture', 'fields': {}}
    response = client.post('/api/v2/accounts/commands', headers={**headers, 'Idempotency-Key': 'account-header'}, json=body)
    assert response.status_code == 200, response.text
    assert client.post('/api/v1/accounts/commands', headers=headers,
        json={**body, 'idempotency_key': 'account-header'}).json() == response.json()
    refused = client.post('/api/v2/accounts/commands', headers={**headers, 'Idempotency-Key': 'account-header'},
        json={**body, 'idempotency_key': 'different'})
    assert refused.status_code == 409


@pytest.mark.parametrize('native_client', ['mobile', 'desktop'])
def test_native_bearer_ws_can_read_and_control_v2_without_web_commands(task_client, native_client):
    from app.api.realtime import PROTOCOL
    from tests.integration.test_api_auth import NEW_PASSWORD, bearer
    client, headers, _ = task_client
    task = enqueue(client, headers)
    body = conditions(task)
    tokens = client.post('/api/v1/auth/login', json={'username': 'admin', 'password': NEW_PASSWORD}).json()
    authorization = bearer(tokens['access_token'])
    client.cookies.clear()
    with client.websocket_connect('/ws/ui', headers=authorization) as ws:
        ws.send_json({'type': 'hello', 'protocol': PROTOCOL, 'client': native_client})
        ready = ws.receive_json()
        assert ready['type'] == 'ready' and 'v2.downloads.pause' in ready['commands']
        assert all(name.startswith(('api.', 'v2.')) for name in ready['commands'])
        assert all(not row['path'].endswith('/reveal') for row in ready['commands'].values())
        snapshot, _ = call(ws, 'v2.capabilities')
        assert snapshot['type'] == 'command.result' and snapshot['data']['task_api'] == '/api/v2'
        paused, _ = call(ws, 'v2.downloads.pause', {'params': {'task_id': task['task_id']}, 'body': body}, request_id='pause')
        assert paused['type'] == 'command.result', paused
    replay = client.post('/api/v1/jobs/' + str(task['job_id']) + '/pause', headers=authorization,
        json={**body, 'task_id': task['task_id']})
    assert replay.status_code == 200 and replay.json() == paused['data']
