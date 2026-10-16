"""Versioned removals retain one acceptance across cleanup and row reuse."""
import asyncio

import pytest

from app.downloads.archived import ArchivedWorkService
from app.review.application import remove_work
from app.review.batches import ReviewBatches
from app.review.commands import WorkCommandError, WorkCommands
from tests.integration.test_queue_control import database, pending


def service(database, tmp_path):
    return ArchivedWorkService(database, tmp_path / 'library', tmp_path)


async def observed(database, candidate_id=1, key='purge'):
    version = await WorkCommands(database).version(candidate_id)
    return {'work_id': version['work_id'], 'expected_revision': version['revision'], 'idempotency_key': key}


@pytest.mark.asyncio
async def test_concurrent_same_key_removal_has_one_intent_and_replays_after_cleanup_and_id_reuse(database, tmp_path):
    pending(database)
    archived = service(database, tmp_path)
    payload = await observed(database)
    first, second = await asyncio.gather(*(
        remove_work(database, archived, 1, 'delete-files', payload, 'admin') for _ in range(2)))
    assert first == second and first['cleanup_status'] == 'PENDING'
    await archived.process_removals()
    assert (await archived.get_removal(first['removal_id']))['status'] == 'COMPLETED'
    with database.connection() as connection:
        connection.execute("INSERT INTO candidates(id, status) VALUES(1, 'PENDING_REVIEW')")
        assert connection.execute('SELECT count(*) FROM queue_removals').fetchone() == (1,)
        assert connection.execute('SELECT count(*) FROM work_command_receipts').fetchone() == (1,)
    assert await remove_work(database, archived, 1, 'delete-files', payload, 'renamed') == first
    assert await database.get_candidate(1) is not None
    with pytest.raises(WorkCommandError, match='作品已变化'):
        await remove_work(database, archived, 1, 'delete-files', {**payload, 'idempotency_key': 'new'}, 'admin')


@pytest.mark.asyncio
async def test_different_keys_from_same_snapshot_have_one_winner(database, tmp_path):
    archived = service(database, tmp_path)
    pending(database)
    payload = await observed(database)
    results = await asyncio.gather(*(remove_work(database, archived, 1, 'remove',
        {**payload, 'idempotency_key': key}, 'admin') for key in ('a', 'b')), return_exceptions=True)
    assert len([item for item in results if isinstance(item, dict)]) == 1
    assert [item.code for item in results if isinstance(item, WorkCommandError)] == ['REVISION_CONFLICT']
    assert await database.get_candidate(1) is not None


@pytest.mark.asyncio
async def test_error_after_removal_rolls_back_records_stops_tombstone_and_receipt(database, tmp_path, monkeypatch):
    pending(database)
    archived = service(database, tmp_path)
    payload = await observed(database)
    original = WorkCommands._save_receipt

    def fail(*args):
        original(*args)
        raise RuntimeError('Fixture failure after receipt')

    monkeypatch.setattr(WorkCommands, '_save_receipt', staticmethod(fail))
    with pytest.raises(RuntimeError):
        await remove_work(database, archived, 1, 'delete-files', payload, 'admin')
    assert await observed(database) == payload
    with database.connection() as connection:
        assert connection.execute('SELECT count(*) FROM download_jobs').fetchone() == (1,)
        for table in ('queue_removals', 'queue_operations', 'work_command_receipts'):
            assert connection.execute('SELECT count(*) FROM ' + table).fetchone() == (0,)


@pytest.mark.asyncio
async def test_version_checked_again_after_lock_free_preparation(database, tmp_path, monkeypatch):
    archived = service(database, tmp_path)
    payload = await observed(database)
    original = archived._roots

    async def change():
        with database.connection() as connection:
            connection.execute("UPDATE candidates SET status = 'REJECTED' WHERE id = 1")
        return await original()

    monkeypatch.setattr(archived, '_roots', change)
    with pytest.raises(WorkCommandError) as error:
        await remove_work(database, archived, 1, 'delete', payload, 'admin')
    assert error.value.code == 'REVISION_CONFLICT'
    assert await database.get_candidate(1) is not None


@pytest.mark.asyncio
async def test_batch_resumes_after_deleted_child_and_keeps_refusal_on_reused_id(database, tmp_path, monkeypatch):
    archived = service(database, tmp_path)
    batches = ReviewBatches(database, archived=archived)
    with database.connection() as connection:
        connection.execute("INSERT INTO candidates(id, status) VALUES(2, 'PENDING_REVIEW')")
    versions = {str(i): await observed(database, i) for i in (1, 2)}
    original = archived._roots
    calls = 0

    async def interrupt():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError('Fixture disconnected after first accepted deletion')
        return await original()

    monkeypatch.setattr(archived, '_roots', interrupt)
    with pytest.raises(RuntimeError):
        await batches.run('delete', [1, 2], versions, 'batch', 'admin')
    assert await database.get_candidate(1) is None
    with database.connection() as connection:
        connection.execute("INSERT INTO candidates(id, status) VALUES(1, 'PENDING_REVIEW')")
        connection.execute("UPDATE candidates SET status = 'REJECTED' WHERE id = 2")
    monkeypatch.setattr(archived, '_roots', original)
    result = await batches.run('delete', [1, 2], versions, 'batch', 'admin')
    assert [item['candidate_id'] for item in result['applied']] == [1]
    assert result['skipped'][0]['code'] == 'REVISION_CONFLICT'
    assert await database.get_candidate(1) is not None
    assert await batches.run('delete', [1, 2], versions, 'batch', 'admin') == result


@pytest.mark.asyncio
async def test_old_receipt_precedes_unavailable_roots_and_conflicting_payload_is_rejected(database, tmp_path, monkeypatch):
    archived = service(database, tmp_path)
    payload = await observed(database)
    first = await remove_work(database, archived, 1, 'delete', payload, 'admin')

    async def broken():
        raise RuntimeError('Fixture root lookup must not run on replay')

    monkeypatch.setattr(archived, '_roots', broken)
    assert await remove_work(database, archived, 1, 'delete', payload, 'admin') == first
    with pytest.raises(WorkCommandError) as error:
        await remove_work(database, archived, 1, 'delete-files', payload, 'admin')
    assert error.value.code == 'IDEMPOTENCY_CONFLICT'
