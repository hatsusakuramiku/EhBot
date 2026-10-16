"""Fixed rearchive batches share durable item receipts and real move journals."""
import asyncio
from contextlib import contextmanager
import threading
from uuid import uuid4

import pytest

from app.archive.maintenance import RearchiveCommands
from app.downloads.archived import ArchivedWorkService
from app.review.commands import WorkCommandError, WorkCommands
from app.settings.commands import SettingsCommands
from tests.integration.test_artifact_pipeline import prepare, packer
from tests.integration.test_work_commands import command


def observed(database, scope='rearchive', **values):
    with database.connection() as connection:
        revision = connection.execute('SELECT revision FROM settings_versions WHERE scope = ?', (scope,)).fetchone()[0]
    return {'expected_revision': revision, 'idempotency_key': uuid4().hex, **values}


async def environment(tmp_path, *, packed=True):
    database, _, candidate_id = await prepare(tmp_path)
    conversion = packer(database, tmp_path)
    if packed:
        await conversion.enqueue_for_candidate(candidate_id)
        assert await conversion._process_one()
    archived = ArchivedWorkService(database, tmp_path / 'library', tmp_path / 'work')
    await conversion._settings.save_library_template('Moved/{title}')
    return database, conversion, archived, candidate_id


@pytest.mark.asyncio
async def test_real_move_two_same_key_callers_one_receipt_and_replay_after_edit(tmp_path):
    database, conversion, archived, candidate_id = await environment(tmp_path)
    commands = RearchiveCommands(database, archived, conversion)
    body = observed(database)
    outcomes = await asyncio.gather(commands.execute(body, 'Owner'), commands.execute(body, 'Owner'),
                                    return_exceptions=True)
    completed = [result for result in outcomes if not isinstance(result, BaseException)]
    assert completed
    first = completed[0]
    for result in outcomes:
        if isinstance(result, BaseException):
            assert isinstance(result, WorkCommandError) and result.code == 'PUBLICATION_RECOVERY_REQUIRED'
        else:
            assert result == first
    assert await commands.execute(body, 'Owner') == first and len(first['moved']) == 1
    assert (tmp_path / 'library/Moved/Archive Title.cbz').exists()
    assert not (tmp_path / 'library/Archive Title.cbz').exists()
    with database.connection() as connection:
        assert connection.execute('SELECT count(*) FROM queue_relocations').fetchone() == (1,)
        assert connection.execute('SELECT is_manual, operator_name FROM work_archive_paths').fetchone() == (0, 'Owner')
    works = WorkCommands(database)
    await works.patch_metadata(candidate_id, await command(works, candidate_id, fields={'Title': 'Later'}), 'Owner')
    assert await RearchiveCommands(database, archived, conversion).execute(body, 'Owner') == first


@pytest.mark.asyncio
async def test_preview_never_enriches_or_queues_and_actual_enqueue_has_exact_input(tmp_path):
    database, conversion, archived, _ = await environment(tmp_path, packed=False)
    async def forbidden(*args):
        raise AssertionError('maintenance planning cannot fetch metadata')
    conversion._metadata_enricher = forbidden
    commands = RearchiveCommands(database, archived, conversion)
    preview = await commands.execute(observed(database, dry_run=True), 'Owner')
    assert preview['dry_run'] and len(preview['queued']) == 1
    with database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM download_jobs WHERE provider = 'CONVERSION'").fetchone() == (0,)
        assert connection.execute('SELECT count(*) FROM work_archive_paths').fetchone() == (0,)
    result = await commands.execute(observed(database), 'Owner')
    assert not result['dry_run'] and len(result['queued']) == 1
    with database.connection() as connection:
        assert connection.execute("SELECT json_extract(details_json, '$.input_artifact_id') FROM queue_tasks WHERE kind = 'PACK'").fetchone()[0]


@pytest.mark.asyncio
async def test_stale_scope_during_planning_cannot_admit_or_move(tmp_path, monkeypatch):
    database, conversion, archived, candidate_id = await environment(tmp_path)
    original = conversion.planned_path_for_candidate
    async def changed(candidate):
        works = WorkCommands(database)
        await works.patch_metadata(candidate_id, await command(works, candidate_id, fields={'Title': 'Other device'}), 'other')
        return await original(candidate)
    monkeypatch.setattr(conversion, 'planned_path_for_candidate', changed)
    with pytest.raises(WorkCommandError) as failure:
        await RearchiveCommands(database, archived, conversion).execute(observed(database), 'Owner')
    assert failure.value.code == 'REVISION_CONFLICT'
    assert (tmp_path / 'library/Archive Title.cbz').exists()
    with database.connection() as connection:
        assert connection.execute('SELECT count(*) FROM work_review_batches').fetchone() == (0,)


@pytest.mark.asyncio
async def test_interrupted_batch_reuses_committed_child_after_database_reinitialize(tmp_path, monkeypatch):
    database, conversion, archived, _ = await environment(tmp_path)
    commands = RearchiveCommands(database, archived, conversion)
    body = observed(database)
    def interrupted(*args):
        raise RuntimeError('Fixture response boundary')
    monkeypatch.setattr(commands.batches, '_complete', interrupted)
    with pytest.raises(RuntimeError, match='Fixture response boundary'):
        await commands.execute(body, 'Owner')
    await database.initialize()
    partial = await commands.receipt(body['idempotency_key'])
    assert partial['status'] == 'INCOMPLETE' and partial['completed'] == 1 and partial['result'] is None
    assert len(partial['outcomes']) == 1
    assert (tmp_path / 'library/Moved/Archive Title.cbz').exists()
    assert len((await RearchiveCommands(database, archived, conversion).execute(body, 'Owner'))['moved']) == 1
    assert (await commands.receipt(body['idempotency_key']))['status'] == 'COMPLETED'
    with database.connection() as connection:
        assert connection.execute('SELECT count(*) FROM queue_relocations').fetchone() == (1,)


@pytest.mark.asyncio
async def test_cache_clear_cas_and_safe_replay_cannot_clear_new_answers(tmp_path):
    database, conversion, _, _ = await environment(tmp_path)
    commands = SettingsCommands(database, archive=conversion._settings)
    body = observed(database, 'path-cache')
    first = await commands.execute('path-cache-clear', body)
    assert first['removed'] == 0
    # Another accepted clear consumes the cache version, even when empty.
    await commands.execute('path-cache-clear', observed(database, 'path-cache'))
    assert await commands.execute('path-cache-clear', body) == first
    with pytest.raises(WorkCommandError) as failure:
        await commands.execute('path-cache-clear', {**body, 'idempotency_key': 'old-version'})
    assert failure.value.code == 'REVISION_CONFLICT'


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['template', 'metadata', 'answer', 'replacement'])
async def test_changes_after_admission_are_fixed_child_refusals(tmp_path, monkeypatch, change):
    from app.archive.maintenance import RearchiveItem
    database, conversion, archived, candidate_id = await environment(tmp_path)
    original = RearchiveItem.execute
    changed = False
    async def interfere(item, service, operator):
        nonlocal changed
        if not changed:
            changed = True
            if change == 'template':
                await conversion._settings.save_library_template('Other/{title}')
            else:
                with database.connection() as connection:
                    if change == 'metadata':
                        connection.execute("UPDATE metadata_values SET field_value = 'Later' WHERE candidate_id = ? AND field_name = 'Title'", (candidate_id,))
                    elif change == 'answer':
                        connection.execute("INSERT INTO ai_path_suggestions(candidate_id, fingerprint, prompt_hash, relative_path, directory, filename, model_name) "
                                           "VALUES(?, 'f', 'p', 'Later.cbz', '', 'Later', 'fixture')", (candidate_id,))
                    else:
                        connection.execute('DELETE FROM artifacts')
                        connection.execute('DELETE FROM download_jobs')
                        connection.execute('DELETE FROM metadata_values')
                        connection.execute('DELETE FROM review_actions')
                        connection.execute('DELETE FROM candidates WHERE id = ?', (candidate_id,))
                        connection.execute("INSERT INTO candidates(id, status) VALUES(?, 'PENDING_REVIEW')", (candidate_id,))
        return await original(item, service, operator)
    monkeypatch.setattr(RearchiveItem, 'execute', interfere)
    commands = RearchiveCommands(database, archived, conversion)
    body = observed(database)
    result = await commands.execute(body, 'Owner')
    assert len(result['skipped']) == 1 and result['skipped'][0]['code'] == 'REVISION_CONFLICT'
    assert (tmp_path / 'library/Archive Title.cbz').exists()
    assert not (tmp_path / 'library/Moved/Archive Title.cbz').exists()
    assert await commands.execute(body, 'Owner') == result


@pytest.mark.asyncio
@pytest.mark.parametrize('packed', [False, True])
async def test_child_receipt_failure_rolls_back_pins_jobs_and_file_moves(tmp_path, monkeypatch, packed):
    database, conversion, archived, _ = await environment(tmp_path, packed=packed)
    commands = RearchiveCommands(database, archived, conversion)
    body = observed(database)
    def fail(*args):
        raise RuntimeError('Fixture receipt failure')
    monkeypatch.setattr(WorkCommands, '_save_receipt', fail)
    with pytest.raises(RuntimeError, match='Fixture receipt failure'):
        await commands.execute(body, 'Owner')
    assert (await commands.receipt(body['idempotency_key']))['completed'] == 0
    with database.connection() as connection:
        assert connection.execute('SELECT count(*) FROM work_command_receipts').fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM download_jobs WHERE provider = 'CONVERSION' AND state = 'CONVERSION_PENDING'").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM work_archive_paths WHERE relative_path LIKE 'Moved/%'").fetchone() == (0,)
    if packed:
        assert (tmp_path / 'library/Archive Title.cbz').exists()
        assert not (tmp_path / 'library/Moved/Archive Title.cbz').exists()


@pytest.mark.asyncio
async def test_invalid_template_parks_only_when_actual_run_commits(tmp_path, monkeypatch):
    from app.conversion.naming import LibraryPathError
    database, conversion, archived, _ = await environment(tmp_path, packed=False)
    async def invalid(*args):
        raise LibraryPathError('PATH_UNUSABLE', 'Fixture invalid path')
    monkeypatch.setattr(conversion, 'planned_path_for_candidate', invalid)
    commands = RearchiveCommands(database, archived, conversion)
    preview = await commands.execute(observed(database, dry_run=True), 'Owner')
    assert preview['skipped'][0]['code'] == 'PATH_UNUSABLE'
    with database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM download_jobs WHERE provider = 'CONVERSION'").fetchone() == (0,)
    result = await commands.execute(observed(database), 'Owner')
    assert result['skipped'] == preview['skipped']
    with database.connection() as connection:
        assert connection.execute("SELECT state, error_code FROM download_jobs WHERE provider = 'CONVERSION'").fetchone() == ('CONVERSION_WAITING_PATH', 'PATH_UNUSABLE')


@pytest.mark.asyncio
async def test_duplicate_during_prepared_move_cannot_freeze_a_refusal(tmp_path, monkeypatch):
    database, conversion, archived, _ = await environment(tmp_path)
    commands = RearchiveCommands(database, archived, conversion)
    body = observed(database)
    entered, release = threading.Event(), threading.Event()
    original = database.connection
    @contextmanager
    def paused():
        with original() as connection:
            yield connection
            stop = not entered.is_set() and bool(connection.execute(
                "SELECT 1 FROM queue_relocations WHERE status = 'PREPARED'").fetchone())
        if stop:
            entered.set()
            assert release.wait(10), 'Fixture move was never released'
    monkeypatch.setattr(database, 'connection', paused)
    running = asyncio.create_task(commands.execute(body, 'Owner'))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        with pytest.raises(WorkCommandError) as failure:
            await commands.execute(body, 'Owner')
        assert failure.value.code == 'PUBLICATION_RECOVERY_REQUIRED'
        partial = await commands.receipt(body['idempotency_key'])
        assert partial['status'] == 'INCOMPLETE' and partial['completed'] == 0
    finally:
        release.set()
        result = await running
    assert len(result['moved']) == 1 and result['skipped'] == []
    assert await commands.execute(body, 'Owner') == result


@pytest.mark.asyncio
async def test_maintenance_versions_track_inputs_and_policy_but_ignore_progress(tmp_path):
    database, _, _, candidate_id = await environment(tmp_path)
    with database.connection() as connection:
        def versions():
            return dict(connection.execute("SELECT scope, revision FROM settings_versions WHERE scope IN ('rearchive', 'path-cache')"))
        before = versions()
        connection.execute("UPDATE download_jobs SET lease_expires_at = '2026-10-16', details_json = '{\"progress\":33}'")
        connection.execute("UPDATE queue_tasks SET updated_at = '2026-10-16'")
        assert versions() == before
        connection.execute("UPDATE metadata_values SET field_value = 'Changed' WHERE candidate_id = ? AND field_name = 'Title'", (candidate_id,))
        assert versions()['rearchive'] > before['rearchive']
        assert versions()['path-cache'] == before['path-cache']
        before = versions()
        connection.execute("INSERT INTO ai_path_suggestions(candidate_id, fingerprint, prompt_hash, relative_path, directory, filename, model_name) "
                           "VALUES(?, 'f', 'p', 'Fixture.cbz', '', 'Fixture', 'fixture')", (candidate_id,))
        assert all(versions()[scope] > before[scope] for scope in before)
        before = versions()
        connection.execute("UPDATE ai_path_suggestions SET relative_path = 'Later.cbz'")
        assert all(versions()[scope] > before[scope] for scope in before)
        before = versions()
        connection.execute('DELETE FROM ai_path_suggestions')
        assert all(versions()[scope] > before[scope] for scope in before)
        before = versions()
        connection.execute("UPDATE archive_settings SET value = 'Other/{title}' WHERE key = 'library_template'")
        assert versions()['rearchive'] > before['rearchive'] and versions()['path-cache'] == before['path-cache']


@pytest.mark.asyncio
async def test_large_fixed_scope_is_not_truncated_and_late_new_work_is_excluded(tmp_path, monkeypatch):
    from app.archive.maintenance import RearchiveItem
    database, conversion, archived, _ = await environment(tmp_path, packed=False)
    def insert_work(connection, candidate_id):
        connection.execute("INSERT INTO candidates(id, status) VALUES(?, 'APPROVED')", (candidate_id,))
        connection.execute("INSERT INTO metadata_values(candidate_id, field_name, field_value, value_source) "
                           "VALUES(?, 'Title', ?, 'EXHENTAI')", (candidate_id, 'Fixture ' + str(candidate_id)))
        job_id = connection.execute("INSERT INTO download_jobs(candidate_id, idempotency_key, provider, state) "
                                    "VALUES(?, ?, 'TELEGRAM', 'COMPLETED')", (candidate_id, 'fixture:' + str(candidate_id))).lastrowid
        connection.execute("INSERT INTO artifacts(job_id, artifact_type, path, size_bytes) VALUES(?, 'ARCHIVE', ?, 1024)",
                           (job_id, str(tmp_path / 'work/downloads/old.zip')))
    with database.connection() as connection:
        for candidate_id in range(2, 126):
            insert_work(connection, candidate_id)
    original = RearchiveItem.execute
    added = False
    async def admit_later(item, service, operator):
        nonlocal added
        if not added:
            with database.connection() as connection:
                insert_work(connection, 126)
            added = True
        return await original(item, service, operator)
    monkeypatch.setattr(RearchiveItem, 'execute', admit_later)
    commands = RearchiveCommands(database, archived, conversion)
    body = observed(database)
    result = await commands.execute(body, 'Owner')
    assert result['scanned'] == 125 and len(result['queued']) == 125 and result['skipped'] == []
    receipt = await commands.receipt(body['idempotency_key'])
    assert receipt['requested'] == receipt['completed'] == 125 and receipt['result'] == result
    with database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM download_jobs WHERE provider = 'CONVERSION'").fetchone() == (125,)
        assert connection.execute("SELECT count(*) FROM download_jobs WHERE provider = 'CONVERSION' AND candidate_id = 126").fetchone() == (0,)
