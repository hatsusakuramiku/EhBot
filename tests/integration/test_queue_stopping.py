"""Physical cleanup must finish before a durable stop releases the work slot."""
import asyncio

import pytest

from app.downloads.service import DownloadError, DownloadService
from tests.integration.test_queue_control import database, observed, pending


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["cancel", "switch-source"])
async def test_stop_waits_for_provider_cleanup_before_releasing_slot(database, tmp_path, action):
    entered, cancelling, release, exited = (asyncio.Event() for _ in range(4))

    async def download(candidate_id):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelling.set()
            await release.wait()
            exited.set()

    service = DownloadService(database, tmp_path, exhentai_download=download,
                              telegraph_preflight=lambda _: asyncio.sleep(0))
    job_id = pending(database, "EXHENTAI")
    worker = asyncio.create_task(service._process_one())
    await asyncio.wait_for(entered.wait(), 2)
    params = {"provider": "TELEGRAPH"} if action == "switch-source" else {}
    receipt = await service.command(job_id, action, **observed(database, job_id), **params)
    operation = asyncio.create_task(service.process_operations())
    await asyncio.wait_for(cancelling.wait(), 2)
    try:
        assert not operation.done() and not exited.is_set()
        with pytest.raises(DownloadError) as error:
            await service.enqueue_telegraph_download(1)
        assert error.value.code == "WORK_SLOT_OCCUPIED"
        with database.connection() as c:
            assert c.execute("SELECT state FROM download_jobs WHERE id = ?", (job_id,)).fetchone()[0] == "STOPPING"
    finally:
        release.set()
        await asyncio.gather(worker, operation)
    assert operation.result() == 1 and worker.result() is True and exited.is_set()
    task = await service.get_task(receipt["task_id"])
    assert task["status"] == "CANCELLED"
    assert task["operations"][0]["status"] == "COMPLETED"
    if action == "switch-source":
        with database.connection() as c:
            assert c.execute("SELECT state FROM download_jobs WHERE provider = 'TELEGRAPH'").fetchone()[0] == "PENDING"
    assert await service.process_operations() == 0


@pytest.mark.asyncio
async def test_unconfirmed_remote_stop_keeps_slot_and_retries_durable_intent(database, tmp_path):
    reachable = False
    calls = []

    async def stop_remote(operation):
        calls.append(operation["operation_id"])
        if not reachable:
            raise DownloadError("REMOTE_UNREACHABLE", "controlled remote outage")

    service = DownloadService(database, tmp_path, torrent_stop=stop_remote,
                              telegraph_preflight=lambda _: asyncio.sleep(0))
    job_id = pending(database, "EH_TORRENT")
    service._claim_pending_job_sync()
    service._park_job_sync(job_id, {"hash": "a" * 40})
    receipt = await service.command(job_id, "switch-source", provider="TELEGRAPH", **observed(database, job_id))
    assert await service.process_operations() == 0
    snapshot = await service.get_task(receipt["task_id"])
    assert snapshot["status"] == "STOPPING"
    assert snapshot["operations"][0]["error_code"] == "REMOTE_UNREACHABLE"
    with pytest.raises(DownloadError):
        await service.enqueue_telegraph_download(1)
    reachable = True
    # A new service can resume the exact saved intention after restart.
    restarted = DownloadService(database, tmp_path, torrent_stop=stop_remote)
    assert await restarted.process_operations() == 1
    assert calls == [receipt["operation_id"], receipt["operation_id"]]


@pytest.mark.asyncio
async def test_successor_enqueue_failure_rolls_back_slot_and_operation(database, tmp_path):
    service = DownloadService(database, tmp_path, telegraph_preflight=lambda _: asyncio.sleep(0))
    job_id = pending(database, "EXHENTAI")
    service._claim_pending_job_sync()
    receipt = await service.command(job_id, "switch-source", provider="TELEGRAPH", **observed(database, job_id))
    with database.connection() as c:
        c.execute("CREATE TRIGGER injected_queue_failure BEFORE INSERT ON download_jobs "
                  "WHEN NEW.provider = 'TELEGRAPH' BEGIN SELECT RAISE(ABORT, 'injected'); END")
    assert await service.process_operations() == 0
    task = await service.get_task(receipt["task_id"])
    assert task["status"] == "STOPPING" and task["operations"][0]["status"] == "PENDING"
    with database.connection() as c:
        assert c.execute("SELECT count(*) FROM download_jobs").fetchone()[0] == 1
        c.execute("DROP TRIGGER injected_queue_failure")
    assert await service.process_operations() == 1
