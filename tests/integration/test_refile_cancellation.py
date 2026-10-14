"""Real refile threads must fence bytes and path records before publication."""
import asyncio
from pathlib import PurePosixPath
import sqlite3
from threading import Event

import pytest

from app.conversion.service import ConversionService
from app.db.database import Database
from app.downloads.archived import ArchivedWorkService
from app.downloads.service import DownloadService
from tests.integration.test_archive_workflow import seed_downloaded_archive, write_zip
from tests.integration.test_queue_control import observed


async def packaged(tmp_path):
    db = Database(tmp_path / "queue.db")
    source = tmp_path / "work" / "downloads" / "comic.zip"
    write_zip(source, ("01.jpg",))
    candidate_id = await seed_downloaded_archive(db, source)
    archive = ArchivedWorkService(db, tmp_path / "library", tmp_path / "work")
    pack = ConversionService(db, tmp_path / "work", tmp_path / "library",
                             refile=lambda candidate_id, relative: archive.refile_work(
                                 candidate_id, relative, allow_while_running=True))
    await pack.enqueue_for_candidate(candidate_id)
    assert await pack._process_one()
    return db, candidate_id, archive, pack


@pytest.mark.asyncio
@pytest.mark.parametrize("remove", [False, True])
async def test_revoked_refile_thread_cannot_move_or_recreate_path_pin(tmp_path, remove):
    db, candidate_id, archive, pack = await packaged(tmp_path)
    published = tmp_path / "library" / "Archive Title.cbz"
    original = published.read_bytes()
    entered, release, exited = Event(), Event(), Event()
    relocate = archive._relocate_sync
    def delayed(*args):
        entered.set()
        assert release.wait(5)
        try:
            return relocate(*args)
        finally:
            exited.set()
    archive._relocate_sync = delayed
    async def path(*args, **kwargs):
        return PurePosixPath("Changed/Book.cbz")
    pack.ai_path_for_refile = path
    job_id = await pack.enqueue_for_candidate(candidate_id, refile=True)
    worker = asyncio.create_task(pack._process_one())
    assert await asyncio.to_thread(entered.wait, 2)
    downloads = DownloadService(db, tmp_path / "work", stop_pack=pack.stop_attempt,
                               cleanup_removals=archive.process_removals)
    before = observed(db, job_id)
    if remove:
        result = await archive.purge_work(candidate_id)
        assert result["cleanup_status"] == "PENDING"
    else:
        await downloads.command(job_id, "cancel", **before)
    stopping = asyncio.create_task(downloads.process_operations())
    await asyncio.sleep(0.02)
    try:
        assert not stopping.done() and not exited.is_set()
        assert published.read_bytes() == original
    finally:
        release.set()
        await asyncio.gather(worker, stopping)
    assert exited.is_set() and stopping.result() == 1 and worker.result() is True
    assert published.read_bytes() == original
    assert not (tmp_path / "library" / "Changed" / "Book.cbz").exists()
    pin = await db.archive_path_pin(candidate_id)
    if remove:
        assert pin is None and await db.get_candidate(candidate_id) is None
    else:
        assert pin is None or pin["relative_path"] != "Changed/Book.cbz"
    assert (await downloads.get_task(before["task_id"]))["status"] == ("REMOVED" if remove else "CANCELLED")


@pytest.mark.asyncio
async def test_refile_pin_write_failure_restores_file_and_artifact_path(tmp_path):
    db, candidate_id, archive, _ = await packaged(tmp_path)
    published = tmp_path / "library" / "Archive Title.cbz"
    original = published.read_bytes()
    with db.connection() as c:
        original_path = c.execute("SELECT path FROM artifacts WHERE artifact_type = 'CBZ'").fetchone()[0]
        c.execute("CREATE TRIGGER reject_refile_pin BEFORE INSERT ON work_archive_paths "
                  "BEGIN SELECT RAISE(ABORT, 'injected pin failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected pin failure"):
        await archive.refile_work(candidate_id, "Changed/Book.cbz")
    assert published.read_bytes() == original
    assert not (tmp_path / "library" / "Changed" / "Book.cbz").exists()
    with db.connection() as c:
        assert c.execute("SELECT path FROM artifacts WHERE artifact_type = 'CBZ'").fetchone()[0] == original_path


@pytest.mark.asyncio
async def test_successful_refile_atomically_records_path_and_demotes_manual_pin(tmp_path):
    db, candidate_id, archive, _ = await packaged(tmp_path)
    published = tmp_path / "library" / "Archive Title.cbz"
    original = published.read_bytes()
    await db.set_archive_path_pin(candidate_id, "Archive Title.cbz", is_manual=True)
    moved = await archive.refile_work(candidate_id, "Changed/Book.cbz")
    assert moved["moved"] and not published.exists()
    assert (tmp_path / "library" / "Changed" / "Book.cbz").read_bytes() == original
    pin = await db.archive_path_pin(candidate_id)
    assert pin["relative_path"] == moved["relative_path"] and not pin["is_manual"]
    with db.connection() as c:
        assert c.execute("SELECT path, library_relative_path FROM artifacts WHERE artifact_type = 'CBZ'").fetchone() == (
            moved["path"], moved["relative_path"],
        )
