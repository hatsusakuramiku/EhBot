"""Dedup confirms a frozen preview and persists child outcomes across interruption."""
import asyncio

import pytest

from app.downloads.archived import ArchivedWorkService
from app.downloads.dedup import apply_dedup, build_dedup_plan
from app.review.commands import WorkCommandError, WorkCommands
from tests.integration.test_downloaded_web import Library, _duplicate_pair
from tests.integration.test_downloaded_web import make_settings


async def environment(tmp_path):
    settings = make_settings(tmp_path)
    library = await asyncio.to_thread(Library, settings)
    database = library.database
    keeper, loser = _duplicate_pair(library)
    archived = ArchivedWorkService(database, settings.library_path, settings.work_path)
    return database, library, archived, keeper, loser


@pytest.mark.asyncio
async def test_concurrent_confirm_and_replay_keep_original_result_after_cleanup(tmp_path):
    database, library, archived, keeper, loser = await environment(tmp_path)
    plan = await build_dedup_plan(database)
    params = {'plan_id': plan['plan_id'], 'idempotency_key': 'dedup'}
    first, second = await asyncio.gather(*(apply_dedup(database, archived, None, **params) for _ in range(2)))
    assert first == second and [item['candidate_id'] for item in first['removed']] == [loser]
    await archived.process_removals()
    assert await apply_dedup(database, archived, None, **params) == first
    assert await database.get_candidate(keeper) is not None and await database.get_candidate(loser) is None
    with database.connection() as connection:
        assert connection.execute('SELECT count(*) FROM work_command_receipts').fetchone() == (1,)


@pytest.mark.asyncio
@pytest.mark.parametrize('change_keeper', [False, True])
async def test_changed_preview_member_prevents_removal_and_refusal_is_fixed(tmp_path, change_keeper):
    database, library, archived, keeper, loser = await environment(tmp_path)
    plan = await build_dedup_plan(database)
    changed = keeper if change_keeper else loser
    with database.connection() as connection:
        connection.execute("UPDATE candidates SET status = 'REJECTED' WHERE id = ?", (changed,))
    result = await apply_dedup(database, archived, None, plan_id=plan['plan_id'], idempotency_key='stale')
    assert result['removed'] == [] and result['skipped'][0]['code'] == 'REVISION_CONFLICT'
    with database.connection() as connection:
        connection.execute("UPDATE candidates SET status = 'DOWNLOADED' WHERE id = ?", (changed,))
    assert await apply_dedup(database, archived, None, plan_id=plan['plan_id'], idempotency_key='stale') == result
    assert await database.get_candidate(loser) is not None


@pytest.mark.asyncio
async def test_later_duplicate_is_not_added_to_confirmed_plan(tmp_path):
    database, library, archived, keeper, loser = await environment(tmp_path)
    plan = await build_dedup_plan(database)
    later = library.add(title='Later duplicate', ex_gid=77001)
    result = await apply_dedup(database, archived, None, plan_id=plan['plan_id'], idempotency_key='fixed')
    assert [item['candidate_id'] for item in result['removed']] == [loser]
    assert await database.get_candidate(later) is not None


@pytest.mark.asyncio
async def test_interrupted_batch_resumes_without_repeating_committed_child(tmp_path, monkeypatch):
    database, library, archived, keeper, loser = await environment(tmp_path)
    later = library.add(title='Second loser', ex_gid=77001)
    plan = await build_dedup_plan(database)
    import app.downloads.dedup as module
    original = module.remove_work

    async def interrupt(database, archived, candidate_id, *args):
        if candidate_id == later:
            raise RuntimeError('Fixture request disconnected')
        return await original(database, archived, candidate_id, *args)

    monkeypatch.setattr(module, 'remove_work', interrupt)
    with pytest.raises(RuntimeError):
        await apply_dedup(database, archived, None, plan_id=plan['plan_id'], idempotency_key='resume')
    monkeypatch.setattr(module, 'remove_work', original)
    result = await apply_dedup(database, archived, None, plan_id=plan['plan_id'], idempotency_key='resume')
    assert {item['candidate_id'] for item in result['removed']} == {loser, later}
    with database.connection() as connection:
        assert connection.execute('SELECT count(*) FROM work_command_receipts').fetchone() == (2,)


@pytest.mark.asyncio
async def test_final_keeper_check_and_removal_receipt_failure_roll_back(tmp_path, monkeypatch):
    database, library, archived, keeper, loser = await environment(tmp_path)
    plan = await build_dedup_plan(database)
    original = WorkCommands.remove

    async def change(self, *args):
        with database.connection() as connection:
            connection.execute("UPDATE candidates SET status = 'REJECTED' WHERE id = ?", (keeper,))
        return await original(self, *args)

    monkeypatch.setattr(WorkCommands, 'remove', change)
    result = await apply_dedup(database, archived, None, plan_id=plan['plan_id'], idempotency_key='late-change')
    assert result['removed'] == [] and result['skipped'][0]['code'] == 'REVISION_CONFLICT'
    assert await database.get_candidate(loser) is not None
    monkeypatch.setattr(WorkCommands, 'remove', original)
    plan = await build_dedup_plan(database)

    def fail(*args):
        raise RuntimeError('Fixture receipt commit failure')

    monkeypatch.setattr(WorkCommands, '_save_receipt', staticmethod(fail))
    with pytest.raises(RuntimeError):
        await apply_dedup(database, archived, None, plan_id=plan['plan_id'], idempotency_key='rollback')
    assert await database.get_candidate(loser) is not None
    with database.connection() as connection:
        assert connection.execute('SELECT count(*) FROM work_command_receipts').fetchone() == (0,)


@pytest.mark.asyncio
async def test_missing_plan_and_key_reuse_never_recompute_current_library(tmp_path):
    database, library, archived, keeper, loser = await environment(tmp_path)
    with pytest.raises(WorkCommandError) as error:
        await apply_dedup(database, archived, None)
    assert error.value.status_code == 428
    first, second = await build_dedup_plan(database), await build_dedup_plan(database)
    await apply_dedup(database, archived, None, plan_id=first['plan_id'], idempotency_key='one-key')
    with pytest.raises(WorkCommandError) as error:
        await apply_dedup(database, archived, None, plan_id=second['plan_id'], idempotency_key='one-key')
    assert error.value.code == 'IDEMPOTENCY_CONFLICT'
