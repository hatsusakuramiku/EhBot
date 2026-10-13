"""Single administrator, independent devices and transactional conflict boundaries."""
import asyncio
import base64
import json
import sqlite3
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from starlette.websockets import WebSocketDisconnect

from app.admin_auth import AuthError
from app.api.deps import CSRF_HEADER
from app.db.database import Database
from app.main import create_app
from tests.integration.test_api_auth import (
    NEW_PASSWORD, ROTATED_PASSWORD, bearer, complete_first_run, make_settings,
)
from tests.integration.test_realtime import call, hello


def login(client, username='admin'):
    page = client.get('/login')
    return client.post('/login', data={
        'username': username, 'password': NEW_PASSWORD,
        'csrf_token': page.context['csrf_token'],
    }, follow_redirects=False)


def profile_write(client, username, revision):
    csrf = client.get('/settings/passwords').context['csrf_token']
    return client.patch('/api/v1/auth/profile', json={
        'username': username, 'current_password': NEW_PASSWORD, 'expected_revision': revision,
    }, headers={CSRF_HEADER: csrf})


def test_username_required_on_both_login_paths(tmp_path):
    settings = replace(make_settings(tmp_path), archive_toolchain_auto_install=False)
    with TestClient(create_app(settings)) as client:
        complete_first_run(client, settings)
        for username in (None, 'wrong-name'):
            page = client.get('/login')
            fields = {'password': NEW_PASSWORD}
            if username is not None:
                fields['username'] = username
            assert client.post('/login', data={**fields, 'csrf_token': page.context['csrf_token']}).status_code == 401
            response = client.post('/api/v1/auth/login', json=fields)
            assert response.status_code == 401
            assert response.json()['error']['code'] == 'AUTH_INVALID_CREDENTIALS'


def test_rename_conflict_preserves_sessions_and_survives_restart(tmp_path):
    settings = replace(make_settings(tmp_path), archive_toolchain_auto_install=False)
    app = create_app(settings)
    with TestClient(app) as a:
        b = TestClient(app)
        complete_first_run(a, settings)
        assert login(b).status_code == 303
        revision = a.get('/api/v1/auth/profile').json()['revision']
        tokens = b.post('/api/v1/auth/login', json={'username': 'admin', 'password': NEW_PASSWORD}).json()
        with b.websocket_connect('/ws/ui', headers={'Origin': 'http://testserver'}) as ws:
            assert hello(b, ws)['type'] == 'ready'
            renamed = profile_write(a, 'Owner', revision)
            assert renamed.status_code == 200
            assert b.get('/api/v1/auth/whoami').json()['username'] == 'Owner'
            assert b.get('/api/v1/auth/whoami', headers=bearer(tokens['access_token'])).json()['username'] == 'Owner'
            assert call(ws, 'api.list_candidates')[0]['type'] == 'command.result'
            assert profile_write(b, 'Stale', revision).status_code == 409
        page = b.get('/settings/passwords')
        refused = b.post('/administrator', data={
            'username': 'KeepDraft', 'current_password': NEW_PASSWORD,
            'expected_revision': revision, 'csrf_token': page.context['csrf_token'],
        })
        assert refused.status_code == 409
        assert 'value="KeepDraft"' in refused.text
        assert login(b).status_code == 401
        assert login(b, 'Owner').status_code == 303
        with app.state.database.connection() as conn:
            assert conn.execute('SELECT COUNT(*) FROM admin_users').fetchone()[0] == 1
    with TestClient(create_app(settings)) as restarted:
        assert login(restarted, 'Owner').status_code == 303
        assert restarted.get('/api/v1/auth/profile').json()['revision'] == revision + 1


def test_logout_is_device_local_and_revokes_cookie_replay_and_socket(tmp_path):
    settings = replace(make_settings(tmp_path), archive_toolchain_auto_install=False)
    app = create_app(settings)
    with TestClient(app) as a:
        b = TestClient(app)
        complete_first_run(a, settings)
        assert login(b).status_code == 303
        old_cookie = a.cookies.get('session')
        with a.websocket_connect('/ws/ui', headers={'Origin': 'http://testserver'}) as ws:
            assert hello(a, ws)['type'] == 'ready'
            csrf = a.get('/settings/passwords').context['csrf_token']
            assert a.post('/logout', data={'csrf_token': csrf}).status_code == 200
            ws.send_json({'type': 'command', 'name': 'api.list_candidates', 'request_id': 'out'})
            while True:
                frame = ws.receive_json()
                if frame['type'] == 'error':
                    break
            assert frame['error']['code'] == 'NOT_AUTHENTICATED'
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
        a.cookies.set('session', old_cookie)
        assert a.get('/api/v1/auth/profile').status_code == 401
        assert b.get('/api/v1/auth/profile').status_code == 200


def test_api_password_change_revokes_other_devices_but_keeps_initiator_and_key(tmp_path):
    settings = replace(make_settings(tmp_path), archive_toolchain_auto_install=False)
    app = create_app(settings)
    with TestClient(app) as a:
        b = TestClient(app)
        complete_first_run(a, settings)
        login(b)
        page = a.get('/settings/passwords')
        csrf = page.context['csrf_token']
        assert a.post('/settings/api-keys/generate', data={'csrf_token': csrf}).status_code == 200
        # The key is an independent row, never revoked by a password transaction.
        key = a.portal.call(app.state.database.active_api_key)
        assert key is not None
        tokens = a.post('/api/v1/auth/login', json={'username': 'admin', 'password': NEW_PASSWORD}).json()
        response = a.post('/api/v1/auth/change-password', json={
            'current_password': NEW_PASSWORD, 'new_password': ROTATED_PASSWORD,
            'confirmation': ROTATED_PASSWORD, 'expected_revision': page.context['administrator']['revision'],
        }, headers={CSRF_HEADER: csrf})
        assert response.status_code == 200
        assert response.json()['reauthenticate'] is False
        assert a.get('/api/v1/auth/profile').status_code == 200
        assert b.get('/api/v1/auth/profile').status_code == 401
        assert a.get('/api/v1/auth/profile', headers=bearer(tokens['access_token'])).status_code == 401
        assert a.portal.call(app.state.database.active_api_key) == key


def test_concurrent_http_refresh_has_only_one_successor(tmp_path):
    settings = replace(make_settings(tmp_path), archive_toolchain_auto_install=False)
    with TestClient(create_app(settings)) as client:
        complete_first_run(client, settings)
        tokens = client.post('/api/v1/auth/login', json={'username': 'admin', 'password': NEW_PASSWORD}).json()
        client.cookies.clear()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(client.post, '/api/v1/auth/refresh', json={'refresh_token': tokens['refresh_token']}) for _ in range(2)]
            responses = [f.result() for f in futures]
        assert sorted(r.status_code for r in responses) == [200, 401]
        with client.app.state.database.connection() as conn:
            assert conn.execute("SELECT COUNT(*) FROM api_credentials WHERE kind='refresh' AND revoked_at IS NULL").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_refresh_transaction_rollback_preserves_old_token(tmp_path):
    db = Database(tmp_path / 'db.sqlite')
    await db.initialize()
    await db.set_bootstrap_admin('admin', 'test-hash')
    await db.change_admin_password('admin', 'changed-test-hash')
    admin = await db.get_administrator()
    args = dict(label='test', family_id='device', access_ttl=60, refresh_ttl=600)
    await db.issue_credential_pair(admin, refresh=('old-refresh', 'hash-r'), access=('old-access', 'hash-a'), **args)
    previous = await db.get_api_credential('old-refresh')
    with pytest.raises(sqlite3.IntegrityError):
        await db.issue_credential_pair(admin, previous=previous, refresh=('new-refresh', 'hash-r2'), access=('old-access', 'collision'), **args)
    assert not (await db.get_api_credential('old-refresh')).is_revoked
    assert await db.get_api_credential('new-refresh') is None


@pytest.mark.asyncio
async def test_stale_login_and_revoked_inflight_edit_cannot_commit(tmp_path):
    db = Database(tmp_path / 'db.sqlite')
    await db.initialize()
    await db.set_bootstrap_admin('admin', 'test-hash')
    await db.change_admin_password('admin', 'changed-test-hash')
    admin = await db.get_administrator()
    session = await db.create_browser_session(admin)
    await db.revoke_browser_session(session)
    with pytest.raises(AuthError, match='会话已结束'):
        await db.update_administrator(admin, username='Blocked', actor_session_id=session)
    winner = await db.update_administrator(admin, username='Owner')
    with pytest.raises(AuthError):
        await db.create_browser_session(admin)
    with pytest.raises(AuthError):
        await db.issue_credential_pair(admin, refresh=('r', 'h'), access=('a', 'h'), label='', family_id='f', access_ttl=60, refresh_ttl=600)
    results = await asyncio.gather(
        db.update_administrator(winner, username='A'),
        db.update_administrator(winner, username='B'), return_exceptions=True,
    )
    assert sum(isinstance(r, AuthError) for r in results) == 1


def old_database(path, *, duplicate=False):
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)')
        for migration in sorted(Path('app/db/migrations').glob('*.sql')):
            version = int(migration.name.split('_')[0])
            if version > 25:
                break
            conn.executescript(migration.read_text(encoding='utf-8'))
            conn.execute('INSERT INTO schema_migrations VALUES (?, CURRENT_TIMESTAMP)', (version,))
        conn.execute("INSERT INTO admin_users (username, password_hash, password_changed) VALUES ('Owner', 'test-hash', 1)")
        if duplicate:
            conn.execute("INSERT INTO admin_users (username, password_hash) VALUES ('unexpected-second', 'test-hash')")


@pytest.mark.asyncio
async def test_migration_026_preserves_identity_and_is_repeatable(tmp_path):
    path = tmp_path / 'old.sqlite'
    old_database(path)
    db = Database(path)
    await db.initialize()
    await db.initialize()
    admin = await db.get_administrator()
    assert (admin.id, admin.username, admin.revision, admin.auth_epoch) == (1, 'Owner', 0, 0)
    with db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0] == 26
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO admin_users (username, password_hash) VALUES ('second', 'test-hash')")


@pytest.mark.asyncio
async def test_migration_rejects_abnormal_multiadmin_without_partial_schema(tmp_path):
    path = tmp_path / 'old.sqlite'
    old_database(path, duplicate=True)
    db = Database(path)
    with pytest.raises(sqlite3.IntegrityError):
        await db.initialize()
    with db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM admin_users').fetchone()[0] == 2
        assert conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == 25
        assert 'revision' not in {row[1] for row in conn.execute('PRAGMA table_info(admin_users)')}


def test_initial_rename_retains_one_identity_on_bootstrap_restart(tmp_path):
    settings = replace(make_settings(tmp_path), archive_toolchain_auto_install=False)
    with TestClient(create_app(settings)) as client:
        password = (settings.data_path / 'bootstrap_admin_password').read_text(encoding='utf-8')
        csrf = client.get('/login').context['csrf_token']
        assert client.post('/login', data={'username': 'admin', 'password': password, 'csrf_token': csrf}, follow_redirects=False).status_code == 303
        page = client.get('/settings/passwords')
        response = client.patch('/api/v1/auth/profile', json={
            'username': 'InitialOwner', 'current_password': password,
            'expected_revision': page.context['administrator']['revision'],
        }, headers={CSRF_HEADER: page.context['csrf_token']})
        assert response.status_code == 200
    with TestClient(create_app(settings)) as client:
        password = (settings.data_path / 'bootstrap_admin_password').read_text(encoding='utf-8')
        csrf = client.get('/login').context['csrf_token']
        assert client.post('/login', data={'username': 'InitialOwner', 'password': password, 'csrf_token': csrf}, follow_redirects=False).status_code == 303
        assert client.get('/api/v1/auth/profile').json()['username'] == 'InitialOwner'


def test_profile_revision_validation_csrf_and_bearer_password_reauthentication(tmp_path):
    settings = replace(make_settings(tmp_path), archive_toolchain_auto_install=False)
    with TestClient(create_app(settings)) as client:
        complete_first_run(client, settings)
        fields = {'username': 'Owner', 'current_password': NEW_PASSWORD}
        assert client.patch('/api/v1/auth/profile', json=fields).status_code == 403
        csrf = client.get('/settings/passwords').context['csrf_token']
        for revision in (None, True, '1', -1):
            response = client.patch('/api/v1/auth/profile', json={**fields, 'expected_revision': revision}, headers={CSRF_HEADER: csrf})
            assert response.status_code == 400
            assert response.json()['error']['code'] == 'REVISION_REQUIRED'
        token = client.post('/api/v1/auth/login', json={'username': 'admin', 'password': NEW_PASSWORD}).json()['access_token']
        revision = client.get('/api/v1/auth/profile').json()['revision']
        response = client.post('/api/v1/auth/change-password', json={
            'current_password': NEW_PASSWORD, 'new_password': ROTATED_PASSWORD,
            'confirmation': ROTATED_PASSWORD, 'expected_revision': revision,
        }, headers=bearer(token))
        assert response.status_code == 200
        assert response.json()['reauthenticate'] is True
        assert response.headers['cache-control'] == 'no-store'
        assert client.get('/api/v1/auth/profile', headers=bearer(token)).status_code == 401


def test_legacy_signed_cookie_requires_new_login(tmp_path):
    settings = replace(make_settings(tmp_path), archive_toolchain_auto_install=False)
    with TestClient(create_app(settings)) as client:
        old = base64.b64encode(json.dumps({
            'authenticated': True, 'username': 'admin', 'must_change_password': False,
        }).encode())
        cookie = TimestampSigner(settings.app_secret_key).sign(old).decode()
        client.cookies.set('session', cookie)
        assert client.get('/api/v1/auth/profile').status_code == 401
        assert client.get('/', follow_redirects=False).headers['location'] == '/login'
