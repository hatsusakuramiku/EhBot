"""Task acceptance, stable work CAS and retry receipts share a transaction."""
import asyncio
import json

import pytest

from app.downloads.service import DownloadError
from app.review.application import enqueue_work
from app.review.commands import WorkCommandError, WorkCommands
from app.review.service import ReviewService
from tests.integration.test_work_review_commands import environment
from tests.integration.test_work_commands import command
from tests.integration.test_artifact_pipeline import prepare, packer


@pytest.mark.asyncio
async def test_source_requires_version_without_creating_a_task(environment):
    database, commands, download, orchestrator = environment
    await ReviewService(database).approve_candidate(1, 'fixture')
    with pytest.raises(WorkCommandError) as error:
        await enqueue_work(database, download, 1, 'TELEGRAPH', {}, available={'TELEGRAPH'})
    assert error.value.status_code == 428
    with database.connection() as connection:
        assert connection.execute('SELECT count(*) FROM download_jobs').fetchone() == (0,)


@pytest.mark.asyncio
async def test_competing_sources_from_same_work_version_have_one_winner(environment):
    database, commands, download, orchestrator = environment
    await ReviewService(database).approve_candidate(1, 'fixture')
    observed = await command(commands)
    results = await asyncio.gather(enqueue_work(database, download, 1, 'TELEGRAPH', observed, available={'TELEGRAPH'}),
        enqueue_work(database, download, 1, 'EXHENTAI', {**observed, 'idempotency_key': 'other'}), return_exceptions=True)
    assert len([result for result in results if isinstance(result, dict)]) == 1
    error = next(result for result in results if isinstance(result, WorkCommandError))
    assert error.code == 'REVISION_CONFLICT'
    with database.connection() as connection:
        assert connection.execute('SELECT count(*) FROM download_jobs').fetchone() == (1,)
        assert connection.execute('SELECT count(*) FROM work_command_receipts').fetchone() == (1,)


@pytest.mark.asyncio
async def test_replay_returns_original_task_before_availability_and_does_not_revive_it(environment, monkeypatch):
    database, commands, download, orchestrator = environment
    await ReviewService(database).approve_candidate(1, 'fixture')
    observed = await command(commands)
    first, second = await asyncio.gather(*(
        enqueue_work(database, download, 1, 'TELEGRAPH', observed, available={'TELEGRAPH'}) for _ in range(2)))
    assert first == second
    with database.connection() as connection:
        connection.execute("UPDATE download_jobs SET state = 'CANCELLED' WHERE id = ?", (first['job_id'],))

    async def forbidden(*args):
        raise AssertionError('receipt replay must not repeat provider preparation')

    monkeypatch.setattr(download, 'prepare_work_download', forbidden)
    assert await enqueue_work(database, download, 1, 'TELEGRAPH', observed, available=set()) == first
    with database.connection() as connection:
        assert connection.execute('SELECT state FROM download_jobs WHERE id = ?', (first['job_id'],)).fetchone() == ('CANCELLED',)


@pytest.mark.asyncio
async def test_failure_after_enqueue_rolls_back_task_version_and_receipt(environment, monkeypatch):
    database, commands, download, orchestrator = environment
    await ReviewService(database).approve_candidate(1, 'fixture')
    observed = await command(commands)
    original = download._enqueue_sync

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise DownloadError('FIXTURE_FAILURE', 'Fixture after task insertion')

    monkeypatch.setattr(download, '_enqueue_sync', fail)
    with pytest.raises(DownloadError):
        await enqueue_work(database, download, 1, 'TELEGRAPH', observed, available={'TELEGRAPH'})
    assert (await commands.version(1))['revision'] == observed['expected_revision']
    with database.connection() as connection:
        for table in ('download_jobs', 'queue_tasks', 'work_command_receipts'):
            assert connection.execute('SELECT count(*) FROM ' + table).fetchone() == (0,)


@pytest.mark.asyncio
async def test_provider_preparation_does_not_hold_lock_and_late_result_cannot_enqueue(environment, monkeypatch):
    database, commands, download, orchestrator = environment
    await ReviewService(database).approve_candidate(1, 'fixture')
    entered, release = asyncio.Event(), asyncio.Event()
    original = download.prepare_work_download

    async def prepare_source(*args):
        entered.set()
        await release.wait()
        return await original(*args)

    monkeypatch.setattr(download, 'prepare_work_download', prepare_source)
    task = asyncio.create_task(enqueue_work(database, download, 1, 'TELEGRAPH', await command(commands), available={'TELEGRAPH'}))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await asyncio.wait_for(commands.patch_metadata(1, await command(commands, key='other-edit', fields={'Title': 'Other device'}), 'admin'), 5)
        release.set()
        with pytest.raises(WorkCommandError) as error:
            await task
        assert error.value.code == 'REVISION_CONFLICT'
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    with database.connection() as connection:
        assert connection.execute('SELECT count(*) FROM download_jobs').fetchone() == (0,)


@pytest.mark.asyncio
async def test_conversion_atomically_keeps_exact_artifact_and_replays_original_task(tmp_path, monkeypatch):
    database, path, candidate_id = await prepare(tmp_path)
    conversion = packer(database, tmp_path)
    commands = WorkCommands(database)
    with database.connection() as connection:
        artifact_id = connection.execute("SELECT artifact_id FROM artifacts WHERE artifact_type = 'ARCHIVE'").fetchone()[0]
    observed = await command(commands, candidate_id, input_artifact_id=artifact_id)
    first, second = await asyncio.gather(*(
        enqueue_work(database, conversion, candidate_id, 'CONVERSION', observed) for _ in range(2)))
    assert first == second
    with database.connection() as connection:
        details = json.loads(connection.execute('SELECT details_json FROM download_jobs WHERE id = ?', (first['job_id'],)).fetchone()[0])
        assert details['input_artifact_id'] == artifact_id
        connection.execute("UPDATE download_jobs SET state = 'CONVERSION_FAILED' WHERE id = ?", (first['job_id'],))

    async def forbidden(*args):
        raise AssertionError('pack preflight must not run again on replay')

    monkeypatch.setattr(conversion, 'prepare_enqueue', forbidden)
    assert await enqueue_work(database, conversion, candidate_id, 'CONVERSION', observed) == first
    with database.connection() as connection:
        assert connection.execute('SELECT state FROM download_jobs WHERE id = ?', (first['job_id'],)).fetchone() == ('CONVERSION_FAILED',)


@pytest.mark.asyncio
async def test_conversion_rolls_back_after_insertion_and_refuses_stale_artifact_selection(tmp_path, monkeypatch):
    database, path, candidate_id = await prepare(tmp_path)
    conversion = packer(database, tmp_path)
    commands = WorkCommands(database)
    observed = await command(commands, candidate_id)
    original = conversion._enqueue_sync

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('Fixture after packaging insertion')

    monkeypatch.setattr(conversion, '_enqueue_sync', fail)
    with pytest.raises(RuntimeError):
        await enqueue_work(database, conversion, candidate_id, 'CONVERSION', observed)
    assert (await commands.version(candidate_id))['revision'] == observed['expected_revision']
    with database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM queue_tasks WHERE kind = 'PACK'").fetchone() == (0,)
        assert connection.execute('SELECT count(*) FROM work_command_receipts').fetchone() == (0,)
    await commands.patch_metadata(candidate_id, await command(commands, candidate_id, key='other-edit', fields={'Title': 'Other'}), 'admin')
    with pytest.raises(WorkCommandError) as error:
        await enqueue_work(database, conversion, candidate_id, 'CONVERSION', observed)
    assert error.value.code == 'REVISION_CONFLICT'


@pytest.mark.asyncio
async def test_active_same_intent_reuses_task_but_pack_change_is_conflict(environment):
    database, commands, download, _ = environment
    await ReviewService(database).approve_candidate(1, 'fixture')
    first = await enqueue_work(database, download, 1, 'EXHENTAI', await command(commands, pack_requested=False))
    again = await enqueue_work(database, download, 1, 'EXHENTAI', await command(commands, key='again', pack_requested=False))
    assert again['task_id'] == first['task_id'] and again['revision'] == first['revision']
    before = await commands.version(1)
    with pytest.raises(WorkCommandError) as failure:
        await enqueue_work(database, download, 1, 'EXHENTAI', await command(commands, key='change', pack_requested=True))
    assert failure.value.code == 'PACK_INTENT_CONFLICT'
    assert await commands.version(1) == before
    with database.connection() as connection:
        assert connection.execute('SELECT pack_requested FROM queue_pack_intents').fetchall() == [(0,)]
        assert connection.execute('SELECT count(*) FROM queue_tasks').fetchone() == (1,)


@pytest.mark.asyncio
async def test_active_different_source_stops_then_starts_and_replay_never_repeats_preflight(environment, monkeypatch):
    database, commands, download, _ = environment
    await ReviewService(database).approve_candidate(1, 'fixture')
    first = await enqueue_work(database, download, 1, 'EXHENTAI', await command(commands, pack_requested=False))
    calls = []
    async def preflight(candidate_id, provider):
        calls.append((candidate_id, provider))
        # An independent writer can proceed during preflight.
        with database.connection() as connection:
            connection.execute("INSERT OR REPLACE INTO archive_settings(key, value) VALUES('fixture', 'unlocked')")
    monkeypatch.setattr(download, '_preflight_switch', preflight)
    body = await command(commands, key='switch', pack_requested=True)
    switched = await enqueue_work(database, download, 1, 'TELEGRAPH', body, available={'TELEGRAPH'})
    assert switched['task_id'] == first['task_id'] and switched['state'] == 'STOPPING'
    with database.connection() as connection:
        assert connection.execute('SELECT provider, state FROM download_jobs').fetchall() == [('EXHENTAI', 'STOPPING')]
        assert connection.execute('SELECT count(*) FROM work_command_receipts').fetchone() == (2,)
    assert await enqueue_work(database, download, 1, 'TELEGRAPH', body) == switched
    assert calls == [(1, 'TELEGRAPH')]
    await download.process_operations()
    with database.connection() as connection:
        assert connection.execute("SELECT provider FROM download_jobs WHERE state = 'PENDING'").fetchall() == [('TELEGRAPH',)]
        assert connection.execute("SELECT i.pack_requested FROM queue_pack_intents i JOIN queue_tasks t USING(task_id) "
                                  "WHERE t.provider = 'TELEGRAPH'").fetchone() == (1,)
    assert await enqueue_work(database, download, 1, 'TELEGRAPH', body) == switched


@pytest.mark.asyncio
async def test_switch_preflight_stale_work_and_receipt_failure_cannot_stop_original(environment, monkeypatch):
    database, commands, download, _ = environment
    await ReviewService(database).approve_candidate(1, 'fixture')
    await enqueue_work(database, download, 1, 'EXHENTAI', await command(commands))
    async def changed(*args):
        await commands.patch_metadata(1, await command(commands, key='other-edit', fields={'Title': 'Changed'}), 'fixture')
    monkeypatch.setattr(download, '_preflight_switch', changed)
    with pytest.raises(WorkCommandError) as stale:
        await enqueue_work(database, download, 1, 'TELEGRAPH', await command(commands, key='stale'), available={'TELEGRAPH'})
    assert stale.value.code == 'REVISION_CONFLICT'
    async def ready(*args):
        pass
    monkeypatch.setattr(download, '_preflight_switch', ready)
    def fail(*args):
        raise RuntimeError('Fixture receipt failure')
    monkeypatch.setattr(commands.__class__, '_save_receipt', staticmethod(fail))
    with pytest.raises(RuntimeError, match='Fixture receipt failure'):
        await enqueue_work(database, download, 1, 'TELEGRAPH', await command(commands, key='rollback'), available={'TELEGRAPH'})
    with database.connection() as connection:
        assert connection.execute('SELECT provider, state FROM download_jobs').fetchall() == [('EXHENTAI', 'PENDING')]
        assert connection.execute('SELECT count(*) FROM queue_operations').fetchone() == (0,)
