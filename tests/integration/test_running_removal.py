"""Removal owns a durable reservation after candidate/job rows disappear."""
import asyncio
import json
import sqlite3
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.downloads.archived import ArchivedWorkService
from app.downloads.queue import QueueControl
from app.downloads.execution import attempt_path, execution_scope
from app.downloads.service import DownloadService
from app.review.application import remove_work
from app.review.commands import WorkCommands
from tests.work_commands import observed_review_batch
from app.main import create_app
from app.api.deps import CSRF_HEADER
from tests.integration.test_api_auth import complete_first_run, make_settings
from tests.integration.test_api_domains import csrf_token
from tests.integration.test_queue_control import database, observed, pending, old_database
from tests.integration.test_remote_stop_confirmation import Remote, services


def archived(database, tmp_path):
    return ArchivedWorkService(database, tmp_path / "library", tmp_path)


def attach(database, job_id, path):
    path.write_bytes(b"existing valid archive")
    with database.connection() as c:
        c.execute("INSERT INTO artifacts(job_id, artifact_type, path) VALUES(?, 'ARCHIVE', ?)",
                  (job_id, str(path)))


@pytest.mark.asyncio
async def test_purge_revokes_late_write_and_readded_identity_waits_for_actual_exit(database, tmp_path):
    with database.connection() as c:
        c.execute("UPDATE candidates SET ex_gid = 1234, ex_gallery_token = 'old' WHERE id = 1")
    job_id = pending(database)
    source = tmp_path / "existing.zip"
    attach(database, job_id, source)
    removal = archived(database, tmp_path)
    downloads = DownloadService(database, tmp_path, cleanup_removals=removal.process_removals)
    entered, cancelling, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    rejected = []
    staged_paths = []

    async def producer(job):
        staged = attempt_path(tmp_path / "downloads" / "comic.zip")
        staged.parent.mkdir(parents=True)
        staged.with_suffix(".partial").write_bytes(b"partial download")
        staged_paths.append(staged)
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelling.set()
            await asyncio.wait_for(release.wait(), 2)
            staged.write_bytes(b"late isolated output")
            try:
                with database.connection() as c:
                    c.execute("UPDATE download_jobs SET state = 'COMPLETED' WHERE id = ?", (job_id,))
            except sqlite3.IntegrityError as exc:
                rejected.append(str(exc))
            raise

    downloads._handle_job = producer
    worker = asyncio.create_task(downloads._process_one())
    await asyncio.wait_for(entered.wait(), 2)
    old_task = observed(database, job_id)["task_id"]
    version = await WorkCommands(database).version(1)
    result = await remove_work(database, removal, 1, 'delete-files', {
        'work_id': version['work_id'], 'expected_revision': version['revision'],
        'idempotency_key': 'purge-running'}, 'admin')
    assert result["removed_candidate"] == 1 and result["cleanup_status"] == "PENDING"
    assert await database.get_candidate(1) is None and source.exists()
    with database.connection() as c:
        # A changed token and new candidate id still denote the same gallery.
        c.execute("INSERT INTO candidates(id, status, ex_gid, ex_gallery_token) "
                  "VALUES(2, 'APPROVED', 1234, 'new')")
    new_job = await downloads.enqueue_telegraph_download(2)
    assert new_job.job_id == job_id and new_job.task_id != old_task
    assert downloads._claim_pending_job_sync() is None
    stopping = asyncio.create_task(downloads.process_operations())
    await asyncio.wait_for(cancelling.wait(), 2)
    assert not stopping.done() and source.exists()
    release.set()
    assert await asyncio.wait_for(stopping, 2) == 1
    assert await worker is True
    assert rejected == ["QUEUE_STALE_EXECUTION"] and not source.exists()
    assert not staged_paths[0].parent.exists()
    claimed = downloads._claim_pending_job_sync()
    assert claimed["task_id"] == new_job.task_id
    assert (await downloads.get_task(old_task))["removals"][0]["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_remove_default_keeps_existing_files_and_cancels_pending_source_switch(database, tmp_path):
    job_id = pending(database)
    removal = archived(database, tmp_path)
    downloads = DownloadService(database, tmp_path, cleanup_removals=removal.process_removals,
                                telegraph_preflight=lambda _: asyncio.sleep(0))
    downloads._claim_pending_job_sync()
    source = tmp_path / "keep.zip"
    attach(database, job_id, source)
    switched = await downloads.command(job_id, "switch-source", provider="TELEGRAPH",
                                       **observed(database, job_id))
    removed = await removal.remove_work(1)
    assert removed["cleanup_status"] == "PENDING"
    assert await downloads.process_operations() == 1
    assert source.exists() and await database.get_candidate(1) is not None
    with database.connection() as c:
        assert c.execute("SELECT count(*) FROM download_jobs").fetchone()[0] == 0
        action, params = c.execute("SELECT action, params_json FROM queue_operations "
                                   "WHERE operation_id = ?", (switched["operation_id"],)).fetchone()
    assert action == "remove" and json.loads(params)["superseded_action"] == "switch-source"


@pytest.mark.asyncio
async def test_remote_removal_keeps_reservation_and_files_until_delete_is_confirmed(database, tmp_path):
    remote = Remote()
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote.request)) as client:
        downloads, torrent, _ = services(database, tmp_path, remote, client)
        removal = archived(database, tmp_path)
        downloads._cleanup_removals = removal.process_removals
        job_id = pending(database, "EH_TORRENT")
        assert await downloads._process_one() is True
        source = tmp_path / "remote.zip"
        attach(database, job_id, source)
        remote.keep_after_delete = True
        result = await removal.purge_work(1, delete_files=True)
        assert await downloads.process_operations() == 0 and source.exists()
        with database.connection() as c:
            assert c.execute("SELECT status FROM queue_removals").fetchone()[0] == "PENDING"
            assert c.execute("SELECT error_code FROM queue_operations").fetchone()[0] == "REMOTE_STOP_NOT_CONFIRMED"
            c.execute("INSERT INTO candidates(id, status, magnet_url, torrent_hash) "
                      "VALUES(2, 'APPROVED', 'magnet:test', ?)", ("A" * 40,))
        new_job = await downloads.enqueue_telegraph_download(2)
        assert downloads._claim_pending_job_sync() is None
        remote.keep_after_delete = False
        assert await downloads.process_operations() == 1
        assert not source.exists() and downloads._claim_pending_job_sync()["task_id"] == new_job.task_id
        assert len(result["operation_ids"]) == 1


@pytest.mark.asyncio
async def test_cleanup_resumes_after_restart_and_retries_failed_file_unlink(database, tmp_path, monkeypatch):
    job_id = pending(database)
    downloads = DownloadService(database, tmp_path)
    downloads._claim_pending_job_sync()
    source = tmp_path / "retry.zip"
    attach(database, job_id, source)
    removal = archived(database, tmp_path)
    result = await removal.purge_work(1, delete_files=True)
    operation = QueueControl(database).pending_operations()[0]
    QueueControl(database).finish_operation(operation)
    original = removal._unlink_unreferenced_sync

    def unavailable(*args):
        raise OSError("controlled locked file")

    monkeypatch.setattr(removal, "_unlink_unreferenced_sync", unavailable)
    await removal.process_removals()
    with database.connection() as c:
        status, failed = c.execute("SELECT status, failed_json FROM queue_removals").fetchone()
    assert status == "PENDING" and json.loads(failed) == [str(source)] and source.exists()
    monkeypatch.setattr(removal, "_unlink_unreferenced_sync", original)
    restarted = archived(database, tmp_path)
    assert await restarted.process_removals() == 1
    assert not source.exists()
    with database.connection() as c:
        assert c.execute("SELECT status FROM queue_removals WHERE removal_id = ?",
                         (result["removal_id"],)).fetchone()[0] == "COMPLETED"
        assert c.execute("SELECT deleted_files FROM removed_works").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_manifest_refuses_a_path_still_owned_by_another_work(database, tmp_path):
    job_id = pending(database)
    with database.connection() as c:
        c.execute("UPDATE download_jobs SET state = 'COMPLETED' WHERE id = ?", (job_id,))
        c.execute("INSERT INTO candidates(id, status) VALUES(2, 'APPROVED')")
        other = c.execute("INSERT INTO download_jobs(candidate_id, idempotency_key, provider, state) "
                          "VALUES(2, 'other', 'TELEGRAPH', 'COMPLETED')").lastrowid
    source = tmp_path / "shared.zip"
    attach(database, job_id, source)
    attach(database, other, source)
    result = await archived(database, tmp_path).remove_work(1, delete_files=True)
    assert result["cleanup_status"] == "PENDING"
    assert result["failed_files"] == (str(source),) and source.exists()


@pytest.mark.asyncio
async def test_direct_claim_cannot_bypass_removal_reservation(database, tmp_path):
    job_id = pending(database)
    downloads = DownloadService(database, tmp_path)
    downloads._claim_pending_job_sync()
    await archived(database, tmp_path).remove_work(1)
    with pytest.raises(sqlite3.IntegrityError, match="QUEUE_REMOVAL_NOT_CONFIRMED"):
        with database.connection() as c:
            c.execute("INSERT INTO download_jobs(candidate_id, idempotency_key, provider, state) "
                      "VALUES(1, 'bypass', 'TELEGRAPH', 'DOWNLOADING')")
    new_job = await downloads.enqueue_telegraph_download(1)
    with pytest.raises(sqlite3.IntegrityError, match="QUEUE_REMOVAL_NOT_CONFIRMED"):
        with database.connection() as c:
            c.execute("UPDATE download_jobs SET state = 'DOWNLOADING' WHERE id = ?", (new_job.job_id,))


def test_shared_http_api_returns_queryable_cleanup_after_candidate_is_gone(tmp_path):
    settings = replace(make_settings(tmp_path), archive_toolchain_auto_install=False)
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/api/v1/removals/unknown").status_code == 401
        complete_first_run(client, settings)
        client.portal.call(app.state.download_service.stop)
        with app.state.database.connection() as c:
            c.execute("INSERT INTO candidates(id, status) VALUES(1, 'APPROVED')")
        pending(app.state.database)
        app.state.download_service._claim_pending_job_sync()
        result = client.post("/api/v1/candidates/batch",
                             json={"action": "delete", "candidate_ids": [1], **observed_review_batch(client, [1])},
                             headers={CSRF_HEADER: csrf_token(client)})
        assert result.status_code == 200
        removed = result.json()["applied"][0]
        assert removed["cleanup_status"] == "PENDING" and removed["operation_ids"]
        assert client.get("/api/v1/works/1").status_code == 404
        path = f"/api/v1/removals/{removed['removal_id']}"
        snapshot = client.get(path).json()
        assert snapshot["status"] == "PENDING" and snapshot["operations"][0]["status"] == "PENDING"
        client.portal.call(app.state.download_service.process_operations)
        assert client.get(path).json()["status"] == "COMPLETED"
        assert client.get("/api/v1/removals/unknown").status_code == 404


@pytest.mark.asyncio
async def test_upgrade_028_creates_removal_reservation_and_retains_running_history(tmp_path):
    db = old_database(tmp_path, [(1, 'TELEGRAM', 'PENDING', 'old')], last_version=28)
    with db.connection() as c:
        c.execute("UPDATE download_jobs SET state = 'DOWNLOADING', attempt_count = 1")
        task_id = c.execute("SELECT task_id FROM download_jobs").fetchone()[0]
    await db.initialize()
    await db.initialize()
    removal = archived(db, tmp_path)
    result = await removal.purge_work(1)
    assert result["cleanup_status"] == "PENDING"
    assert await db.get_candidate(1) is None
    snapshot = await QueueControl(db).get_task(task_id)
    assert snapshot["status"] == "REMOVED" and len(snapshot["attempts"]) == 1
    assert snapshot["removals"][0]["status"] == "PENDING"
    with db.connection() as c:
        assert c.execute("SELECT count(*) FROM schema_migrations WHERE version = 29").fetchone()[0] == 1


def test_upgrade_029_conflict_rolls_back_all_new_tables_and_guards(tmp_path):
    db = old_database(tmp_path, [(1, 'TELEGRAM', 'PENDING', 'old')], last_version=28)
    with db.connection() as c:
        before = c.execute("SELECT * FROM download_jobs").fetchall()
        c.execute("CREATE TABLE queue_attempt_paths(marker TEXT)")
    with pytest.raises(sqlite3.OperationalError, match="already exists"):
        asyncio.run(db.initialize())
    with db.connection() as c:
        assert c.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 28
        assert c.execute("SELECT * FROM download_jobs").fetchall() == before
        assert c.execute("SELECT count(*) FROM sqlite_master WHERE name IN "
                         "('queue_remote_resources', 'queue_remote_reclaim_guard', 'queue_removals')").fetchone()[0] == 0
        assert c.execute("PRAGMA table_info(queue_attempt_paths)").fetchone()[1] == 'marker'
        c.execute("DROP TABLE queue_attempt_paths")
    asyncio.run(db.initialize())
    asyncio.run(db.initialize())
    with db.connection() as c:
        assert c.execute("SELECT count(*) FROM schema_migrations WHERE version = 29").fetchone()[0] == 1


def test_legacy_torrent_failure_reserves_slot_without_new_resource_row(database, tmp_path):
    job_id = pending(database, "EH_TORRENT")
    downloads = DownloadService(database, tmp_path)
    downloads._claim_pending_job_sync()
    with database.connection() as c:
        c.execute("UPDATE download_jobs SET details_json = ? WHERE id = ?",
                  (json.dumps({"hash": "b" * 40}), job_id))
    downloads._mark_job_failed_sync(job_id, "TEMPORARY", "controlled legacy failure")
    with database.connection() as c:
        assert c.execute("SELECT state FROM download_jobs WHERE id = ?", (job_id,)).fetchone()[0] == "STOPPING"
        assert c.execute("SELECT status FROM queue_attempts").fetchone()[0] == "FAILED"
        assert c.execute("SELECT action, status FROM queue_operations").fetchone() == ("cancel", "PENDING")
    with pytest.raises(ValueError, match="作品槽"):
        downloads._enqueue_sync(1, "TELEGRAPH", "blocked", "{}")


@pytest.mark.asyncio
async def test_default_removal_keeps_registered_archive_inside_attempt_tree_but_cleans_partial(database, tmp_path):
    job_id = pending(database)
    downloads = DownloadService(database, tmp_path)
    execution = downloads._claim_pending_job_sync()
    with execution_scope(database.path, execution):
        source = attempt_path(tmp_path / "downloads" / "saved.zip")
        source.parent.mkdir(parents=True)
        attach(database, job_id, source)
        downloads._mark_job_completed_sync(job_id)
    partial = source.with_suffix(".partial")
    partial.write_bytes(b"uncommitted partial")
    result = await archived(database, tmp_path).remove_work(1)
    assert result["cleanup_status"] == "COMPLETED"
    assert source.exists() and not partial.exists()
    assert result["deleted_files"] == (str(partial),)
    with database.connection() as c:
        assert c.execute("SELECT deleted_files FROM removed_works").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_old_purge_cleanup_never_clears_new_candidate_pin_when_integer_id_is_reused(database, tmp_path):
    pending(database)
    DownloadService(database, tmp_path)._claim_pending_job_sync()
    with database.connection() as c:
        c.execute("INSERT INTO work_archive_paths(candidate_id, relative_path, operator_name) "
                  "VALUES(1, 'kept.cbz', 'fixture')")
    removal = archived(database, tmp_path)
    await removal.purge_work(1, delete_files=True)
    with database.connection() as c:
        c.execute("INSERT INTO candidates(id, status) VALUES(1, 'APPROVED')")
        c.execute("INSERT INTO work_archive_paths(candidate_id, relative_path, operator_name) "
                  "VALUES(1, 'kept.cbz', 'new-fixture')")
    operation = QueueControl(database).pending_operations()[0]
    QueueControl(database).finish_operation(operation)
    await removal.process_removals()
    with database.connection() as c:
        assert c.execute("SELECT relative_path, operator_name FROM work_archive_paths WHERE candidate_id = 1").fetchone() == (
            'kept.cbz', 'new-fixture')
