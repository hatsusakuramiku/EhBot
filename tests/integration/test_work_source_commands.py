"""Manual source edits keep one work identity and one durable command result."""
import asyncio

import pytest

from app.candidates.references import process_reference_changes
from app.candidates.operations import get_operation
from app.review.commands import WorkCommands, WorkCommandError
from app.review.source_commands import SourceCommands
from tests.integration.test_work_commands import environment, command


CASES = [('preview', {'preview_url': 'https://telegra.ph/fixture'}),
         ('torrent', {'magnet': 'magnet:?xt=urn:btih:' + 'a' * 40}),
         ('eh-ref', {'ex_gid': 'https://exhentai.org/g/123/fixture'})]


@pytest.mark.asyncio
@pytest.mark.parametrize('action,values', CASES)
async def test_same_key_concurrent_replay_and_other_device_conflict(environment, action, values):
    database, works = environment
    service = SourceCommands(database)
    body = await command(works, **values)
    first, second = await asyncio.gather(service.execute(1, action, body), service.execute(1, action, body))
    assert first == second and first['revision'] > body['expected_revision']
    await works.patch_metadata(1, await command(works, key='later', fields={'Title': 'Later'}), 'Owner')
    assert await service.execute(1, action, body) == first
    with pytest.raises(WorkCommandError) as error:
        await service.execute(1, action, {**body, 'idempotency_key': 'other-device'})
    assert error.value.code == 'REVISION_CONFLICT'
    with pytest.raises(WorkCommandError) as error:
        await service.execute(1, action, {**body, next(iter(values)): ''})
    assert error.value.code == 'IDEMPOTENCY_CONFLICT'


@pytest.mark.asyncio
@pytest.mark.parametrize('action,values', CASES)
async def test_receipt_failure_rolls_back_source_stop_and_identity(environment, action, values, monkeypatch):
    database, works = environment
    before = await works.version(1)
    def fail(*args):
        raise RuntimeError('Fixture receipt failed')
    monkeypatch.setattr(WorkCommands, '_save_receipt', staticmethod(fail))
    with pytest.raises(RuntimeError, match='Fixture receipt failed'):
        await SourceCommands(database).execute(1, action, await command(works, **values))
    assert await works.version(1) == before
    with database.connection() as connection:
        assert connection.execute('SELECT ex_gid, preview_url, magnet_url FROM candidates WHERE id = 1').fetchone() == (None, None, None)
        assert connection.execute('SELECT count(*) FROM ingest_reference_changes').fetchone() == (0,)


@pytest.mark.asyncio
@pytest.mark.parametrize('action,values', CASES)
async def test_deleted_numeric_id_reuse_only_replays_original_result(environment, action, values):
    database, works = environment
    service = SourceCommands(database)
    body = await command(works, **values)
    first = await service.execute(1, action, body)
    with database.connection() as connection:
        connection.execute('DELETE FROM review_actions WHERE candidate_id = 1')
        connection.execute('DELETE FROM candidates WHERE id = 1')
        connection.execute("INSERT INTO candidates(id, status) VALUES(1, 'PENDING_REVIEW')")
    assert await service.execute(1, action, body) == first
    with pytest.raises(WorkCommandError) as error:
        await service.execute(1, action, {**body, 'idempotency_key': 'new-request'})
    assert error.value.code == 'REVISION_CONFLICT'
    with database.connection() as connection:
        assert connection.execute('SELECT ex_gid, preview_url, magnet_url FROM candidates WHERE id = 1').fetchone() == (None, None, None)


@pytest.mark.asyncio
async def test_gallery_merge_requires_explicit_target_version_and_replays_after_source_is_deleted(environment):
    database, works = environment
    with database.connection() as connection:
        connection.execute('UPDATE candidates SET ex_gid = 123 WHERE id = 2')
    service = SourceCommands(database)
    body = await command(works, **CASES[2][1])
    with pytest.raises(WorkCommandError) as error:
        await service.execute(1, 'eh-ref', body)
    assert error.value.code == 'MERGE_TARGET_CHANGED' and error.value.details['target']['candidate_id'] == 2
    target = await works.version(2)
    body.update(target_work_id=target['work_id'], target_revision=target['revision'])
    first = await service.execute(1, 'eh-ref', body)
    assert first['result_candidate_id'] == 2 and first['result_work_id'] == target['work_id']
    assert await database.get_candidate(1) is None
    assert await service.execute(1, 'eh-ref', body) == first


@pytest.mark.asyncio
@pytest.mark.parametrize('target_exists', [False, True])
async def test_target_change_while_source_stops_is_fixed_failure_and_not_retryable(environment, target_exists):
    database, works = environment
    with database.connection() as connection:
        if target_exists:
            connection.execute('UPDATE candidates SET ex_gid = 123 WHERE id = 2')
        connection.execute("INSERT INTO download_jobs(candidate_id, provider, state, idempotency_key) VALUES(1, 'EXHENTAI', 'DOWNLOADING', 'running')")
    body = await command(works, **CASES[2][1])
    if target_exists:
        target = await works.version(2)
        body.update(target_work_id=target['work_id'], target_revision=target['revision'])
    service = SourceCommands(database)
    accepted = await service.execute(1, 'eh-ref', body)
    assert accepted['status'] == 'PENDING'
    with database.connection() as connection:
        connection.execute("UPDATE candidates SET ex_gid = 123, status = 'REJECTED' WHERE id = 2")
        connection.execute("UPDATE download_jobs SET state = 'CANCELLED' WHERE candidate_id = 1")
        connection.execute("UPDATE queue_operations SET status = 'COMPLETED'")
    results = await asyncio.to_thread(process_reference_changes, database)
    assert results[0]['status'] == 'FAILED' and results[0]['error_code'] == 'INGEST_REFERENCE_TARGET_CHANGED'
    operation = await asyncio.to_thread(get_operation, database, accepted['operation_id'])
    assert not operation['retryable']
    assert await database.get_candidate(1) is not None
    assert await service.execute(1, 'eh-ref', body) == accepted
