"""qBittorrent settings share atomic edits and never persist secret receipts."""
import asyncio
from copy import copy
from hashlib import sha256
import json
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from app.archive.service import ArchiveSettingsService
from app.main import create_app
from app.review.commands import WorkCommandError
from tests.integration.test_settings_commands import setup, observed
from tests.integration.test_settings_command_http import fields
from tests.integration.test_manual_add import _settings, _authenticate
from tests.integration.test_realtime import hello, call
from tests.integration.test_work_enqueue_http import stop_workers


@pytest.mark.asyncio
async def test_first_concurrent_secret_saves_share_one_private_key(tmp_path):
    database, system, archive, commands = await setup(tmp_path)
    peers = [copy(archive) for _ in range(8)]
    keys = await asyncio.gather(*(peer.master_key() for peer in peers))
    assert len(set(keys)) == 1
    assert await ArchiveSettingsService(database, tmp_path).master_key() == keys[0]


@pytest.mark.asyncio
async def test_secret_command_concurrency_replay_restart_and_hmac(tmp_path):
    database, system, archive, commands = await setup(tmp_path)
    password = uuid4().hex
    request = await observed(commands, 'archive', 'torrent', base_url='http://remote.invalid',
                             username='fixture-user', password=password)
    results = await asyncio.gather(*(commands.execute('torrent', request) for _ in range(2)))
    assert results[0] == results[1]
    assert set(results[0]) == {'scope', 'revision', 'status'}
    with database.connection() as connection:
        digest, receipt = connection.execute('SELECT payload_hash, result_json FROM settings_command_receipts').fetchone()
        canonical = json.dumps({'action': 'torrent', 'expected_revision': request['expected_revision'], 'values': {
            'base_url': request['base_url'], 'username': request['username'], 'password': password}},
            sort_keys=True, separators=(',', ':')).encode()
        assert digest != sha256(canonical).hexdigest()
        assert password not in receipt and password not in digest
        assert password not in dict(connection.execute('SELECT key, value FROM archive_settings')).values()
    database, system, archive, commands = await setup(tmp_path)
    assert (await archive.torrent_client()).password == password
    await archive.save_torrent_client({'base_url': 'http://new.invalid'})
    assert await commands.execute('torrent', request) == results[0]
    assert (await archive.torrent_client()).base_url == 'http://new.invalid'
    with pytest.raises(WorkCommandError) as error:
        await commands.execute('torrent', {**request, 'password': uuid4().hex})
    assert error.value.code == 'IDEMPOTENCY_CONFLICT'


@pytest.mark.asyncio
async def test_two_devices_have_one_winner_and_partial_json_preserves_other_fields(tmp_path):
    database, system, archive, commands = await setup(tmp_path)
    password = uuid4().hex
    await archive.save_torrent_client({'base_url': 'http://remote.invalid', 'username': 'original',
                                       'password': password, 'keep_seeding': True})
    original = await observed(commands, 'archive', 'torrent', username='changed')
    results = await asyncio.gather(*(commands.execute('torrent', {**original, 'idempotency_key': key})
                                    for key in ('first', 'second')), return_exceptions=True)
    assert sum(isinstance(result, dict) for result in results) == 1
    assert [result.code for result in results if isinstance(result, WorkCommandError)] == ['REVISION_CONFLICT']
    config = await archive.torrent_client()
    assert config.base_url == 'http://remote.invalid' and config.username == 'changed'
    assert config.password == password and config.keep_seeding
    assert (await commands.versions('archive'))['auto-pack'] == 1


@pytest.mark.asyncio
async def test_receipt_failure_rolls_back_encrypted_value_and_config(tmp_path, monkeypatch):
    database, system, archive, commands = await setup(tmp_path)
    original = await observed(commands, 'archive', 'torrent', base_url='http://remote.invalid', password=uuid4().hex)
    before = await commands.versions('archive')

    def fail(*args):
        raise RuntimeError('Fixture receipt failure')

    monkeypatch.setattr(commands, '_save_receipt', fail)
    with pytest.raises(RuntimeError):
        await commands.execute('torrent', original)
    assert await database.archive_settings() == {}
    assert await commands.versions('archive') == before


@pytest.mark.asyncio
async def test_preparation_does_not_hold_lock_and_final_cas_rejects_stale_config(tmp_path, monkeypatch):
    database, system, archive, commands = await setup(tmp_path)
    original = await observed(commands, 'archive', 'torrent', base_url='http://remote.invalid')
    prepare = ArchiveSettingsService.save_torrent_client

    async def race(self, values):
        await prepare(self, values)
        await database.save_archive_settings({'torrent_client_url': 'http://winner.invalid'})

    monkeypatch.setattr(ArchiveSettingsService, 'save_torrent_client', race)
    with pytest.raises(WorkCommandError) as error:
        await commands.execute('torrent', original)
    assert error.value.code == 'REVISION_CONFLICT'
    assert (await archive.torrent_client()).base_url == 'http://winner.invalid'


@pytest.mark.asyncio
async def test_blank_password_preserves_and_mask_is_refused(tmp_path):
    database, system, archive, commands = await setup(tmp_path)
    password = uuid4().hex
    await archive.save_torrent_client({'base_url': 'http://remote.invalid', 'password': password})
    request = await observed(commands, 'archive', 'torrent', password='')
    result = await commands.execute('torrent', request)
    assert await commands.execute('torrent', {key: value for key, value in request.items() if key != 'password'}) == result
    assert (await archive.torrent_client()).password == password
    request = await observed(commands, 'archive', 'torrent', key='mask', password='********')
    with pytest.raises(WorkCommandError) as error:
        await commands.execute('torrent', request)
    assert error.value.code == 'SECRET_MASK_INVALID'


def test_http_ws_and_form_replay_same_safe_receipt(tmp_path):
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False) as client:
        _authenticate(client, settings)
        stop_workers(client)
        form = fields(client, 'archive', '/archive-settings/torrent')
        headers = {'X-CSRF-Token': form['csrf_token']}
        path = '/api/v1/settings/archive/torrent'
        assert client.put(path, headers=headers, json={'base_url': 'http://remote.invalid'}).status_code == 428
        body = {**form, 'expected_revision': int(form['expected_revision']), 'base_url': 'http://remote.invalid',
                'password': uuid4().hex, 'keep_seeding': False, 'auto_pack': False}
        first = client.put(path, headers=headers, json=body)
        assert first.status_code == 200, first.text
        assert set(first.json()) == {'scope', 'revision', 'status'}
        with client.websocket_connect('/ws/ui', headers={'Origin': 'http://testserver'}) as ws:
            ready = hello(client, ws)
            name = next(name for name, entry in ready['commands'].items() if entry['path'] == path)
            replay, _ = call(ws, name, {'body': body})
            assert replay['type'] == 'command.result' and replay['data'] == first.json()
        submitted = {**body, 'keep_seeding': 'off', 'auto_pack': 'off'}
        assert client.post('/archive-settings/torrent', data=submitted).status_code == 303
        snapshot = client.get('/api/v1/settings/archive').json()
        assert snapshot['torrent']['password_set'] and 'password' not in snapshot['torrent']


def test_form_conflict_keeps_nonsecret_draft_and_original_identity(tmp_path):
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False) as client:
        _authenticate(client, settings)
        stop_workers(client)
        form = fields(client, 'archive', '/archive-settings/torrent')
        body = {'expected_revision': int(form['expected_revision']), 'idempotency_key': 'winner',
                'base_url': 'http://winner.invalid'}
        assert client.put('/api/v1/settings/archive/torrent', headers={'X-CSRF-Token': form['csrf_token']},
                          json=body).status_code == 200
        password = uuid4().hex
        response = client.post('/archive-settings/torrent', data={**form, 'base_url': 'http://draft.invalid',
                                                               'password': password})
        assert response.status_code == 409 and password not in response.text
        retained = fields_from_response(response)
        assert retained['expected_revision'] == form['expected_revision']
        assert retained['idempotency_key'] == form['idempotency_key']
        assert 'value="http://draft.invalid"' in response.text
        assert 'name="confirmed_command"' in response.text and 'data-dirty="true"' in response.text


def fields_from_response(response):
    from tests.integration.test_work_review_http import Forms
    return next(item['fields'] for item in Forms(response.text).forms if item['action'].endswith('/archive-settings/torrent'))
