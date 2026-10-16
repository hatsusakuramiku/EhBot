"""Profile identities, independent CAS and the public/legacy shared command."""
import asyncio
from html import unescape
import re

from fastapi.testclient import TestClient
import pytest

from app.main import create_app
from app.review.commands import WorkCommandError
from app.settings.commands import SettingsCommands
from app.settings.profile_commands import ProfileCommands
from tests.integration.test_manual_add import _settings, _authenticate
from tests.integration.test_realtime import hello, call
from tests.integration.test_settings_command_http import fields
from tests.integration.test_settings_commands import setup
from tests.integration.test_work_enqueue_http import stop_workers
from tests.settings_commands import observed_profile


async def observed(database, name='7zz-default', key='profile', **values):
    profile = await database.get_archive_tool_profile(name)
    return {'profile_uuid': profile.profile_uuid, 'expected_revision': profile.revision,
            'idempotency_key': key, **values}


@pytest.mark.asyncio
async def test_two_devices_one_winner_replay_and_other_profiles_independent(tmp_path):
    database, *_ = await setup(tmp_path)
    commands = ProfileCommands(database)
    original = await observed(database, enabled=False, timeout_seconds=42)
    other = await observed(database, 'zipfile-default', key='other', timeout_seconds=43)
    results = await asyncio.gather(*(commands.execute('7zz-default', {**original, 'idempotency_key': key})
                                    for key in ('first', 'second')), return_exceptions=True)
    assert sum(isinstance(row, dict) for row in results) == 1
    assert [row.code for row in results if isinstance(row, WorkCommandError)] == ['REVISION_CONFLICT']
    await commands.execute('zipfile-default', other)
    winner = next(index for index, row in enumerate(results) if isinstance(row, dict))
    payload = {**original, 'idempotency_key': ('first', 'second')[winner]}
    await database.set_archive_tool_profile_state('7zz-default', timeout_seconds=55)
    assert await ProfileCommands(database).execute('7zz-default', payload) == results[winner]
    assert (await database.get_archive_tool_profile('7zz-default')).timeout_seconds == 55
    with pytest.raises(WorkCommandError) as failure:
        await commands.execute('7zz-default', {**payload, 'timeout_seconds': 100})
    assert failure.value.code == 'IDEMPOTENCY_CONFLICT'


@pytest.mark.asyncio
@pytest.mark.parametrize('timeout', [0, -1, 'bad', True])
async def test_invalid_field_keeps_other_fields_and_version(tmp_path, timeout):
    database, *_ = await setup(tmp_path)
    profile = await database.get_archive_tool_profile('7zz-default')
    with pytest.raises(WorkCommandError):
        await ProfileCommands(database).execute(profile.name,
            await observed(database, timeout_seconds=timeout, enabled=False, executable_path='new-path'))
    assert await database.get_archive_tool_profile(profile.name) == profile


@pytest.mark.asyncio
async def test_noop_consumes_version_and_receipt_failure_rolls_back(tmp_path, monkeypatch):
    database, *_ = await setup(tmp_path)
    commands = ProfileCommands(database)
    original = await observed(database)
    result = await commands.execute('7zz-default', original)
    assert result['revision'] == original['expected_revision'] + 1
    before = await database.get_archive_tool_profile('7zz-default')

    def fail(*args):
        raise RuntimeError('Fixture receipt failure')

    monkeypatch.setattr(SettingsCommands, '_save_receipt', staticmethod(fail))
    with pytest.raises(RuntimeError):
        await commands.execute('7zz-default', await observed(database, key='failed', enabled=False))
    assert await database.get_archive_tool_profile('7zz-default') == before


@pytest.mark.asyncio
async def test_reused_name_or_number_cannot_accept_old_identity(tmp_path):
    database, *_ = await setup(tmp_path)
    original = await observed(database, enabled=False)
    commands = ProfileCommands(database)
    receipt = await commands.execute('7zz-default', original)
    with database.connection() as connection:
        connection.execute("DELETE FROM archive_tool_profiles WHERE name = '7zz-default'")
        connection.execute("INSERT INTO archive_tool_profiles(id, name, backend, kind) VALUES(2, '7zz-default', 'seven_zip', 'CLI')")
    with pytest.raises(WorkCommandError) as failure:
        await commands.execute('7zz-default', {**original, 'idempotency_key': 'stale-other-intent'})
    assert failure.value.status_code == 410
    assert await commands.execute('7zz-default', original) == receipt
    assert (await database.get_archive_tool_profile('7zz-default')).enabled


def test_http_ws_form_replay_and_explicit_conflict_confirmation(tmp_path):
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False) as client:
        _authenticate(client, settings)
        stop_workers(client)
        form = fields(client, 'archive', '/archive-settings/profiles/7zz-default')
        api = '/api/v1/settings/archive/profiles/7zz-default'
        headers = {'X-CSRF-Token': form['csrf_token']}
        assert client.put(api, headers=headers, json={'timeout_seconds': 42}).status_code == 428
        body = {**observed_profile(client, '7zz-default'), 'enabled': False, 'timeout_seconds': 42,
                'executable_path': '7zz-fixture'}
        first = client.put(api, headers=headers, json=body)
        assert first.status_code == 200, first.text
        with client.websocket_connect('/ws/ui', headers={'Origin': 'http://testserver'}) as ws:
            ready = hello(client, ws)
            name = next(name for name, entry in ready['commands'].items()
                        if entry['path'] == '/api/v1/settings/archive/profiles/{name}')
            replay, _ = call(ws, name, {'params': {'name': '7zz-default'}, 'body': body})
            assert replay['type'] == 'command.result' and replay['data'] == first.json()
        assert client.post('/archive-settings/profiles/7zz-default', data={**body, 'enabled': 'off',
                           'csrf_token': form['csrf_token']}).status_code == 303
        conflict = client.post('/archive-settings/profiles/7zz-default', data={**form, 'timeout_seconds': 99,
                                                                             'executable_path': 'draft-path'})
        assert conflict.status_code == 409
        assert 'value="draft-path"' in conflict.text and 'value="99"' in conflict.text
        assert 'data-dirty="true"' in conflict.text
        confirmation = unescape(re.search(r'name="confirmed_command" value="([^"]+)"', conflict.text)[1])
        data = {**form, 'timeout_seconds': 99, 'executable_path': 'draft-path', 'confirmed_command': confirmation}
        for _ in range(2):
            response = client.post('/archive-settings/profiles/7zz-default', data=data)
            assert response.status_code == 303, response.text
        profile = next(row for row in client.get('/api/v1/settings/archive').json()['profiles'] if row['name'] == '7zz-default')
        assert profile['timeout_seconds'] == 99 and profile['executable_path'] == 'draft-path'


@pytest.mark.parametrize('replace', [False, True])
def test_deleted_profile_native_error_keeps_original_draft_and_identity(tmp_path, replace):
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False) as client:
        _authenticate(client, settings)
        stop_workers(client)
        form = fields(client, 'archive', '/archive-settings/profiles/7zz-default')
        with client.app.state.database.connection() as connection:
            connection.execute("DELETE FROM archive_tool_profiles WHERE name = '7zz-default'")
            if replace:
                connection.execute("INSERT INTO archive_tool_profiles(id, name, backend, kind) "
                                   "VALUES(2, '7zz-default', 'seven_zip', 'CLI')")
        draft = {**form, 'executable_path': 'removed-profile-draft', 'timeout_seconds': '67', 'enabled': 'on'}
        for _ in range(2):
            response = client.post('/archive-settings/profiles/7zz-default', data=draft)
            assert response.status_code == 410
            assert 'value="removed-profile-draft"' in response.text and 'value="67"' in response.text
            assert 'data-dirty="true"' in response.text
            assert 'name="confirmed_command"' not in response.text
            retained = fields_from_response(response)
            for name in ('profile_uuid', 'expected_revision', 'idempotency_key'):
                assert retained[name] == form[name]
            draft = {**retained, 'executable_path': 'removed-profile-draft', 'timeout_seconds': '67', 'enabled': 'on'}


def fields_from_response(response):
    from tests.integration.test_work_review_http import Forms
    return next(item['fields'] for item in Forms(response.text).forms
                if item['action'].endswith('/archive-settings/profiles/7zz-default'))
