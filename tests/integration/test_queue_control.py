"""Concurrent clients, legacy adapters and durable execution identities."""
import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.deps import CSRF_HEADER
from app.db.database import Database
from app.downloads.service import DownloadError, DownloadService
from app.downloads.queue import task_snapshot
from app.conversion.service import ConversionService
from app.downloads.archived import ArchivedWorkService, ArchivedWorkError
from app.main import create_app
from tests.integration.test_api_auth import complete_first_run, make_settings, api_login, NEW_PASSWORD, bearer
from tests.integration.test_realtime import call, hello


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / 'queue.db')
    asyncio.run(db.initialize())
    with db.connection() as c:
        c.execute("INSERT INTO candidates(id, status, filter_result) VALUES(1, 'APPROVED', 'ACCEPT')")
    return db


def pending(db, provider='TELEGRAM'):
    with db.connection() as c:
        return c.execute(
            "INSERT INTO download_jobs(candidate_id, idempotency_key, provider, state) VALUES(1, ?, ?, 'PENDING')",
            (uuid4().hex, provider),
        ).lastrowid


def observed(db, job_id):
    with db.connection() as c:
        snap = task_snapshot(c, job_id)
    return {'task_id': snap['task_id'], 'expected_revision': snap['revision'], 'idempotency_key': uuid4().hex}


def test_same_work_different_sources_cannot_enqueue_together(database, tmp_path):
    services = [DownloadService(database, tmp_path)] * 2
    def enqueue(index):
        try:
            return services[index]._enqueue_sync(1, ('TELEGRAM', 'TELEGRAPH')[index], str(index), '{}')
        except DownloadError as exc:
            return exc.code
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(enqueue, range(2)))
    assert sum(not isinstance(x, str) for x in results) == 1
    assert 'WORK_SLOT_OCCUPIED' in results
    with database.connection() as c:
        assert c.execute('SELECT count(*) FROM download_jobs').fetchone()[0] == 1


def test_two_workers_claim_one_job_once(database, tmp_path):
    job_id = pending(database)
    services = [DownloadService(database, tmp_path), DownloadService(database, tmp_path)]
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda service: service._claim_pending_job_sync(), services))
    claimed = [x for x in results if x is not None]
    assert len(claimed) == 1 and claimed[0]['job_id'] == job_id
    assert claimed[0]['task_id'] and claimed[0]['attempt_id']
    with database.connection() as c:
        assert c.execute('SELECT count(*) FROM queue_attempts').fetchone()[0] == 1


def test_two_conversion_workers_claim_one_attempt(database, tmp_path):
    with database.connection() as c:
        job_id = c.execute("INSERT INTO download_jobs(candidate_id, idempotency_key, provider, state) "
                           "VALUES(1, 'convert:1', 'CONVERSION', 'CONVERSION_PENDING')").lastrowid
    services = [ConversionService(database, tmp_path / 'work', tmp_path / 'library') for _ in range(2)]
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda service: service._claim_pending_job_sync(), services))
    assert sum(row is not None for row in results) == 1
    with database.connection() as c:
        assert c.execute('SELECT kind, status FROM queue_attempts').fetchone() == ('PACK', 'CONVERSION_RUNNING')
        assert task_snapshot(c, job_id)['attempt_id']


def test_retry_new_task_and_attempt_preserves_history_and_rejects_old_draft(database, tmp_path):
    service = DownloadService(database, tmp_path)
    job_id = pending(database)
    first = service._claim_pending_job_sync()
    service._mark_job_failed_sync(job_id, 'TEMPORARY', 'temporary refusal')
    draft = observed(database, job_id)
    result = asyncio.run(service.command(job_id, 'retry', **draft))
    assert result['task_id'] != first['task_id']
    assert result['generation'] > first['generation']
    assert asyncio.run(service.command(job_id, 'retry', **draft)) == result
    with pytest.raises(DownloadError, match='旧任务') as exc:
        asyncio.run(service.command(job_id, 'cancel', **{**draft, 'idempotency_key': uuid4().hex}))
    assert exc.value.code == 'TASK_SUPERSEDED'
    second = service._claim_pending_job_sync()
    assert second['attempt_id'] != first['attempt_id']
    with database.connection() as c:
        old = c.execute('SELECT status, error_code FROM queue_attempts WHERE attempt_id = ?', (first['attempt_id'],)).fetchone()
        assert old == ('FAILED', 'TEMPORARY')
        assert c.execute('SELECT status FROM queue_tasks WHERE task_id = ?', (first['task_id'],)).fetchone()[0] == 'FAILED'


@pytest.mark.parametrize('terminal,action', [('FAILED', 'retry'), ('COMPLETED', 'restart')])
def test_remote_stop_intent_preserves_finished_attempt(database, tmp_path, terminal, action):
    service = DownloadService(database, tmp_path)
    job_id = pending(database, 'EH_TORRENT')
    attempt = service._claim_pending_job_sync()
    with database.connection() as c:
        c.execute("UPDATE download_jobs SET details_json = ? WHERE id = ?",
                  (json.dumps({'hash': 'a' * 40}), job_id))
    if terminal == 'FAILED':
        service._mark_job_failed_sync(job_id, 'TEMPORARY', 'original failure')
        # A legacy remote handle now also retains its slot on failure. Only
        # after the fixture confirms its stop can a new retry be requested.
        with pytest.raises(DownloadError) as blocked:
            asyncio.run(service.command(job_id, action, **observed(database, job_id)))
        assert blocked.value.code == 'JOB_NOT_RETRYABLE'
        operation = service._queue_control.pending_operations()[0]
        service._queue_control.finish_operation(operation)
    else:
        service._mark_job_completed_sync(job_id)
    with database.connection() as c:
        original = c.execute('SELECT * FROM queue_attempts WHERE attempt_id = ?', (attempt['attempt_id'],)).fetchone()
    result = asyncio.run(service.command(job_id, action, **observed(database, job_id)))
    assert result['state'] == 'STOPPING' and result['operation_id']
    with database.connection() as c:
        assert c.execute('SELECT * FROM queue_attempts WHERE attempt_id = ?', (attempt['attempt_id'],)).fetchone() == original
        assert c.execute('SELECT count(*) FROM queue_attempts').fetchone()[0] == 1


def test_internal_retry_reports_slot_conflict_without_changing_history(database, tmp_path):
    service = DownloadService(database, tmp_path)
    job_id = pending(database)
    service._claim_pending_job_sync()
    service._mark_job_failed_sync(job_id, 'TEMPORARY', 'original failure')
    service._enqueue_sync(1, 'TELEGRAPH', 'active-preview', '{}')
    before = asyncio.run(service.get_task(observed(database, job_id)['task_id']))
    with pytest.raises(DownloadError) as exc:
        asyncio.run(service.retry_job(job_id))
    assert exc.value.code == 'WORK_SLOT_OCCUPIED'
    assert asyncio.run(service.get_task(before['task_id'])) == before


@pytest.mark.asyncio
async def test_revoked_conversion_commit_is_handled_as_a_stale_result(database, tmp_path):
    with database.connection() as c:
        job_id = c.execute("INSERT INTO download_jobs(candidate_id, idempotency_key, provider, state) "
                           "VALUES(1, 'convert:1', 'CONVERSION', 'CONVERSION_PENDING')").lastrowid
    service = ConversionService(database, tmp_path / 'work', tmp_path / 'library')
    downloads = DownloadService(database, tmp_path)
    started, released = asyncio.Event(), asyncio.Event()
    async def legacy_handler(job):
        started.set()
        await released.wait()
        with database.connection() as c:
            c.execute("UPDATE download_jobs SET state = 'CONVERSION_COMPLETED' WHERE id = ?", (job['job_id'],))
    service._handle_job = legacy_handler
    worker = asyncio.create_task(service._process_one())
    await asyncio.wait_for(started.wait(), 5)
    await downloads.command(job_id, 'cancel', **observed(database, job_id))
    released.set()
    assert await asyncio.wait_for(worker, 5) is True
    with database.connection() as c:
        assert task_snapshot(c, job_id)['state'] == 'STOPPING'
        assert c.execute('SELECT count(*) FROM queue_attempts').fetchone()[0] == 1


def test_receipt_before_revision_check_and_key_content_conflict(database, tmp_path):
    service = DownloadService(database, tmp_path)
    job_id = pending(database)
    draft = observed(database, job_id)
    result = asyncio.run(service.command(job_id, 'pause', **draft))
    assert asyncio.run(service.command(job_id, 'pause', **draft)) == result
    with pytest.raises(DownloadError) as exc:
        asyncio.run(service.command(job_id, 'cancel', **draft))
    assert exc.value.code == 'IDEMPOTENCY_CONFLICT'


def test_simultaneous_same_key_submissions_share_one_operation(database, tmp_path):
    service = DownloadService(database, tmp_path)
    job_id = pending(database)
    service._claim_pending_job_sync()
    draft = observed(database, job_id)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: asyncio.run(service.command(job_id, 'cancel', **draft)), range(2)))
    assert results[0] == results[1]
    with database.connection() as c:
        assert c.execute('SELECT count(*) FROM queue_operations').fetchone()[0] == 1
        assert c.execute('SELECT count(*) FROM queue_command_receipts').fetchone()[0] == 1
        assert task_snapshot(c, job_id)['generation'] == results[0]['generation']


def test_old_receipt_is_retained_until_its_operation_finishes(database, tmp_path):
    service = DownloadService(database, tmp_path)
    job_id = pending(database)
    service._claim_pending_job_sync()
    draft = observed(database, job_id)
    result = asyncio.run(service.command(job_id, 'cancel', **draft))
    with database.connection() as c:
        c.execute("UPDATE queue_command_receipts SET created_at = datetime('now', '-8 days')")
        c.execute("INSERT INTO candidates(id, status) VALUES(2, 'APPROVED')")
        second = c.execute("INSERT INTO download_jobs(candidate_id, idempotency_key, provider, state) "
                           "VALUES(2, 'second', 'TELEGRAM', 'PENDING')").lastrowid
    asyncio.run(service.command(second, 'pause', **observed(database, second)))
    assert asyncio.run(service.command(job_id, 'cancel', **draft)) == result
    with database.connection() as c:
        c.execute("UPDATE queue_operations SET status = 'COMPLETED' WHERE operation_id = ?", (result['operation_id'],))
        c.execute("UPDATE download_jobs SET state = 'CANCELLED' WHERE id = ?", (job_id,))
    asyncio.run(service.command(second, 'resume', **observed(database, second)))
    with database.connection() as c:
        assert c.execute('SELECT count(*) FROM queue_command_receipts WHERE idempotency_key = ?',
                         (draft['idempotency_key'],)).fetchone()[0] == 0


def test_different_devices_revision_competition_has_one_winner(database, tmp_path):
    service = DownloadService(database, tmp_path)
    job_id = pending(database)
    draft = observed(database, job_id)
    def apply(action):
        try:
            return asyncio.run(service.command(job_id, action, **{**draft, 'idempotency_key': action}))
        except DownloadError as exc:
            return exc.code
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(apply, ['pause', 'cancel']))
    assert sum(isinstance(x, dict) for x in results) == 1
    assert 'REVISION_CONFLICT' in results


def test_receipt_failure_rolls_back_state_intent_and_revision(database, tmp_path):
    service = DownloadService(database, tmp_path)
    job_id = pending(database)
    service._claim_pending_job_sync()
    draft = observed(database, job_id)
    with database.connection() as c:
        c.executescript("CREATE TRIGGER reject_receipt BEFORE INSERT ON queue_command_receipts BEGIN SELECT RAISE(ABORT, 'injected'); END;")
    with pytest.raises(sqlite3.IntegrityError, match='injected'):
        asyncio.run(service.command(job_id, 'cancel', **draft))
    with database.connection() as c:
        snap = task_snapshot(c, job_id)
        assert snap['state'] == 'DOWNLOADING' and snap['revision'] == draft['expected_revision']
        assert c.execute('SELECT count(*) FROM queue_operations').fetchone()[0] == 0


def test_stopping_keeps_slot_and_rejects_legacy_completion_and_artifact(database, tmp_path):
    service = DownloadService(database, tmp_path)
    job_id = pending(database)
    service._claim_pending_job_sync()
    result = asyncio.run(service.command(job_id, 'cancel', **observed(database, job_id)))
    assert result['state'] == 'STOPPING' and result['operation_id']
    with pytest.raises(DownloadError) as exc:
        service._enqueue_sync(1, 'TELEGRAPH', 'next', '{}')
    assert exc.value.code == 'WORK_SLOT_OCCUPIED'
    with pytest.raises(sqlite3.IntegrityError, match='QUEUE_STOP_NOT_CONFIRMED'):
        service._mark_job_completed_sync(job_id)
    with pytest.raises(sqlite3.IntegrityError, match='QUEUE_STALE_ARTIFACT'):
        with database.connection() as c:
            c.execute("INSERT INTO artifacts(job_id, artifact_type, path) VALUES(?, 'ARCHIVE', 'stale.zip')", (job_id,))


def test_running_records_cannot_be_deleted_by_raw_legacy_writer(database, tmp_path):
    service = DownloadService(database, tmp_path)
    job_id = pending(database)
    service._claim_pending_job_sync()
    with pytest.raises(sqlite3.IntegrityError, match='QUEUE_ACTIVE_RECORD_NOT_REMOVABLE'):
        with database.connection() as c:
            c.execute('DELETE FROM download_jobs WHERE id = ?', (job_id,))
    with database.connection() as c:
        assert task_snapshot(c, job_id)['state'] == 'DOWNLOADING'


def test_remove_tracks_older_running_source_before_deleting_files(database, tmp_path):
    pending(database)
    downloads = DownloadService(database, tmp_path)
    downloads._claim_pending_job_sync()
    source = tmp_path / 'kept.zip'
    source.write_bytes(b'keep existing file')
    with database.connection() as c:
        newest = c.execute("INSERT INTO download_jobs(candidate_id, idempotency_key, provider, state) "
                           "VALUES(1, 'finished-preview', 'TELEGRAPH', 'COMPLETED')").lastrowid
        c.execute("INSERT INTO artifacts(job_id, artifact_type, path) VALUES(?, 'ARCHIVE', ?)", (newest, str(source)))
    archived = ArchivedWorkService(database, library_path=tmp_path / 'library', work_path=tmp_path)
    result = asyncio.run(archived.remove_work(1, delete_files=True))
    assert result['cleanup_status'] == 'PENDING' and source.exists()
    with database.connection() as c:
        assert c.execute('SELECT count(*) FROM download_jobs').fetchone()[0] == 0


@pytest.mark.asyncio
async def test_revoked_legacy_completion_does_not_crash_the_download_worker(database, tmp_path):
    job_id = pending(database)
    service = DownloadService(database, tmp_path)
    started, released = asyncio.Event(), asyncio.Event()
    async def legacy_handler(job):
        started.set()
        await released.wait()
        service._mark_job_completed_sync(job['job_id'])
    service._handle_job = legacy_handler
    worker = asyncio.create_task(service._process_one())
    await asyncio.wait_for(started.wait(), 5)
    result = await service.command(job_id, 'cancel', **observed(database, job_id))
    released.set()
    assert await asyncio.wait_for(worker, 5) is True
    with database.connection() as c:
        assert task_snapshot(c, job_id)['state'] == 'STOPPING'
    assert result['operation_id']


def test_task_progress_does_not_invalidate_priority_draft(database, tmp_path):
    service = DownloadService(database, tmp_path)
    job_id = pending(database)
    service._claim_pending_job_sync()
    draft = observed(database, job_id)
    with database.connection() as c:
        generation = task_snapshot(c, job_id)['generation']
        c.execute("UPDATE download_jobs SET details_json = '{\"progress\":25}' WHERE id = ?", (job_id,))
    result = asyncio.run(service.command(job_id, 'priority', priority=10, **draft))
    assert result['generation'] == generation
    assert result['revision'] == draft['expected_revision'] + 1


def test_arbitrary_old_job_is_not_an_approval_fact(database, tmp_path):
    with database.connection() as c:
        c.execute('DELETE FROM candidate_approvals')
        c.execute("UPDATE candidates SET status = 'NEEDS_INFO'")
        c.execute("INSERT INTO download_jobs(candidate_id, idempotency_key, provider, state) VALUES(1, 'old', 'TELEGRAM', 'FAILED')")
    service = DownloadService(database, tmp_path)
    with pytest.raises(DownloadError) as exc:
        service._enqueue_sync(1, 'TELEGRAPH', 'new', '{}')
    assert exc.value.code == 'CANDIDATE_NOT_DOWNLOADABLE'


def test_approval_survives_job_removal(database, tmp_path):
    job_id = pending(database)
    service = DownloadService(database, tmp_path)
    service._cancel_job_sync(job_id)
    with database.connection() as c:
        c.execute('DELETE FROM download_jobs WHERE id = ?', (job_id,))
        c.execute("UPDATE candidates SET status = 'NEEDS_INFO'")
    assert service._enqueue_sync(1, 'TELEGRAPH', 'new', '{}').created


def test_retry_refusal_performs_no_remote_side_effects(database, tmp_path):
    called = []
    async def remote(*args):
        called.append(args)
    service = DownloadService(database, tmp_path, torrent_push=remote, torrent_abandon=remote, torrent_verify=remote)
    job_id = pending(database, 'EH_TORRENT')
    with pytest.raises(DownloadError):
        asyncio.run(service.retry_job(job_id))
    assert called == []


def test_switch_source_persists_intent_without_enqueuing_second_source(database, tmp_path):
    job_id = pending(database)
    service = DownloadService(database, tmp_path, telegraph_preflight=lambda _: asyncio.sleep(0))
    result = asyncio.run(service.command(job_id, 'switch-source', provider='TELEGRAPH', **observed(database, job_id)))
    assert result['state'] == 'STOPPING'
    assert service._claim_pending_job_sync() is None
    with database.connection() as c:
        assert c.execute('SELECT count(*) FROM download_jobs').fetchone()[0] == 1
        assert json.loads(c.execute('SELECT params_json FROM queue_operations').fetchone()[0]) == {'provider': 'TELEGRAPH'}


def test_legacy_job_id_reuse_cannot_redirect_old_task_command(database, tmp_path):
    job_id = pending(database)
    old = observed(database, job_id)
    service = DownloadService(database, tmp_path)
    service._cancel_job_sync(job_id)
    with database.connection() as c:
        c.execute('DELETE FROM download_jobs WHERE id = ?', (job_id,))
    assert pending(database) == job_id
    with pytest.raises(DownloadError) as exc:
        asyncio.run(service.command(job_id, 'cancel', **old))
    assert exc.value.code == 'TASK_SUPERSEDED'
    assert asyncio.run(service.get_task(old['task_id']))['status'] == 'REMOVED'


def old_database(tmp_path, rows, last_version=26):
    path = tmp_path / 'old.db'
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)')
        for migration in sorted(Path('app/db/migrations').glob('*.sql')):
            version = int(migration.name.split('_')[0])
            if version > last_version:
                break
            c.executescript(migration.read_text(encoding='utf-8'))
            c.execute('INSERT INTO schema_migrations VALUES(?, CURRENT_TIMESTAMP)', (version,))
            c.commit()
        c.execute("INSERT INTO candidates(id, status, filter_result) VALUES(1, 'PROCESSING', 'ACCEPT')")
        c.execute("INSERT INTO candidates(id, status, filter_result) VALUES(2, 'FAILED', 'ACCEPT')")
        c.execute("INSERT INTO review_actions(candidate_id, action, operator_name) VALUES(1, 'AUTO_APPROVE', 'system')")
        for candidate_id, provider, state, key in rows:
            c.execute('INSERT INTO download_jobs(candidate_id, provider, state, idempotency_key) VALUES(?, ?, ?, ?)',
                      (candidate_id, provider, state, key))
    return Database(path)


def test_upgrade_backfills_approval_evidence_and_running_attempt_without_fabricated_history(tmp_path):
    db = old_database(tmp_path, [(1, 'TELEGRAM', 'DOWNLOADING', 'one'), (2, 'TELEGRAM', 'FAILED', 'two')])
    asyncio.run(db.initialize())
    asyncio.run(db.initialize())
    with db.connection() as c:
        assert c.execute('SELECT candidate_id FROM candidate_approvals').fetchall() == [(1,)]
        assert c.execute('SELECT count(*) FROM queue_tasks').fetchone()[0] == 2
        assert c.execute('SELECT count(*) FROM queue_attempts').fetchone()[0] == 1
        assert c.execute('SELECT count(*) FROM schema_migrations WHERE version = 27').fetchone()[0] == 1


def test_upgrade_027_preserves_finished_attempt_and_is_idempotent(tmp_path):
    db = old_database(tmp_path, [(1, 'EH_TORRENT', 'PENDING', 'one')], last_version=27)
    service = DownloadService(db, tmp_path)
    with db.connection() as c:
        c.execute("UPDATE download_jobs SET state = 'DOWNLOADING', attempt_count = 1")
        attempt = task_snapshot(c, c.execute('SELECT id FROM download_jobs').fetchone()[0])
        attempt['job_id'] = c.execute('SELECT id FROM download_jobs').fetchone()[0]
    job_id = attempt['job_id']
    with db.connection() as c:
        c.execute("UPDATE download_jobs SET details_json = ? WHERE id = ?",
                  (json.dumps({'hash': 'a' * 40}), job_id))
    # Reproduce the old writer on its actual schema, then upgrade before using
    # the current service's resource-aware failure/cleanup implementation.
    with db.connection() as c:
        c.execute("UPDATE download_jobs SET state = 'FAILED', error_code = 'TEMPORARY', "
                  "error_message = 'failure before upgrade' WHERE id = ?", (job_id,))
    with db.connection() as c:
        c.row_factory = sqlite3.Row
        before = [dict(row) for row in c.execute('SELECT * FROM queue_attempts WHERE task_id = ?',
                                               (attempt['task_id'],))]
    asyncio.run(db.initialize())
    asyncio.run(db.initialize())
    result = asyncio.run(service.command(job_id, 'retry', **observed(db, job_id)))
    assert result['state'] == 'STOPPING'
    assert asyncio.run(service.get_task(attempt['task_id']))['attempts'] == before
    with db.connection() as c:
        assert c.execute('SELECT count(*) FROM schema_migrations WHERE version = 28').fetchone()[0] == 1


def test_upgrade_with_two_active_sources_rolls_back_and_keeps_both_remote_records(tmp_path):
    db = old_database(tmp_path, [(1, 'TELEGRAM', 'DOWNLOADING', 'one'), (1, 'EH_TORRENT', 'WAITING_TORRENT', 'two')])
    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(db.initialize())
    with db.connection() as c:
        assert c.execute('SELECT count(*) FROM download_jobs').fetchone()[0] == 2
        assert c.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 26
        assert 'task_id' not in {row[1] for row in c.execute('PRAGMA table_info(download_jobs)')}
        assert c.execute("SELECT count(*) FROM sqlite_master WHERE name = 'queue_tasks'").fetchone()[0] == 0


def test_operation_survives_terminal_record_and_candidate_removal(database, tmp_path):
    service = DownloadService(database, tmp_path)
    job_id = pending(database)
    service._claim_pending_job_sync()
    result = asyncio.run(service.command(job_id, 'cancel', **observed(database, job_id)))
    with database.connection() as c:
        c.execute("UPDATE download_jobs SET state = 'CANCELLED' WHERE id = ?", (job_id,))
        c.execute('DELETE FROM download_jobs WHERE id = ?', (job_id,))
        c.execute('DELETE FROM candidates WHERE id = 1')
        assert c.execute('SELECT task_id, status FROM queue_operations WHERE operation_id = ?', (result['operation_id'],)).fetchone() == (result['task_id'], 'PENDING')


def test_http_web_ws_share_preconditions_and_receipt_across_credentials(tmp_path):
    settings = replace(make_settings(tmp_path), archive_toolchain_auto_install=False)
    app = create_app(settings)
    with TestClient(app) as client:
        complete_first_run(client, settings)
        client.portal.call(app.state.download_service.stop)
        db = app.state.database
        with db.connection() as c:
            c.execute("INSERT INTO candidates(id, status, filter_result) VALUES(1, 'APPROVED', 'ACCEPT')")
        job_id = pending(db)
        token = api_login(client, NEW_PASSWORD).json()['access_token']
        draft = observed(db, job_id)
        path = f'/api/v1/jobs/{job_id}/pause'
        response = client.post(path, json=draft, headers=bearer(token))
        assert response.status_code == 200
        result = response.json()
        task = client.get(f"/api/v1/tasks/{result['task_id']}", headers=bearer(token))
        assert task.status_code == 200 and task.json()['status'] == 'PAUSED'
        page = client.get('/activity')
        assert f'name="task_id_{job_id}" value="{result["task_id"]}"' in page.text
        assert f'name="expected_revision_{job_id}" value="{result["revision"]}"' in page.text
        csrf = client.get('/activity').context['csrf_token']
        assert client.post(path, json=draft, headers={CSRF_HEADER: csrf}).json() == result
        assert client.post(f'/api/v1/jobs/{job_id}/resume', json={}, headers=bearer(token)).json()['error']['code'] == 'TASK_ID_REQUIRED'
        with client.websocket_connect('/ws/ui', headers={'Origin': 'http://testserver'}) as ws:
            hello(client, ws)
            frame = call(ws, 'api.job_action', {'params': {'job_id': job_id, 'action': 'pause'}, 'body': draft})[0]
            assert frame['type'] == 'command.result'
            assert frame['data'] == result
        form = {**draft, 'csrf_token': csrf}
        posted = client.post(f'/activity/jobs/{job_id}/pause', data=form, follow_redirects=False)
        assert posted.status_code == 303 and 'error=' not in posted.headers['location']
        assert len(client.get('/activity').context['snapshot']['downloads']) > 0
        with db.connection() as c:
            batch_jobs = []
            for candidate_id in (2, 3):
                c.execute("INSERT INTO candidates(id, status) VALUES(?, 'APPROVED')", (candidate_id,))
                batch_jobs.append(c.execute("INSERT INTO download_jobs(candidate_id, idempotency_key, provider, state) "
                                            "VALUES(?, ?, 'TELEGRAM', 'PENDING')", (candidate_id, uuid4().hex)).lastrowid)
        preconditions = {str(jid): observed(db, jid) for jid in batch_jobs}
        advance = client.post(f'/api/v1/jobs/{batch_jobs[0]}/priority',
                              json={**preconditions[str(batch_jobs[0])], 'idempotency_key': uuid4().hex, 'priority': 10},
                              headers=bearer(token))
        assert advance.status_code == 200
        payload = {'action': 'pause', 'job_ids': batch_jobs, 'preconditions': preconditions}
        batch = client.post('/api/v1/jobs/batch', json=payload, headers=bearer(token))
        assert batch.status_code == 200
        data = batch.json()
        assert [item['job_id'] for item in data['applied']] == [batch_jobs[1]]
        assert data['skipped'] == [{'job_id': batch_jobs[0], 'code': 'REVISION_CONFLICT',
                                    'message': '任务已被另一端修改，请刷新后核对'}]
        assert client.post('/api/v1/jobs/batch', json=payload, headers=bearer(token)).json() == data
