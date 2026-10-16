"""Downloaded management commands share the queue and work receipt transaction."""
import asyncio

import pytest

from app.downloads.archived import ArchivedWorkService
from app.review.application import downloaded_task
from app.review.commands import WorkCommandError, WorkCommands
from tests.integration.test_artifact_pipeline import prepare, packer, downloader


async def environment(tmp_path):
    database, old, candidate_id = await prepare(tmp_path)
    archived = ArchivedWorkService(database, tmp_path / 'library', tmp_path / 'work')
    return database, archived, packer(database, tmp_path), candidate_id


async def payload(database, candidate_id=1, key='downloaded'):
    version = await WorkCommands(database).version(candidate_id)
    return {'work_id': version['work_id'], 'expected_revision': version['revision'], 'idempotency_key': key}


@pytest.mark.asyncio
async def test_redownload_is_atomic_keeps_pack_intent_and_does_not_revive_on_replay(tmp_path):
    database, archived, pack, candidate_id = await environment(tmp_path)
    observed = {**await payload(database, candidate_id), 'repack': True}
    first, second = await asyncio.gather(*(downloaded_task(database, archived, pack,
        candidate_id, 'redownload', observed, 'admin') for _ in range(2)))
    assert first == second and first['repack']
    assert await downloader(database, tmp_path)._process_one()
    assert await downloaded_task(database, archived, pack, candidate_id, 'redownload', observed, 'admin') == first
    with database.connection() as connection:
        assert connection.execute('SELECT count(*) FROM work_command_receipts').fetchone() == (1,)
        assert connection.execute("SELECT j.state, p.pack_requested FROM download_jobs j "
            "JOIN queue_pack_intents p ON p.task_id = j.task_id WHERE j.id = ?", (first['job_id'],)).fetchone() == ('COMPLETED', 1)


@pytest.mark.asyncio
async def test_task_failure_after_receipt_rolls_back_restart_and_pack_intent(tmp_path, monkeypatch):
    database, archived, pack, candidate_id = await environment(tmp_path)
    observed = await payload(database, candidate_id)
    original = WorkCommands._save_receipt

    def fail(*args):
        original(*args)
        raise RuntimeError('Fixture failure after receipt')

    monkeypatch.setattr(WorkCommands, '_save_receipt', staticmethod(fail))
    with database.connection() as connection:
        task = connection.execute('SELECT task_id FROM download_jobs').fetchone()[0]
    with pytest.raises(RuntimeError):
        await downloaded_task(database, archived, pack, candidate_id, 'redownload', {**observed, 'repack': True}, 'admin')
    assert await payload(database, candidate_id) == observed
    with database.connection() as connection:
        assert connection.execute('SELECT task_id, state FROM download_jobs').fetchone() == (task, 'COMPLETED')
        assert connection.execute('SELECT count(*) FROM work_command_receipts').fetchone() == (0,)


@pytest.mark.asyncio
async def test_repack_commits_computed_pin_with_exact_task_and_receipt(tmp_path, monkeypatch):
    database, archived, pack, candidate_id = await environment(tmp_path)
    observed = await payload(database, candidate_id)
    from pathlib import Path

    async def planned(*args):
        return Path('New') / 'Fixture.cbz'

    monkeypatch.setattr(pack, 'planned_library_path', planned)
    first = await downloaded_task(database, archived, pack, candidate_id, 'repack', observed, 'admin')
    with database.connection() as connection:
        assert connection.execute('SELECT relative_path FROM work_archive_paths').fetchone() == ('New/Fixture.cbz',)
        assert connection.execute('SELECT json_extract(details_json, "$.input_artifact_id") FROM queue_tasks '
            'WHERE task_id = ?', (first['task_id'],)).fetchone()[0]
    assert await payload(database, candidate_id) != observed
    assert await downloaded_task(database, archived, pack, candidate_id, 'repack', observed, 'admin') == first


@pytest.mark.asyncio
async def test_repack_rollback_does_not_leave_computed_pin_or_task(tmp_path, monkeypatch):
    database, archived, pack, candidate_id = await environment(tmp_path)
    observed = await payload(database, candidate_id)

    def fail(*args):
        raise RuntimeError('Fixture commit failed')

    monkeypatch.setattr(WorkCommands, '_save_receipt', staticmethod(fail))
    with pytest.raises(RuntimeError):
        await downloaded_task(database, archived, pack, candidate_id, 'repack', observed, 'admin')
    assert await payload(database, candidate_id) == observed
    with database.connection() as connection:
        assert connection.execute('SELECT count(*) FROM work_archive_paths').fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM download_jobs WHERE provider = 'CONVERSION'").fetchone() == (0,)


@pytest.mark.asyncio
async def test_lock_free_path_preparation_cannot_use_changed_work(tmp_path, monkeypatch):
    database, archived, pack, candidate_id = await environment(tmp_path)
    observed = await payload(database, candidate_id)
    original = pack.planned_library_path

    async def change(*args):
        with database.connection() as connection:
            connection.execute("UPDATE candidates SET status = 'REJECTED' WHERE id = ?", (candidate_id,))
        return await original(*args)

    monkeypatch.setattr(pack, 'planned_library_path', change)
    with pytest.raises(WorkCommandError) as error:
        await downloaded_task(database, archived, pack, candidate_id, 'repack', observed, 'admin')
    assert error.value.code == 'REVISION_CONFLICT'
    with database.connection() as connection:
        assert connection.execute('SELECT count(*) FROM work_archive_paths').fetchone() == (0,)


@pytest.mark.asyncio
async def test_path_problem_is_parked_with_one_fixed_refusal(tmp_path, monkeypatch):
    database, archived, pack, candidate_id = await environment(tmp_path)
    observed = await payload(database, candidate_id)
    from app.conversion.naming import LibraryPathError

    async def invalid(*args):
        raise LibraryPathError('FIXTURE_PATH_INVALID', 'Fixture invalid path')

    monkeypatch.setattr(pack, 'planned_library_path', invalid)
    for _ in range(2):
        with pytest.raises(WorkCommandError) as error:
            await downloaded_task(database, archived, pack, candidate_id, 'repack', observed, 'admin')
        assert error.value.code == 'FIXTURE_PATH_INVALID'
    with database.connection() as connection:
        assert connection.execute("SELECT state FROM download_jobs WHERE provider = 'CONVERSION'").fetchall() == [('CONVERSION_WAITING_PATH',)]
        assert connection.execute('SELECT count(*) FROM work_command_receipts').fetchone() == (1,)


@pytest.mark.asyncio
async def test_downloaded_snapshot_joins_version_and_pin_change_invalidates_old_command(tmp_path):
    database, archived, pack, candidate_id = await environment(tmp_path)
    from app.api.downloaded import downloaded_snapshot
    snapshot = (await downloaded_snapshot(database))['works'][0]
    observed = await payload(database, candidate_id)
    assert snapshot['work_id'] == observed['work_id'] and snapshot['revision'] == observed['expected_revision']
    await database.set_archive_path_pin(candidate_id, 'Manual.cbz', is_manual=True, operator_name='admin')
    with pytest.raises(WorkCommandError) as error:
        await downloaded_task(database, archived, pack, candidate_id, 'redownload', observed, 'admin')
    assert error.value.code == 'REVISION_CONFLICT'
