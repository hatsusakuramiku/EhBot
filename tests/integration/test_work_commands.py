"""P10 database edit contracts, independent of HTTP and browser state."""
import asyncio

import pytest
import pytest_asyncio

from app.db.database import Database
from app.review.commands import WorkCommandError, WorkCommands
from app.review.service import ReviewError


@pytest_asyncio.fixture
async def environment(tmp_path):
    database = Database(tmp_path / 'works.db')
    await database.initialize()
    with database.connection() as connection:
        connection.execute("INSERT INTO candidates(id, status) VALUES(1, 'PENDING_REVIEW')")
        connection.execute("INSERT INTO candidates(id, status) VALUES(2, 'PENDING_REVIEW')")
    return database, WorkCommands(database)


async def command(service, candidate_id=1, key='edit', **changes):
    version = await service.version(candidate_id)
    return {'work_id': version['work_id'], 'expected_revision': version['revision'],
            'idempotency_key': key, **changes}


@pytest.mark.asyncio
async def test_concurrent_edit_of_same_version_has_one_winner(environment):
    database, service = environment
    first = await command(service, fields={'Title': 'First'})
    second = {**first, 'idempotency_key': 'second', 'fields': {'Title': 'Second'}}
    results = await asyncio.gather(service.patch_metadata(1, first, 'admin'),
                                   service.patch_metadata(1, second, 'admin'), return_exceptions=True)
    assert sum(isinstance(result, dict) for result in results) == 1
    conflict = next(result for result in results if isinstance(result, WorkCommandError))
    assert conflict.code == 'REVISION_CONFLICT' and conflict.status_code == 409
    with database.connection() as connection:
        assert connection.execute('SELECT count(*) FROM review_actions').fetchone()[0] == 1
        assert connection.execute('SELECT count(*) FROM work_command_receipts').fetchone()[0] == 1


@pytest.mark.asyncio
async def test_replay_returns_original_result_before_current_version_check(environment):
    database, service = environment
    payload = await command(service, fields={'Title': 'First'})
    original = await service.patch_metadata(1, payload, 'admin')
    later = await command(service, key='later', fields={'Title': 'Later'})
    await service.patch_metadata(1, later, 'admin')
    assert await service.patch_metadata(1, payload, 'renamed-admin') == original
    assert (await database.effective_metadata(1))['Title'] == 'Later'
    with pytest.raises(WorkCommandError, match='幂等键') as failure:
        await service.patch_metadata(1, {**payload, 'fields': {'Title': 'Changed'}}, 'admin')
    assert failure.value.code == 'IDEMPOTENCY_CONFLICT'


@pytest.mark.asyncio
async def test_all_fields_and_locks_roll_back_when_later_lock_fails(environment):
    database, service = environment
    before = await service.version(1)
    payload = await command(service, fields={'Title': 'Must roll back'}, locks={'Rating': True})
    with pytest.raises(WorkCommandError) as failure:
        await service.patch_metadata(1, payload, 'admin')
    assert failure.value.code == 'METADATA_VALUE_NOT_FOUND'
    assert await database.effective_metadata(1) == {}
    assert await service.version(1) == before
    with database.connection() as connection:
        assert connection.execute('SELECT count(*) FROM review_actions').fetchone()[0] == 0
        assert connection.execute('SELECT count(*) FROM work_command_receipts').fetchone()[0] == 0
    # A rejected key was never consumed, so the administrator can correct it.
    fixed = {**payload, 'locks': {'Title': True}}
    await service.patch_metadata(1, fixed, 'admin')
    with database.connection() as connection:
        assert connection.execute("SELECT is_locked FROM metadata_values WHERE field_name='Title'").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_invalid_second_value_never_saves_the_first_field(environment):
    database, service = environment
    payload = await command(service, fields={'Title': 'Must not save', 'Rating': 'wrong'})
    with pytest.raises(ReviewError) as failure:
        await service.patch_metadata(1, payload, 'admin')
    assert failure.value.code == 'METADATA_VALUE_INVALID'
    assert await database.effective_metadata(1) == {}


@pytest.mark.asyncio
async def test_background_metadata_conflicts_but_timestamps_do_not(environment):
    database, service = environment
    payload = await command(service, fields={'Title': 'Draft'})
    with database.connection() as connection:
        connection.execute("UPDATE candidates SET updated_at = '2000-01-01' WHERE id = 1")
    assert (await service.version(1))['revision'] == payload['expected_revision']
    with database.connection() as connection:
        connection.execute("INSERT INTO metadata_values(candidate_id,field_name,field_value,value_source) "
                           "VALUES(1,'Title','Fetched','EXHENTAI')")
    with pytest.raises(WorkCommandError) as failure:
        await service.patch_metadata(1, payload, 'admin')
    assert failure.value.code == 'REVISION_CONFLICT'
    assert (await database.effective_metadata(1))['Title'] == 'Fetched'


@pytest.mark.asyncio
async def test_independent_work_versions_allow_both_edits(environment):
    _, service = environment
    a = await command(service, 1, key='a', fields={'Title': 'A'})
    b = await command(service, 2, key='b', fields={'Title': 'B'})
    results = await asyncio.gather(service.patch_metadata(1, a, 'admin'), service.patch_metadata(2, b, 'admin'))
    assert [item['candidate_id'] for item in results] == [1, 2]


@pytest.mark.asyncio
async def test_deleted_numeric_id_reuse_cannot_receive_an_old_command(environment):
    database, service = environment
    pending = await command(service, fields={'Title': 'Old draft'})
    committed = {**pending, 'idempotency_key': 'committed', 'fields': {'Title': 'Old edit'}}
    original = await service.patch_metadata(1, committed, 'admin')
    with database.connection() as connection:
        connection.execute('DELETE FROM review_actions WHERE candidate_id = 1')
        connection.execute('DELETE FROM candidates WHERE id = 1')
        connection.execute("INSERT INTO candidates(id, status) VALUES(1,'PENDING_REVIEW')")
    assert (await service.version(1))['work_id'] != pending['work_id']
    with pytest.raises(WorkCommandError) as failure:
        await service.patch_metadata(1, pending, 'admin')
    assert failure.value.code == 'REVISION_CONFLICT'
    assert await service.patch_metadata(1, committed, 'admin') == original
    assert await database.effective_metadata(1) == {}


@pytest.mark.asyncio
async def test_missing_preconditions_fail_before_mutation(environment):
    database, service = environment
    with pytest.raises(WorkCommandError) as failure:
        await service.patch_metadata(1, {'fields': {'Title': 'Unversioned'}}, 'admin')
    assert failure.value.status_code == 428
    assert await database.effective_metadata(1) == {}
