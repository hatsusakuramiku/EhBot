"""Batch outcomes and path preparation verified against the real queue database."""
from pathlib import Path

import pytest

from app.api.contracts import ApiError
from app.api.downloaded import apply_downloaded_batch
from app.archive.service import PATH_SOURCE_AI
from app.review.commands import WorkCommands
from tests.integration.test_downloaded_commands import environment, payload


async def run(database, archived, pack, candidate_id, action='remove', key='batch', ids=None, **options):
    ids = ids if ids is not None else [candidate_id]
    versions = {str(i): await payload(database, i) for i in ids}
    options.setdefault('versions', versions)
    return await apply_downloaded_batch(archived, pack, action, ids, idempotency_key=key, **options)


@pytest.mark.asyncio
async def test_unknown_action_and_empty_selection_refuse_before_mutation(tmp_path):
    database, archived, pack, candidate_id = await environment(tmp_path)
    for action, ids, code in (('delete', [candidate_id], 'ACTION_UNKNOWN'), ('remove', [], 'CANDIDATE_IDS_INVALID')):
        with pytest.raises(ApiError) as error:
            await run(database, archived, pack, candidate_id, action, ids=ids)
        assert error.value.code == code
    assert await database.downloaded_work(candidate_id) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize('delete_files', [False, True])
async def test_removal_file_decision_is_opt_in_and_original_result_is_fixed(tmp_path, delete_files):
    database, archived, pack, candidate_id = await environment(tmp_path)
    work = await database.downloaded_work(candidate_id)
    versions = {str(candidate_id): await payload(database, candidate_id)}
    first = await run(database, archived, pack, candidate_id, delete_files=delete_files, versions=versions)
    assert first['delete_files'] is delete_files
    assert first['skipped'] == []
    assert first['applied'][0]['result']['removed_jobs'] == 1
    assert Path(work.archive_path).exists()
    await archived.process_removals()
    assert Path(work.archive_path).exists() is not delete_files
    assert await run(database, archived, pack, candidate_id, delete_files=delete_files, versions=versions) == first


@pytest.mark.asyncio
async def test_one_missing_download_is_a_fixed_skip_and_does_not_refuse_the_rest(tmp_path):
    database, archived, pack, candidate_id = await environment(tmp_path)
    with database.connection() as connection:
        connection.execute("INSERT INTO candidates(id, status) VALUES(2, 'APPROVED')")
    versions = {str(i): await payload(database, i) for i in (candidate_id, 2)}
    first = await run(database, archived, pack, candidate_id, ids=[candidate_id, 2], versions=versions)
    assert [item['candidate_id'] for item in first['applied']] == [candidate_id]
    assert first['skipped'][0]['code'] == 'WORK_NOT_DOWNLOADED'
    assert await run(database, archived, pack, candidate_id, ids=[candidate_id, 2], versions=versions) == first


@pytest.mark.asyncio
async def test_unexpected_preparation_error_is_not_disguised_as_skip_and_can_resume(tmp_path, monkeypatch):
    database, archived, pack, candidate_id = await environment(tmp_path)
    original = archived._roots
    versions = {str(candidate_id): await payload(database, candidate_id)}

    async def broken():
        raise OSError('Fixture disk unavailable')

    monkeypatch.setattr(archived, '_roots', broken)
    with pytest.raises(OSError):
        await run(database, archived, pack, candidate_id, versions=versions)
    assert await database.downloaded_work(candidate_id) is not None
    monkeypatch.setattr(archived, '_roots', original)
    assert len((await run(database, archived, pack, candidate_id, versions=versions))['applied']) == 1


@pytest.mark.asyncio
async def test_each_applied_work_is_announced_once_per_response(tmp_path):
    database, archived, pack, candidate_id = await environment(tmp_path)
    with database.connection() as connection:
        connection.execute("INSERT INTO candidates(id, status) VALUES(2, 'APPROVED')")
    announced = []
    await run(database, archived, pack, candidate_id, ids=[candidate_id, 2], announce=announced.append)
    assert announced == [candidate_id]


@pytest.mark.asyncio
async def test_redownload_carries_pack_choice_and_replay_does_not_revive_later_terminal_task(tmp_path):
    database, archived, pack, candidate_id = await environment(tmp_path)
    versions = {str(candidate_id): await payload(database, candidate_id)}
    first = await run(database, archived, pack, candidate_id, 'redownload', repack=True, versions=versions)
    assert first['applied'][0]['result']['repack'] is True
    assert first['delete_files'] is False
    with database.connection() as connection:
        connection.execute("UPDATE download_jobs SET state = 'FAILED' WHERE candidate_id = ?", (candidate_id,))
    assert await run(database, archived, pack, candidate_id, 'redownload', repack=True, versions=versions) == first
    with database.connection() as connection:
        assert connection.execute('SELECT state FROM download_jobs').fetchone() == ('FAILED',)


@pytest.mark.asyncio
async def test_manual_path_is_preserved_and_needs_no_metadata_request(tmp_path, monkeypatch):
    database, archived, pack, candidate_id = await environment(tmp_path)
    await database.set_archive_path_pin(candidate_id, 'Manual/Book.cbz', is_manual=True, operator_name='admin')

    async def forbidden(*args):
        raise AssertionError('Manual paths need no scrape or computed name')

    monkeypatch.setattr(pack, 'ensure_metadata', forbidden)
    monkeypatch.setattr(pack, 'planned_library_path', forbidden)
    result = await run(database, archived, pack, candidate_id, 'repack', delete_files=True)
    assert len(result['applied']) == 1 and result['delete_files'] is False
    with database.connection() as connection:
        assert connection.execute('SELECT relative_path, is_manual FROM work_archive_paths').fetchone() == ('Manual/Book.cbz', 1)


@pytest.mark.asyncio
@pytest.mark.parametrize('cached', [True, False])
async def test_repack_uses_only_current_ai_cache_and_defers_missing_answer_to_worker(tmp_path, monkeypatch, cached):
    database, archived, pack, candidate_id = await environment(tmp_path)

    async def source():
        return PATH_SOURCE_AI

    async def answer(*args):
        return Path('AI/Book.cbz') if cached else None

    monkeypatch.setattr(pack, 'path_source', source)
    monkeypatch.setattr(pack, 'current_ai_path', answer)
    result = await run(database, archived, pack, candidate_id, 'repack')
    assert len(result['applied']) == 1
    with database.connection() as connection:
        pins = connection.execute('SELECT relative_path FROM work_archive_paths').fetchall()
    assert pins == ([('AI/Book.cbz',)] if cached else [])


@pytest.mark.asyncio
async def test_metadata_precedes_name_preparation_and_pin_task_receipt_commit_together(tmp_path, monkeypatch):
    database, archived, pack, candidate_id = await environment(tmp_path)
    order = []

    async def metadata(*args):
        order.append('metadata')
        with database.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')

    async def path(*args):
        order.append('path')
        return Path('Computed/Book.cbz')

    monkeypatch.setattr(pack, 'ensure_metadata', metadata)
    monkeypatch.setattr(pack, 'planned_library_path', path)
    first = await run(database, archived, pack, candidate_id, 'repack')
    assert order == ['metadata', 'path'] and len(first['applied']) == 1
    assert (await WorkCommands(database).version(candidate_id))['revision'] == first['applied'][0]['result']['work_revision']


@pytest.mark.asyncio
async def test_remove_and_redownload_do_not_derive_a_path(tmp_path, monkeypatch):
    for index, action in enumerate(('remove', 'redownload')):
        database, archived, pack, candidate_id = await environment(tmp_path / str(index))

        async def forbidden(*args):
            raise AssertionError('This action must not compute a path')

        monkeypatch.setattr(pack, 'planned_library_path', forbidden)
        assert len((await run(database, archived, pack, candidate_id, action))['applied']) == 1
