"""Review commands must commit approval, queue intent and retry receipt together."""
import asyncio

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from app.db.database import Database
from app.downloads.service import DownloadError, DownloadService
from app.review.commands import WorkCommandError, WorkCommands
from app.review.orchestration import ReviewOrchestrator
from app.main import create_app
from tests.integration.test_api_domains import csrf_token, seed
from tests.integration.test_api_v1 import make_settings, log_in
from tests.integration.test_work_commands import command
from tests.work_commands import observed_work
from tests.integration.test_realtime import hello, call


@pytest_asyncio.fixture
async def environment(tmp_path):
    database = Database(tmp_path / 'works.db')
    await database.initialize()
    with database.connection() as connection:
        connection.execute("INSERT INTO candidates(id, status, ex_gid, preview_url) "
                           "VALUES(1, 'PENDING_REVIEW', 123, 'https://telegra.ph/fixture')")
    download = DownloadService(database, tmp_path / 'work')
    orchestrator = ReviewOrchestrator(database, lambda: download,
        torrent_available=lambda: False, telegraph_available=lambda: True, telegram_available=lambda: False)
    return database, WorkCommands(database), download, orchestrator


@pytest.mark.asyncio
async def test_approve_and_reject_from_same_version_have_exactly_one_winner(environment):
    database, commands, download, orchestrator = environment
    observed = await command(commands)
    results = await asyncio.gather(orchestrator.work_command(1, 'approve', observed, 'admin'),
        orchestrator.work_command(1, 'reject', {**observed, 'idempotency_key': 'reject'}, 'admin'), return_exceptions=True)
    accepted = [result for result in results if isinstance(result, dict)]
    assert len(accepted) == 1
    failure = next(result for result in results if isinstance(result, WorkCommandError))
    assert failure.code == 'REVISION_CONFLICT'
    with database.connection() as connection:
        assert connection.execute('SELECT count(*) FROM review_actions').fetchone() == (1,)
        assert connection.execute('SELECT count(*) FROM work_command_receipts').fetchone() == (1,)
        approvals = connection.execute('SELECT count(*) FROM candidate_approvals').fetchone()[0]
        jobs = connection.execute('SELECT count(*) FROM download_jobs').fetchone()[0]
        assert approvals == jobs == (1 if accepted[0]['action'] == 'approve' else 0)


@pytest.mark.asyncio
async def test_failure_after_enqueue_rolls_back_approval_task_audit_version_and_receipt(environment, monkeypatch):
    database, commands, download, orchestrator = environment
    observed = await command(commands)
    original = download._enqueue_sync

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise DownloadError('FIXTURE_QUEUE_FAILURE', 'Fixture failure after creating task')

    monkeypatch.setattr(download, '_enqueue_sync', fail)
    with pytest.raises(DownloadError):
        await orchestrator.work_command(1, 'approve', observed, 'admin')
    assert (await commands.version(1))['revision'] == observed['expected_revision']
    with database.connection() as connection:
        for table in ('candidate_approvals', 'download_jobs', 'queue_tasks', 'queue_attempts', 'review_actions', 'work_command_receipts'):
            assert connection.execute('SELECT count(*) FROM ' + table).fetchone() == (0,)
        assert connection.execute('SELECT status FROM candidates WHERE id=1').fetchone() == ('PENDING_REVIEW',)


@pytest.mark.asyncio
async def test_accepted_approval_replay_does_not_reprepare_or_create_another_task(environment, monkeypatch):
    database, commands, download, orchestrator = environment
    observed = await command(commands)
    first = await orchestrator.work_command(1, 'approve', observed, 'admin')
    assert len(first['job_ids']) == len(first['task_ids']) == 1

    async def forbidden(*args):
        raise AssertionError('replay must return receipt before routing or provider preflight')

    monkeypatch.setattr(download, 'prepare_work_download', forbidden)
    assert await orchestrator.work_command(1, 'approve', observed, 'renamed-admin') == first
    with database.connection() as connection:
        assert connection.execute('SELECT count(*) FROM download_jobs').fetchone() == (1,)
        assert connection.execute('SELECT count(*) FROM review_actions').fetchone() == (1,)
        assert connection.execute('SELECT count(*) FROM queue_pack_intents').fetchone() == (1,)


@pytest.mark.asyncio
async def test_source_preparation_wait_does_not_hold_lock_and_late_approval_is_refused(environment, monkeypatch):
    database, commands, download, orchestrator = environment
    entered, release = asyncio.Event(), asyncio.Event()
    original = download.prepare_work_download

    async def prepare(*args):
        entered.set()
        await release.wait()
        return await original(*args)

    monkeypatch.setattr(download, 'prepare_work_download', prepare)
    task = asyncio.create_task(orchestrator.work_command(1, 'approve', await command(commands), 'admin'))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await asyncio.wait_for(commands.patch_metadata(1,
            await command(commands, key='other', fields={'Title': 'Other device'}), 'admin'), 5)
        release.set()
        with pytest.raises(WorkCommandError) as failure:
            await task
        assert failure.value.code == 'REVISION_CONFLICT'
        with database.connection() as connection:
            assert connection.execute('SELECT count(*) FROM candidate_approvals').fetchone() == (0,)
            assert connection.execute('SELECT count(*) FROM download_jobs').fetchone() == (0,)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_reject_requeue_and_unchanged_status_each_consume_the_observed_version(environment):
    database, commands, download, orchestrator = environment
    reject = await orchestrator.work_command(1, 'reject', await command(commands), 'admin')
    same = await command(commands, key='same')
    second = await orchestrator.work_command(1, 'reject', same, 'admin')
    assert second['revision'] > reject['revision']
    with pytest.raises(WorkCommandError) as failure:
        await orchestrator.work_command(1, 'reject', {**same, 'idempotency_key': 'stale'}, 'admin')
    assert failure.value.code == 'REVISION_CONFLICT'
    requeue = await orchestrator.work_command(1, 'requeue', await command(commands, key='requeue'), 'admin')
    assert requeue['revision'] > second['revision'] and not requeue['job_ids']
    with database.connection() as connection:
        assert connection.execute('SELECT status FROM candidates WHERE id=1').fetchone() == ('PENDING_REVIEW',)
        assert connection.execute('SELECT count(*) FROM download_jobs').fetchone() == (0,)


@pytest.mark.asyncio
async def test_key_cannot_be_reused_for_another_review_action(environment):
    database, commands, download, orchestrator = environment
    observed = await command(commands)
    await orchestrator.work_command(1, 'reject', observed, 'admin')
    with pytest.raises(WorkCommandError) as failure:
        await orchestrator.work_command(1, 'requeue', observed, 'admin')
    assert failure.value.code == 'IDEMPOTENCY_CONFLICT'


@pytest.mark.asyncio
async def test_invalid_transition_rolls_back_without_a_receipt(environment):
    database, commands, download, orchestrator = environment
    await orchestrator.work_command(1, 'approve', await command(commands), 'admin')
    with pytest.raises(WorkCommandError) as failure:
        await orchestrator.work_command(1, 'reject', await command(commands, key='invalid'), 'admin')
    assert failure.value.code == 'REVIEW_INVALID_TRANSITION'
    with database.connection() as connection:
        assert connection.execute('SELECT count(*) FROM work_command_receipts').fetchone() == (1,)


@pytest.mark.asyncio
async def test_no_version_refuses_review_without_side_effects(environment):
    database, commands, download, orchestrator = environment
    with pytest.raises(WorkCommandError) as failure:
        await orchestrator.work_command(1, 'reject', {}, 'admin')
    assert failure.value.status_code == 428
    with database.connection() as connection:
        assert connection.execute('SELECT count(*) FROM review_actions').fetchone() == (0,)


@pytest.mark.asyncio
async def test_concurrent_approval_replays_return_one_original_receipt(environment):
    database, commands, download, orchestrator = environment
    observed = await command(commands)
    results = await asyncio.gather(orchestrator.work_command(1, 'approve', observed, 'admin'),
                                   orchestrator.work_command(1, 'approve', observed, 'admin'))
    assert results[0] == results[1]
    with database.connection() as connection:
        assert connection.execute('SELECT count(*) FROM download_jobs').fetchone() == (1,)
        assert connection.execute('SELECT count(*) FROM review_actions').fetchone() == (1,)


@pytest.mark.asyncio
async def test_receipt_is_rechecked_if_concurrent_owner_finishes_before_preparation(environment, monkeypatch):
    database, commands, download, orchestrator = environment
    observed = await command(commands)
    prepared = await download.prepare_work_download(1, 'TELEGRAPH')
    original = orchestrator._load_reviewable
    accepted = []

    async def interleaved(candidate_id):
        accepted.append(await commands.review(candidate_id, 'approve', observed, 'admin',
            download=download, prepared=prepared))
        return await original(candidate_id)

    monkeypatch.setattr(orchestrator, '_load_reviewable', interleaved)
    assert await orchestrator.work_command(1, 'approve', observed, 'admin') == accepted[0]


def test_http_review_requires_version_and_replays_original_outcome(tmp_path):
    settings = make_settings(tmp_path)
    settings.data_path.mkdir(parents=True, exist_ok=True)
    seed(settings, [(1, 'PENDING_REVIEW', 'Review fixture')])
    with TestClient(create_app(settings)) as client:
        log_in(client, settings)
        headers = {'X-CSRF-Token': csrf_token(client)}
        assert client.post('/api/v1/works/1/review', headers=headers, json={'action': 'reject'}).status_code == 428
        first = {**observed_work(client), 'action': 'reject', 'note': 'Fixture reason'}
        accepted = client.post('/api/v1/works/1/review', headers=headers, json=first)
        assert accepted.status_code == 200, accepted.text
        requeue = {**observed_work(client), 'action': 'requeue'}
        assert client.post('/api/v1/works/1/review', headers=headers, json=requeue).status_code == 200
        replay = client.post('/api/v1/works/1/review', headers=headers, json=first)
        assert replay.json() == accepted.json()
        stale = client.post('/api/v1/works/1/review', headers=headers, json={**first, 'idempotency_key': 'stale'})
        assert stale.status_code == 409 and stale.json()['error']['details']['work_id'] == first['work_id']
        assert client.get('/api/v1/works/1').json()['status']['code'] == 'PENDING_REVIEW'


def test_review_http_and_ws_share_original_receipt(tmp_path):
    settings = make_settings(tmp_path)
    settings.data_path.mkdir(parents=True, exist_ok=True)
    seed(settings, [(1, 'PENDING_REVIEW', 'Review fixture')])
    with TestClient(create_app(settings)) as client:
        log_in(client, settings)
        first = {**observed_work(client), 'action': 'reject'}
        accepted = client.post('/api/v1/works/1/review', headers={'X-CSRF-Token': csrf_token(client)}, json=first)
        assert accepted.status_code == 200
        with client.websocket_connect('/ws/ui', headers={'Origin': 'http://testserver'}) as ws:
            hello(client, ws)
            replay, _ = call(ws, 'api.review_work', {'params': {'candidate_id': '1'}, 'body': first})
            assert replay['type'] == 'command.result', replay
            assert replay['data'] == accepted.json()
            changed, _ = call(ws, 'api.review_work', {'params': {'candidate_id': '1'},
                'body': {**first, 'action': 'requeue'}}, request_id='changed')
            assert changed['type'] == 'command.error' and changed['status'] == 409
