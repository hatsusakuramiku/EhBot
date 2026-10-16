"""Concurrent and interrupted batches preserve their original child outcomes."""
import asyncio

import pytest

from app.downloads.service import DownloadError
from app.review.commands import WorkCommandError
from tests.integration.test_work_review_commands import environment


async def observed(commands, *ids):
    return {str(candidate_id): {'work_id': version['work_id'], 'expected_revision': version['revision']}
            for candidate_id in ids for version in [await commands.version(candidate_id)]}


@pytest.mark.asyncio
async def test_concurrent_same_batch_returns_identical_original_success(environment):
    database, commands, download, orchestrator = environment
    versions = await observed(commands, 1)
    first, second = await asyncio.gather(*(
        orchestrator.review_batch('approve', [1], versions, 'batch', 'admin') for _ in range(2)))
    assert first == second
    assert first['skipped'] == []
    assert len(first['applied'][0]['task_ids']) == 1
    with database.connection() as connection:
        assert connection.execute('SELECT count(*) FROM download_jobs').fetchone() == (1,)
        assert connection.execute('SELECT count(*) FROM review_actions').fetchone() == (1,)


@pytest.mark.asyncio
async def test_partial_batch_resumes_after_committed_child_and_preserves_original_result(environment, monkeypatch):
    database, commands, download, orchestrator = environment
    with database.connection() as connection:
        connection.execute("INSERT INTO candidates(id, status) VALUES(2, 'PENDING_REVIEW')")
    versions = await observed(commands, 1, 2)
    original = orchestrator.work_command

    async def interrupt(candidate_id, *args):
        if candidate_id == 2:
            raise RuntimeError('Fixture request interrupted after first child commit')
        return await original(candidate_id, *args)

    monkeypatch.setattr(orchestrator, 'work_command', interrupt)
    with pytest.raises(RuntimeError):
        await orchestrator.review_batch('reject', [1, 2], versions, 'batch', 'admin')
    with database.connection() as connection:
        connection.execute("UPDATE candidates SET status = 'PENDING_REVIEW' WHERE id = 1")
    monkeypatch.setattr(orchestrator, 'work_command', original)
    result = await orchestrator.review_batch('reject', [1, 2], versions, 'batch', 'renamed-admin')
    assert [row['candidate_id'] for row in result['applied']] == [1, 2]
    assert result['skipped'] == []
    with database.connection() as connection:
        assert connection.execute('SELECT count(*) FROM review_actions WHERE candidate_id = 1').fetchone() == (1,)
        assert connection.execute('SELECT status FROM candidates WHERE id = 1').fetchone() == ('PENDING_REVIEW',)
    assert await orchestrator.review_batch('reject', [1, 2], versions, 'batch', 'admin') == result


@pytest.mark.asyncio
async def test_stale_refusal_survives_later_state_and_integer_id_reuse(environment):
    database, commands, download, orchestrator = environment
    versions = await observed(commands, 1)
    with database.connection() as connection:
        connection.execute("UPDATE candidates SET status = 'REJECTED' WHERE id = 1")
    first = await orchestrator.review_batch('reject', [1], versions, 'batch', 'admin')
    assert first['skipped'][0]['code'] == 'REVISION_CONFLICT'
    with database.connection() as connection:
        connection.execute('DELETE FROM candidates WHERE id = 1')
        connection.execute("INSERT INTO candidates(id, status) VALUES(1, 'PENDING_REVIEW')")
    assert await orchestrator.review_batch('reject', [1], versions, 'batch', 'admin') == first
    with database.connection() as connection:
        assert connection.execute('SELECT status FROM candidates WHERE id = 1').fetchone() == ('PENDING_REVIEW',)


@pytest.mark.asyncio
async def test_refusal_committed_while_other_dispatch_prepares_cannot_become_success(environment, monkeypatch):
    database, commands, download, orchestrator = environment
    versions = await observed(commands, 1)
    original = download.prepare_work_download
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def prepare(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
            return await original(*args)
        raise DownloadError('FIXTURE_PREPARE_REFUSAL', 'Fixture source unavailable')

    monkeypatch.setattr(download, 'prepare_work_download', prepare)
    first = asyncio.create_task(orchestrator.review_batch('approve', [1], versions, 'batch', 'admin'))
    await entered.wait()
    try:
        second = await orchestrator.review_batch('approve', [1], versions, 'batch', 'admin')
    finally:
        release.set()
    assert await first == second
    assert second['applied'] == []
    assert second['skipped'][0]['code'] == 'FIXTURE_PREPARE_REFUSAL'
    with database.connection() as connection:
        assert connection.execute('SELECT count(*) FROM download_jobs').fetchone() == (0,)
        assert connection.execute('SELECT count(*) FROM review_actions').fetchone() == (0,)


@pytest.mark.asyncio
async def test_success_receipt_wins_before_late_batch_failure_is_saved(environment, monkeypatch):
    database, commands, download, orchestrator = environment
    versions = await observed(commands, 1)
    original = orchestrator.work_command

    async def lose_response(*args):
        await original(*args)
        raise DownloadError('FIXTURE_LOST_RESPONSE', 'Fixture after committed success')

    monkeypatch.setattr(orchestrator, 'work_command', lose_response)
    result = await orchestrator.review_batch('approve', [1], versions, 'batch', 'admin')
    assert result['skipped'] == []
    assert len(result['applied'][0]['job_ids']) == 1


@pytest.mark.asyncio
async def test_all_preconditions_validated_before_any_child_executes(environment):
    database, commands, download, orchestrator = environment
    versions = await observed(commands, 1)
    with pytest.raises(WorkCommandError) as error:
        await orchestrator.review_batch('reject', [1, 2], versions, 'batch', 'admin')
    assert error.value.status_code == 428
    with database.connection() as connection:
        assert connection.execute('SELECT count(*) FROM review_actions').fetchone() == (0,)
        assert connection.execute('SELECT count(*) FROM work_review_batches').fetchone() == (0,)


@pytest.mark.asyncio
async def test_batch_key_reuse_across_actions_versions_and_individual_commands_is_refused(environment):
    database, commands, download, orchestrator = environment
    versions = await observed(commands, 1)
    await orchestrator.review_batch('reject', [1], versions, 'batch', 'admin')
    for action, observed_versions in [('approve', versions), ('reject', await observed(commands, 1))]:
        with pytest.raises(WorkCommandError) as error:
            await orchestrator.review_batch(action, [1], observed_versions, 'batch', 'admin')
        assert error.value.code == 'IDEMPOTENCY_CONFLICT'
    payload = {**(await observed(commands, 1))['1'], 'idempotency_key': 'batch'}
    with pytest.raises(WorkCommandError) as error:
        await orchestrator.work_command(1, 'requeue', payload, 'admin')
    assert error.value.code == 'IDEMPOTENCY_CONFLICT'
    await orchestrator.work_command(1, 'requeue', {**payload, 'idempotency_key': 'single'}, 'admin')
    with pytest.raises(WorkCommandError) as error:
        await orchestrator.review_batch('reject', [1], await observed(commands, 1), 'single', 'admin')
    assert error.value.code == 'IDEMPOTENCY_CONFLICT'


@pytest.mark.asyncio
async def test_unknown_work_identity_is_fixed_refusal_without_creating_fake_work(environment):
    database, commands, download, orchestrator = environment
    versions = {'99': {'work_id': 'unknown', 'expected_revision': 1}}
    result = await orchestrator.review_batch('reject', [99], versions, 'batch', 'admin')
    assert result['skipped'][0]['code'] == 'CANDIDATE_NOT_FOUND'
    with database.connection() as connection:
        assert connection.execute('SELECT count(*) FROM work_versions').fetchone() == (1,)
    assert await orchestrator.review_batch('reject', [99], versions, 'batch', 'admin') == result
