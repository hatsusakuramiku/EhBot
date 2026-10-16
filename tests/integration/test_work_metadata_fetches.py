"""Explicit metadata fetch commands protect drafts across network waits and retries."""
import asyncio
import threading
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from app.api.deps import CSRF_HEADER
from app.db.database import Database
from app.downloads.archived import ArchivedWorkService
from app.downloads.worker import WorkerSession
from app.exhentai.downloader import ExHentaiDownloadError
from app.exhentai.service import ExHentaiService
from app.main import create_app
from app.review.application import fetch_metadata
from app.review.commands import WorkCommandError, WorkCommands
from tests.integration.test_api_domains import csrf_token
from tests.integration.test_api_v1 import log_in
from tests.integration.test_archive_metadata import packed_fixture
from tests.integration.test_work_commands import command
from tests.integration.test_realtime import hello, call


@pytest_asyncio.fixture
async def environment(tmp_path):
    database = Database(tmp_path / 'works.db')
    await database.initialize()
    with database.connection() as connection:
        connection.execute("INSERT INTO candidates(id, status, ex_gid, ex_gallery_token) "
                           "VALUES(1, 'PENDING_REVIEW', 123, 'fixture')")
        connection.execute("INSERT INTO candidates(id, status) VALUES(2, 'PENDING_REVIEW')")
    archived = ArchivedWorkService(database, tmp_path / 'library', tmp_path / 'work')
    supplier = ExHentaiService(database, tmp_path / 'work', tmp_path / 'library', None)
    return database, WorkCommands(database), supplier, archived


@pytest.mark.asyncio
async def test_edit_during_network_wait_wins_and_late_fetch_cannot_commit(environment, monkeypatch):
    database, commands, supplier, archived = environment
    entered, release = asyncio.Event(), asyncio.Event()

    async def network(*args):
        entered.set()
        await release.wait()
        return {'Title': 'Late title'}, SimpleNamespace(torrent_count=9, best_torrent=None, thumb='https://fixture/cover')

    monkeypatch.setattr(supplier, '_fetch_metadata', network)
    payload = await command(commands, key='fetch')
    task = asyncio.create_task(fetch_metadata(database, supplier, archived, 1, payload))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        # Same-work and independent writes both finish while the network is blocked.
        await asyncio.wait_for(commands.patch_metadata(1,
            await command(commands, key='edit', fields={'Title': 'Other client'}), 'admin'), 5)
        await asyncio.wait_for(commands.patch_metadata(2,
            await command(commands, 2, key='independent', fields={'Title': 'Independent'}), 'admin'), 5)
        release.set()
        with pytest.raises(WorkCommandError) as failure:
            await task
        assert failure.value.code == 'REVISION_CONFLICT'
        assert (await database.effective_metadata(1))['Title'] == 'Other client'
        with database.connection() as connection:
            assert connection.execute('SELECT torrent_count, thumb_url FROM candidates WHERE id = 1').fetchone() == (None, None)
            assert connection.execute("SELECT count(*) FROM metadata_values WHERE value_source='EXHENTAI'").fetchone()[0] == 0
            assert connection.execute('SELECT status, error_code FROM work_metadata_fetches').fetchone() == ('FAILED', 'REVISION_CONFLICT')
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_completed_replay_never_fetches_again_or_adopts_a_later_snapshot(environment, monkeypatch):
    database, commands, supplier, archived = environment
    calls = []

    async def network(*args):
        calls.append(args)
        return {'Title': 'Fetched'}, None

    monkeypatch.setattr(supplier, '_fetch_metadata', network)
    payload = await command(commands)
    first = await fetch_metadata(database, supplier, archived, 1, payload)
    assert first['status'] == 'COMPLETED' and first['metadata_editor']['revision'] == first['revision']
    await commands.patch_metadata(1, await command(commands, key='later', fields={'Title': 'Later'}), 'admin')
    replay = await fetch_metadata(database, supplier, archived, 1, payload)
    assert replay['revision'] == first['revision']
    assert replay['snapshot_changed'] and replay['metadata_editor'] is None
    assert len(calls) == 1 and (await database.effective_metadata(1))['Title'] == 'Later'


@pytest.mark.asyncio
async def test_running_replay_has_same_operation_and_new_key_is_busy(environment, monkeypatch):
    database, commands, supplier, archived = environment
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def network(*args):
        calls.append(args)
        entered.set()
        await release.wait()
        return {'Title': 'Fetched'}, None

    monkeypatch.setattr(supplier, '_fetch_metadata', network)
    payload = await command(commands)
    task = asyncio.create_task(fetch_metadata(database, supplier, archived, 1, payload))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        running = await fetch_metadata(database, supplier, archived, 1, payload)
        assert running['status'] == 'RUNNING' and running['metadata_editor'] is None
        with pytest.raises(WorkCommandError) as failure:
            await fetch_metadata(database, supplier, archived, 1, {**payload, 'idempotency_key': 'another'})
        assert failure.value.code == 'WORK_BUSY'
        release.set()
        result = await task
        assert result['metadata_fetch_operation_id'] == running['metadata_fetch_operation_id'] and len(calls) == 1
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_failed_replay_preserves_public_error_without_redispatch(environment, monkeypatch):
    database, commands, supplier, archived = environment
    calls = []

    async def network(*args):
        calls.append(args)
        raise ExHentaiDownloadError('EXHENTAI_UNREACHABLE', 'Fixture public error')

    monkeypatch.setattr(supplier, '_fetch_metadata', network)
    payload = await command(commands)
    for _ in range(2):
        with pytest.raises(WorkCommandError) as failure:
            await fetch_metadata(database, supplier, archived, 1, payload)
        assert failure.value.code == 'EXHENTAI_UNREACHABLE' and failure.value.status_code == 400
    assert len(calls) == 1
    assert await database.effective_metadata(1) == {}


@pytest.mark.asyncio
async def test_failed_final_write_rolls_back_metadata_routing_and_revision(environment, monkeypatch):
    database, commands, supplier, archived = environment
    payload = await command(commands)

    async def network(*args):
        return {'Title': 'Must roll back'}, SimpleNamespace(torrent_count=7, best_torrent=None)

    def fail(*args):
        raise OSError('private fixture provider detail')

    monkeypatch.setattr(supplier, '_fetch_metadata', network)
    monkeypatch.setattr(supplier, '_write_cover', fail)
    with pytest.raises(OSError):
        await fetch_metadata(database, supplier, archived, 1, payload)
    assert await database.effective_metadata(1) == {}
    assert (await commands.version(1))['revision'] == payload['expected_revision']
    with database.connection() as connection:
        assert connection.execute('SELECT torrent_count FROM candidates WHERE id=1').fetchone() == (None,)
        status, code, result = connection.execute('SELECT status, error_code, result_json FROM work_metadata_fetches').fetchone()
        assert (status, code) == ('FAILED', 'METADATA_FETCH_FAILED') and 'private fixture' not in result


@pytest.mark.asyncio
async def test_restart_marks_running_unknown_and_same_key_does_not_repeat_network(environment, monkeypatch):
    database, commands, supplier, archived = environment
    payload = await command(commands)
    receipt, dispatch = await commands.begin_metadata_fetch(1, payload)
    assert dispatch
    session = WorkerSession(database)
    await session.start()
    try:
        result = await commands.metadata_fetch_result(receipt['metadata_fetch_operation_id'])
        assert result['status'] == 'UNKNOWN'
        with pytest.raises(WorkCommandError) as failure:
            await fetch_metadata(database, supplier, archived, 1, payload)
        assert failure.value.code == 'METADATA_FETCH_INTERRUPTED'
    finally:
        await session.stop()


@pytest.mark.asyncio
async def test_completed_replay_survives_deleted_and_reused_numeric_id(environment, monkeypatch):
    database, commands, supplier, archived = environment

    async def network(*args):
        return {'Title': 'Original'}, None

    monkeypatch.setattr(supplier, '_fetch_metadata', network)
    payload = await command(commands)
    first = await fetch_metadata(database, supplier, archived, 1, payload)
    with database.connection() as connection:
        connection.execute('DELETE FROM candidates WHERE id=1')
    replay = await fetch_metadata(database, supplier, archived, 1, payload)
    assert replay['status'] == 'COMPLETED' and replay['metadata_editor'] is None
    with database.connection() as connection:
        connection.execute("INSERT INTO candidates(id, status) VALUES(1, 'PENDING_REVIEW')")
    replay = await fetch_metadata(database, supplier, archived, 1, payload)
    assert replay['revision'] == first['revision'] and replay['metadata_editor'] is None
    assert await database.effective_metadata(1) == {}


def test_http_and_html_share_fetch_receipts_and_require_seen_version(tmp_path, monkeypatch):
    settings, database, _, _ = packed_fixture(tmp_path)
    with database.connection() as connection:
        connection.execute("UPDATE candidates SET ex_gid=123, ex_gallery_token='fixture' WHERE id=1")
    calls = []

    async def network(*args):
        calls.append(args)
        return {'Title': 'Fetched'}, None

    with TestClient(create_app(settings), follow_redirects=False) as client:
        log_in(client, settings)
        monkeypatch.setattr(client.app.state.exhentai_service, '_fetch_metadata', network)
        csrf = csrf_token(client)
        headers = {CSRF_HEADER: csrf}
        assert client.post('/api/v1/works/1/fetch-metadata', headers=headers).status_code == 428
        assert client.post('/candidates/1/exhentai-metadata', data={'csrf_token': csrf}).status_code == 400
        assert not calls
        editor = client.get('/api/v1/works/1').json()['metadata_editor']
        payload = {'work_id': editor['work_id'], 'expected_revision': editor['revision'], 'idempotency_key': 'fetch'}
        accepted, _ = client.portal.call(WorkCommands(database).begin_metadata_fetch, 1, payload)
        running = client.post('/api/v1/works/1/fetch-metadata', headers=headers, json=payload)
        assert running.status_code == 202 and running.json()['status'] == 'RUNNING'
        # Simulate the original HTTP owner reaching its final DB commit.
        client.app.state.exhentai_service._commit_metadata_fetch(1, accepted['metadata_fetch_operation_id'], {'Title': 'Fetched'}, None)
        replay = client.post('/candidates/1/exhentai-metadata', data={'csrf_token': csrf, **payload})
        assert replay.status_code == 303 and not calls
        operation = client.get('/api/v1/metadata-fetches/' + accepted['metadata_fetch_operation_id'])
        assert operation.status_code == 200 and operation.json()['status'] == 'COMPLETED'
        client.cookies.clear()
        assert client.get('/api/v1/metadata-fetches/' + accepted['metadata_fetch_operation_id']).status_code == 401


@pytest.mark.asyncio
async def test_cancel_during_acceptance_drains_thread_and_does_not_leave_running_operation(environment, monkeypatch):
    database, commands, supplier, archived = environment
    entered, release = threading.Event(), threading.Event()
    original = WorkCommands._begin_metadata_fetch

    def blocked(self, *args):
        result = original(self, *args)
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(WorkCommands, '_begin_metadata_fetch', blocked)
    task = asyncio.create_task(fetch_metadata(database, supplier, archived, 1, await command(commands)))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        with database.connection() as connection:
            assert connection.execute('SELECT status, error_code FROM work_metadata_fetches').fetchone() == (
                'FAILED', 'METADATA_FETCH_CANCELLED')
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_stopping_session_rejects_new_fetch_and_late_public_response(environment, monkeypatch):
    database, commands, supplier, archived = environment
    session = WorkerSession(database)
    await session.start()
    entered, release = asyncio.Event(), asyncio.Event()

    async def network(*args):
        entered.set()
        await release.wait()
        return {'Title': 'Late'}, None

    monkeypatch.setattr(supplier, '_fetch_metadata', network)
    payload = await command(commands)
    task = asyncio.create_task(fetch_metadata(database, supplier, archived, 1, payload))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        session.stopping = True
        with pytest.raises(WorkCommandError) as failure:
            await commands.begin_metadata_fetch(2, await command(commands, 2, key='stopping'))
        assert failure.value.code == 'WORKER_STOPPING'
        release.set()
        with pytest.raises(WorkCommandError) as failure:
            await task
        assert failure.value.code == 'METADATA_FETCH_STALE' and await database.effective_metadata(1) == {}
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await session.stop()


def test_fetch_http_to_ws_replay_uses_original_network_result(tmp_path, monkeypatch):
    settings, database, path, _ = packed_fixture(tmp_path)
    with database.connection() as connection:
        connection.execute("UPDATE candidates SET ex_gid=123, ex_gallery_token='fixture' WHERE id=1")
    calls = []

    async def network(*args):
        calls.append(args)
        return {'Title': 'Fetched'}, None

    with TestClient(create_app(settings)) as client:
        log_in(client, settings)
        monkeypatch.setattr(client.app.state.exhentai_service, '_fetch_metadata', network)
        editor = client.get('/api/v1/works/1').json()['metadata_editor']
        payload = {'work_id': editor['work_id'], 'expected_revision': editor['revision'], 'idempotency_key': 'fetch-ws'}
        first = client.post('/api/v1/works/1/fetch-metadata', headers={CSRF_HEADER: csrf_token(client)}, json=payload)
        assert first.status_code == 200, first.text
        with client.websocket_connect('/ws/ui', headers={'Origin': 'http://testserver'}) as ws:
            hello(client, ws)
            replay, _ = call(ws, 'api.fetch_work_metadata', {'params': {'candidate_id': '1'}, 'body': payload})
            assert replay['type'] == 'command.result', replay
            assert replay['data']['metadata_fetch_operation_id'] == first.json()['metadata_fetch_operation_id']
            assert replay['data']['revision'] == first.json()['revision']
        assert len(calls) == 1
