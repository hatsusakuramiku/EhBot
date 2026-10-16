"""Tool admission survives disconnects and drains real installer threads."""
import asyncio
from html import unescape
import re
import sqlite3
from threading import Event

from fastapi.testclient import TestClient
import pytest

from app.archive.service import ArchiveSettingsError
from app.downloads.worker import WorkerSession
from app.main import create_app
from app.review.commands import WorkCommandError
from app.settings.toolchain_commands import ToolchainCommands
from tests.integration.test_manual_add import _settings, _authenticate
from tests.integration.test_realtime import hello, call
from tests.integration.test_settings_commands import setup
from tests.integration.test_settings_command_http import fields
from tests.integration.test_work_enqueue_http import stop_workers


async def settled(service):
    await asyncio.gather(*tuple(service._submissions), return_exceptions=True)
    await asyncio.gather(*tuple(service._tasks), return_exceptions=True)


@pytest.mark.asyncio
async def test_two_devices_share_one_install_and_fixed_admission_after_failure(tmp_path, monkeypatch):
    database, _, archive, _ = await setup(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def install(*, force):
        calls.append(force)
        entered.set()
        await release.wait()
        raise ArchiveSettingsError('TOOLCHAIN_DOWNLOAD_FAILED', 'Fixture unavailable')

    monkeypatch.setattr(archive, 'install_toolchain', install)
    a, b = ToolchainCommands(database, archive), ToolchainCommands(database, archive)
    payload = {'expected_revision': 1, 'idempotency_key': 'same'}
    try:
        first, second = await asyncio.gather(a.submit(payload), b.submit(payload))
        assert first == second and first['status'] == 'ACCEPTED'
        await asyncio.wait_for(entered.wait(), 5)
        for body, code in [({**payload, 'idempotency_key': 'other'}, 'REVISION_CONFLICT'),
                           ({'expected_revision': 2, 'idempotency_key': 'other'}, 'TOOLCHAIN_BUSY'),
                           ({**payload, 'force': False}, 'IDEMPOTENCY_CONFLICT')]:
            with pytest.raises(WorkCommandError) as failure:
                await b.submit(body)
            assert failure.value.code == code
        assert (await b.snapshot())['revision'] == 2 and calls == [True]
    finally:
        release.set()
        await a.stop()
        await b.stop()
    replay = ToolchainCommands(database, archive)
    assert await replay.submit(payload) == first
    result = await replay.result(first['toolchain_operation_id'])
    assert result['status'] == 'FAILED' and result['error_code'] == 'TOOLCHAIN_DOWNLOAD_FAILED'
    assert 'Fixture unavailable' not in str(result) and calls == [True]


@pytest.mark.asyncio
async def test_admission_rollback_keeps_version_and_never_starts_install(tmp_path):
    database, _, archive, _ = await setup(tmp_path)
    service = ToolchainCommands(database, archive)
    with database.connection() as connection:
        connection.execute("CREATE TRIGGER fixture_fail_install BEFORE INSERT ON toolchain_operations BEGIN "
                           "SELECT RAISE(ABORT, 'fixture receipt failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        await service.submit({'expected_revision': 1, 'idempotency_key': 'rollback'})
    assert await service.snapshot() == {'revision': 1, 'operation': None}
    assert not service._tasks
    for payload in ({}, {'expected_revision': 1, 'idempotency_key': 'invalid', 'force': 'false'}):
        with pytest.raises(WorkCommandError):
            await service.submit(payload)


@pytest.mark.asyncio
async def test_disconnected_admission_still_dispatches_and_shutdown_waits_for_real_thread(tmp_path, monkeypatch):
    database, _, archive, _ = await setup(tmp_path)
    service = ToolchainCommands(database, archive)
    committed, dispatch = Event(), Event()
    entered, release = Event(), Event()
    begin = service._begin

    def delayed(payload):
        result = begin(payload)
        committed.set()
        assert dispatch.wait(5)
        return result

    def install(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return tmp_path / 'fixture-7zip'

    monkeypatch.setattr(service, '_begin', delayed)
    monkeypatch.setattr('app.archive.service.install_seven_zip', install)
    payload = {'expected_revision': 1, 'idempotency_key': 'disconnect'}
    caller = asyncio.create_task(service.submit(payload))
    stopping = None
    try:
        assert await asyncio.to_thread(committed.wait, 5)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        stopping = asyncio.create_task(service.stop())
        dispatch.set()
        assert await asyncio.to_thread(entered.wait, 5)
        await asyncio.sleep(0)
        assert not stopping.done()
        assert (await service.snapshot())['operation']['status'] == 'RUNNING'
    finally:
        dispatch.set()
        release.set()
        if stopping:
            await stopping
        else:
            await service.stop()
    result = await service.snapshot()
    assert result['operation']['status'] == 'COMPLETED'
    with pytest.raises(WorkCommandError) as failure:
        await service.submit({'expected_revision': 2, 'idempotency_key': 'after-stop'})
    assert failure.value.code == 'WORKER_STOPPING'


@pytest.mark.asyncio
async def test_cancelling_install_waits_for_thread_before_unknown_and_slot_release(tmp_path, monkeypatch):
    database, _, archive, _ = await setup(tmp_path)
    entered, release = Event(), Event()

    def install(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return tmp_path / 'fixture-7zip'

    monkeypatch.setattr('app.archive.service.install_seven_zip', install)
    service = ToolchainCommands(database, archive)
    result = await service.submit({'expected_revision': 1, 'idempotency_key': 'cancel'})
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        execution = next(iter(service._tasks))
        execution.cancel()
        await asyncio.sleep(0)
        assert not execution.done()
        assert (await service.result(result['toolchain_operation_id']))['status'] == 'RUNNING'
    finally:
        release.set()
        await service.stop()
    assert (await service.result(result['toolchain_operation_id']))['status'] == 'UNKNOWN'


@pytest.mark.asyncio
async def test_restart_marks_undispatched_intent_unknown_and_never_reinstalls_on_replay(tmp_path, monkeypatch):
    database, _, archive, _ = await setup(tmp_path)
    worker = WorkerSession(database)
    await worker.start()
    service = ToolchainCommands(database, archive)
    payload = {'expected_revision': 1, 'idempotency_key': 'interrupted'}
    receipt, dispatch, _ = service._begin(payload)
    assert dispatch
    await worker.stop()
    replacement = WorkerSession(database)
    await replacement.start()
    try:
        def unexpected(**kwargs):
            pytest.fail('Replay cannot install')
        monkeypatch.setattr(archive, 'install_toolchain', unexpected)
        replay = ToolchainCommands(database, archive)
        assert await replay.submit(payload) == receipt
        status = await replay.result(receipt['toolchain_operation_id'])
        assert status['status'] == 'UNKNOWN' and status['error_code'] == 'TOOLCHAIN_INTERRUPTED'
        assert (await replay.snapshot())['revision'] == receipt['revision']
        async def unavailable():
            return {'available': False}
        monkeypatch.setattr(archive, 'toolchain_status', unavailable)
        await replay.ensure()
        await replay._run(receipt['toolchain_operation_id'], True)
        with database.connection() as connection:
            assert connection.execute('SELECT count(*) FROM toolchain_operations').fetchone() == (1,)
    finally:
        await replacement.stop()


def test_json_ws_and_native_form_share_one_durable_install(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False) as client:
        _authenticate(client, settings)
        stop_workers(client)
        service = client.app.state.toolchain_commands
        entered, release, calls = Event(), Event(), []

        def install(*args, **kwargs):
            calls.append(kwargs['force'])
            entered.set()
            assert release.wait(10)
            return tmp_path / 'fixture-7zip'

        monkeypatch.setattr('app.archive.service.install_seven_zip', install)
        form = fields(client, 'archive', '/archive-settings/toolchain/install')
        api = '/api/v1/settings/archive/toolchain/install'
        headers = {'X-CSRF-Token': form['csrf_token']}
        payload = {'expected_revision': int(form['expected_revision']), 'idempotency_key': form['idempotency_key']}
        try:
            assert client.post(api, headers=headers, json={}).status_code == 428
            first = client.post(api, headers=headers, json=payload)
            assert first.status_code == 202, first.text
            assert entered.wait(5)
            result_url = '/api/v1/settings/archive/toolchain/operations/' + first.json()['toolchain_operation_id']
            state = client.get(result_url)
            assert state.json()['status'] == 'RUNNING' and state.headers['cache-control'] == 'no-store'
            with client.websocket_connect('/ws/ui', headers={'Origin': 'http://testserver'}) as ws:
                ready = hello(client, ws)
                name = next(name for name, entry in ready['commands'].items() if entry['path'] == api)
                replay, _ = call(ws, name, {'body': payload})
                assert replay['type'] == 'command.result' and replay['data'] == first.json()
            assert client.post('/archive-settings/toolchain/install', data=form).status_code == 202
            stale = client.post('/archive-settings/toolchain/install', data={**form, 'idempotency_key': 'stale'})
            assert stale.status_code == 409 and 'data-dirty="true"' in stale.text
            assert 'name="confirmed_command"' in stale.text
            assert client.get('/archive-settings/toolchain/operations/' + first.json()['toolchain_operation_id']).status_code == 200
            assert calls == [True]
        finally:
            release.set()
            client.portal.call(settled, service)
        assert client.get(result_url).json()['status'] == 'COMPLETED'
        assert client.post(api, headers=headers, json=payload).json() == first.json()
        assert calls == [True]
        confirmation = unescape(re.search(r'name="confirmed_command" value="([^"]+)"', stale.text)[1])
        retry = {**form, 'idempotency_key': 'stale', 'confirmed_command': confirmation}
        for _ in range(2):
            assert client.post('/archive-settings/toolchain/install', data=retry).status_code == 202
        client.portal.call(settled, service)
        assert calls == [True, True]
