"""A real archive thread finishing after cancellation cannot replace the CBZ."""
import asyncio
from threading import Event

import pytest

from app.conversion.service import ConversionService
from app.db.database import Database
from app.downloads.service import DownloadService
from app.downloads.archived import ArchivedWorkService
from tests.integration.test_archive_workflow import seed_downloaded_archive, write_zip
from tests.integration.test_queue_control import observed


@pytest.mark.asyncio
@pytest.mark.parametrize("remove", [False, True])
async def test_stopped_repack_drains_thread_and_preserves_previous_archive(tmp_path, remove):
    db = Database(tmp_path / "queue.db")
    source = tmp_path / "work" / "downloads" / "comic.zip"
    write_zip(source, ("01.jpg",))
    candidate_id = await seed_downloaded_archive(db, source)
    pack = ConversionService(db, tmp_path / "work", tmp_path / "library", data_path=tmp_path / "data")
    await pack.enqueue_for_candidate(candidate_id)
    assert await pack._process_one()
    published = tmp_path / "library" / "Archive Title.cbz"
    original = published.read_bytes()
    write_zip(source, ("01.jpg", "02.jpg"))
    entered, released, exited = Event(), Event(), Event()
    build = pack._build_processor

    async def blocking_processor(quality, **kwargs):
        actual = await build(quality, **kwargs)

        class Processor:
            def process(self, *args, **kwargs):
                entered.set()
                assert released.wait(5)
                try:
                    return actual.process(*args, **kwargs)
                finally:
                    exited.set()

        return Processor()

    pack._build_processor = blocking_processor
    job_id = await pack.enqueue_for_candidate(candidate_id)
    worker = asyncio.create_task(pack._process_one())
    assert await asyncio.to_thread(entered.wait, 2)
    removal = ArchivedWorkService(db, tmp_path / "library", tmp_path / "work")
    downloads = DownloadService(db, tmp_path / "work", stop_pack=pack.stop_attempt,
                               cleanup_removals=removal.process_removals)
    task_id = observed(db, job_id)["task_id"]
    if remove:
        receipt = await removal.purge_work(candidate_id)
        assert receipt["cleanup_status"] == "PENDING" and await db.get_candidate(candidate_id) is None
    else:
        receipt = await downloads.command(job_id, "cancel", **observed(db, job_id))
    stopping = asyncio.create_task(downloads.process_operations())
    await asyncio.sleep(0.02)
    try:
        assert not stopping.done() and not exited.is_set()
        assert published.read_bytes() == original
    finally:
        released.set()
        await asyncio.gather(worker, stopping)
    assert exited.is_set() and stopping.result() == 1 and worker.result() is True
    assert published.read_bytes() == original
    assert (await downloads.get_task(task_id))["status"] == ("REMOVED" if remove else "CANCELLED")
    if remove:
        assert (await removal.get_removal(receipt["removal_id"]))["status"] == "COMPLETED"
        assert not list((tmp_path / "library").rglob(".ehbot-staging/**/*.cbz"))
    assert not list((tmp_path / "library").glob("Archive Title (2)*"))
