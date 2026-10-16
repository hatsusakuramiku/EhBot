"""Exercise tagged push, ambiguous requests and stop confirmation over HTTP."""
import asyncio
import sqlite3
from dataclasses import replace
from urllib.parse import parse_qsl

import httpx
import pytest

from app.downloads.service import DownloadError, DownloadService
from app.downloads.execution import execution_scope
from app.torrent.models import TorrentClientConfig
from app.torrent.service import TorrentService
from tests.integration.test_queue_control import database, observed, pending


DIGEST = "a" * 40


class Remote:
    def __init__(self):
        self.row = None
        self.hidden = False
        self.keep_after_delete = False
        self.ambiguous = False
        self.deleted = []
        self.added = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def request(self, request):
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(200, text="Ok.")
        if request.url.path.endswith("/torrents/info"):
            return httpx.Response(200, json=[] if self.hidden or self.row is None else [self.row])
        if request.url.path.endswith("/torrents/add"):
            self.added += 1
            self.entered.set()
            await self.release.wait()
            form = dict(parse_qsl(request.content.decode()))
            self.row = {"hash": DIGEST, "state": "downloading", "tags": form["tags"]}
            if self.ambiguous:
                self.hidden = True
                raise httpx.ReadTimeout("controlled lost acknowledgement", request=request)
            return httpx.Response(200, text="Ok.")
        if request.url.path.endswith("/torrents/delete"):
            self.deleted.append(dict(parse_qsl(request.content.decode())))
            if not self.keep_after_delete:
                self.row = None
            return httpx.Response(200)
        return httpx.Response(404)


def services(database, tmp_path, remote, client):
    config = TorrentClientConfig("http://remote.invalid", "test-user", "test-password")

    async def configuration():
        return config

    with database.connection() as c:
        c.execute("UPDATE candidates SET magnet_url = ?, torrent_hash = ? WHERE id = 1",
                  (f"magnet:?xt=urn:btih:{DIGEST}", DIGEST))
    torrent = TorrentService(database, tmp_path, config_provider=configuration,
                             credentials_provider=None, client_http_client=client)
    download = DownloadService(database, tmp_path, torrent_push=torrent.push_for_candidate,
                               torrent_stop=torrent.stop_for_operation,
                               telegraph_preflight=lambda _: asyncio.sleep(0))
    return download, torrent, config


@pytest.mark.asyncio
async def test_cancel_during_push_waits_for_ack_then_queries_and_stops_owned_resource(database, tmp_path):
    remote = Remote()
    remote.release.clear()
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote.request)) as client:
        service, torrent, _ = services(database, tmp_path, remote, client)
        job_id = pending(database, "EH_TORRENT")
        worker = asyncio.create_task(service._process_one())
        await asyncio.wait_for(remote.entered.wait(), 2)
        receipt = await service.command(job_id, "switch-source", provider="TELEGRAPH", **observed(database, job_id))
        stopping = asyncio.create_task(service.process_operations())
        await asyncio.sleep(0.02)
        assert not stopping.done() and remote.deleted == []
        with database.connection() as c:
            assert c.execute("SELECT status FROM queue_remote_resources").fetchone()[0] == "INTENDED"
            assert c.execute("SELECT state FROM download_jobs WHERE id = ?", (job_id,)).fetchone()[0] == "STOPPING"
        remote.release.set()
        assert await asyncio.wait_for(stopping, 2) == 1
        assert await worker is True
        assert remote.added == 1
        assert remote.deleted == [{"hashes": DIGEST, "deleteFiles": "false"}]
        assert (await service.get_task(receipt["task_id"]))["status"] == "CANCELLED"
        with database.connection() as c:
            assert c.execute("SELECT status FROM queue_remote_resources").fetchone()[0] == "STOP_CONFIRMED"


@pytest.mark.asyncio
async def test_remote_http_success_is_not_stop_confirmation(database, tmp_path):
    remote = Remote()
    remote.keep_after_delete = True
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote.request)) as client:
        service, torrent, _ = services(database, tmp_path, remote, client)
        job_id = pending(database, "EH_TORRENT")
        assert await service._process_one()
        receipt = await service.command(job_id, "cancel", **observed(database, job_id))
        assert await service.process_operations() == 0
        snapshot = await service.get_task(receipt["task_id"])
        assert snapshot["status"] == "STOPPING"
        assert snapshot["operations"][0]["error_code"] == "REMOTE_STOP_NOT_CONFIRMED"
        remote.keep_after_delete = False
        assert await service.process_operations() == 1


@pytest.mark.asyncio
async def test_unknown_push_absence_is_not_confirmation_and_can_be_found_after_restart(database, tmp_path):
    remote = Remote()
    remote.ambiguous = True
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote.request)) as client:
        service, torrent, _ = services(database, tmp_path, remote, client)
        job_id = pending(database, "EH_TORRENT")
        await service._process_one()
        draft = observed(database, job_id)
        snapshot = await service.get_task(draft["task_id"])
        assert snapshot["status"] == "STOPPING"
        assert snapshot["attempts"][0]["status"] == "FAILED"
        with pytest.raises(DownloadError) as conflict:
            await service.enqueue_telegraph_download(1)
        assert conflict.value.code == "WORK_SLOT_OCCUPIED"
        assert await service.process_operations() == 0
        snapshot = await service.get_task(draft["task_id"])
        assert snapshot["operations"][0]["error_code"] == "REMOTE_PUSH_UNCONFIRMED"
        remote.hidden = False
        restarted, _, _ = services(database, tmp_path, remote, client)
        assert await restarted.process_operations() == 1
        assert remote.added == 1 and len(remote.deleted) == 1


@pytest.mark.asyncio
async def test_shared_external_torrent_is_refused_before_managed_attachment(database, tmp_path):
    remote = Remote()
    remote.row = {"hash": DIGEST, "state": "downloading", "tags": "another-application"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote.request)) as client:
        service, torrent, _ = services(database, tmp_path, remote, client)
        job_id = pending(database, "EH_TORRENT")
        await service._process_one()
        jobs = await service.list_jobs_for_candidate(1)
        assert jobs[0].state == "FAILED" and jobs[0].error_code == "TORRENT_RESOURCE_SHARED"
        with database.connection() as c:
            assert c.execute("SELECT ownership, status FROM queue_remote_resources").fetchone() == ("SHARED", "STOP_CONFIRMED")
        assert remote.added == 0 and remote.deleted == [] and remote.row is not None
        assert (await service.enqueue_telegraph_download(1)).created


@pytest.mark.asyncio
async def test_legacy_shared_association_is_detached_without_deleting_external_torrent(database, tmp_path):
    remote = Remote()
    remote.row = {"hash": DIGEST, "state": "downloading", "tags": "another-application"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote.request)) as client:
        service, _, _ = services(database, tmp_path, remote, client)
        job_id = pending(database, "EH_TORRENT")
        original = service._claim_pending_job_sync()
        service._park_job_sync(job_id, {"hash": DIGEST})
        with database.connection() as c:
            c.execute("INSERT INTO queue_remote_resources(resource_id, task_id, attempt_id, legacy_job_id, "
                      "instance_url, digest, ownership_tag, ownership, status) "
                      "VALUES('legacy-shared', ?, ?, ?, 'http://remote.invalid', ?, 'foreign', 'SHARED', 'REGISTERED')",
                      (original["task_id"], original["attempt_id"], job_id, DIGEST))
        await service.command(job_id, "cancel", **observed(database, job_id))
        assert await service.process_operations() == 1
        assert remote.added == 0 and remote.deleted == [] and remote.row is not None


@pytest.mark.asyncio
async def test_accepted_push_is_not_registered_until_ownership_is_observed(database, tmp_path):
    remote = Remote()
    remote.hidden = True
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote.request)) as client:
        service, torrent, _ = services(database, tmp_path, remote, client)
        job_id = pending(database, "EH_TORRENT")
        await service._process_one()
        with database.connection() as c:
            assert c.execute("SELECT ownership, status FROM queue_remote_resources").fetchone() == ("UNKNOWN", "INTENDED")
        receipt = await service.command(job_id, "cancel", **observed(database, job_id))
        assert await service.process_operations() == 0
        assert (await service.get_task(receipt["task_id"]))["status"] == "STOPPING"
        remote.hidden = False
        assert await service.process_operations() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["unconfirmed", "ownership", "instance"])
async def test_poller_cannot_deliver_without_current_remote_ownership(database, tmp_path, change):
    remote = Remote()
    remote.hidden = change == "unconfirmed"
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote.request)) as client:
        service, torrent, config = services(database, tmp_path, remote, client)
        job_id = pending(database, "EH_TORRENT")
        assert await service._process_one()
        remote.hidden = False
        remote.row.update(state="uploading", progress=1.0, content_path=str(tmp_path / "foreign.zip"))
        if change in {"unconfirmed", "ownership"}:
            remote.row["tags"] = "another-application"
        else:
            async def replacement_config():
                return replace(config, base_url="http://other-instance.invalid")
            torrent._config_provider = replacement_config
        assert await torrent.poll_once() == 1
        snapshot = await service.get_task(observed(database, job_id)["task_id"])
        assert snapshot["status"] == "STOPPING"
        expected = "REMOTE_INSTANCE_CHANGED" if change == "instance" else "REMOTE_OWNERSHIP_UNKNOWN"
        assert snapshot["attempts"][0]["error_code"] == expected
        with database.connection() as c:
            assert c.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
        assert remote.deleted == [] and remote.added == 1
        assert await service.process_operations() == 0
        with pytest.raises(DownloadError) as conflict:
            await service.enqueue_telegraph_download(1)
        assert conflict.value.code == "WORK_SLOT_OCCUPIED"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["ownership", "instance"])
async def test_changed_remote_identity_cannot_be_deleted_or_release_slot(database, tmp_path, change):
    remote = Remote()
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote.request)) as client:
        service, torrent, config = services(database, tmp_path, remote, client)
        job_id = pending(database, "EH_TORRENT")
        await service._process_one()
        if change == "ownership":
            remote.row["tags"] = "another-application"
        else:
            async def changed_config():
                return replace(config, base_url="http://other.invalid")
            torrent._config_provider = changed_config
        receipt = await service.command(job_id, "cancel", **observed(database, job_id))
        assert await service.process_operations() == 0
        assert (await service.get_task(receipt["task_id"]))["status"] == "STOPPING"
        assert remote.deleted == []


@pytest.mark.asyncio
async def test_config_change_during_push_retains_original_instance_until_restored(database, tmp_path):
    remote = Remote()
    remote.release.clear()
    hosts = []

    async def request(message):
        hosts.append(message.url.host)
        return await remote.request(message)

    async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as client:
        service, torrent, original = services(database, tmp_path, remote, client)
        job_id = pending(database, 'EH_TORRENT')
        worker = asyncio.create_task(service._process_one())
        await asyncio.wait_for(remote.entered.wait(), 2)

        async def changed_config():
            return replace(original, base_url='http://new-instance.invalid')

        torrent._config_provider = changed_config
        remote.release.set()
        assert await asyncio.wait_for(worker, 2)
        assert set(hosts) == {'remote.invalid'}
        with database.connection() as connection:
            assert connection.execute('SELECT instance_url, ownership FROM queue_remote_resources').fetchone() == (
                original.base_url, 'OWNED')
        receipt = await service.command(job_id, 'cancel', **observed(database, job_id))
        assert await service.process_operations() == 0
        assert remote.deleted == [] and set(hosts) == {'remote.invalid'}
        assert (await service.get_task(receipt['task_id']))['status'] == 'STOPPING'

        async def restored_config():
            # Repairing credentials or mappings on the original instance must
            # remain possible while its old resource occupies the work slot.
            return replace(original, username='repaired-user', local_save_path=str(tmp_path))

        torrent._config_provider = restored_config
        assert await service.process_operations() == 1
        assert (await service.get_task(receipt['task_id']))['status'] == 'CANCELLED'
        assert remote.added == 1 and len(remote.deleted) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["INTENDED", "REGISTERED"])
async def test_restart_after_remote_push_before_park_stops_old_transfer_before_requeue(database, tmp_path, phase):
    remote = Remote()
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote.request)) as client:
        service, torrent, _ = services(database, tmp_path, remote, client)
        job_id = pending(database, "EH_TORRENT")
        original = service._claim_pending_job_sync()
        with execution_scope(database.path, original):
            await torrent.push_for_candidate(1)
        # Crash between remote acknowledgement and writing WAITING_TORRENT.
        with database.connection() as c:
            c.execute("UPDATE queue_remote_resources SET status = ?, ownership = ?",
                      (phase, "UNKNOWN" if phase == "INTENDED" else "OWNED"))
            c.execute("UPDATE download_jobs SET lease_expires_at = datetime('now', '-1 minute')")
        restarted, _, _ = services(database, tmp_path, remote, client)
        assert await restarted.reclaim_expired_leases() == 1
        assert await restarted.reclaim_expired_leases() == 0
        assert restarted._claim_pending_job_sync() is None
        snapshot = await restarted.get_task(original["task_id"])
        assert snapshot["status"] == "STOPPING" and snapshot["operations"][0]["action"] == "restart"
        assert remote.added == 1 and not remote.deleted
        remote.keep_after_delete = True
        assert await restarted.process_operations() == 0
        assert restarted._claim_pending_job_sync() is None and remote.added == 1
        remote.keep_after_delete = False
        assert await restarted.process_operations() == 1
        jobs = await restarted.list_jobs_for_candidate(1)
        assert jobs[0].state == "PENDING" and jobs[0].task_id != original["task_id"]
        assert await restarted._process_one()
        assert remote.added == 2


def test_legacy_reclaim_writer_cannot_requeue_unresolved_push(database, tmp_path):
    service = DownloadService(database, tmp_path)
    job_id = pending(database, "EH_TORRENT")
    original = service._claim_pending_job_sync()
    with database.connection() as c:
        c.execute("INSERT INTO queue_remote_resources(resource_id, task_id, attempt_id, legacy_job_id, "
                  "instance_url, digest, ownership_tag, ownership, status) "
                  "VALUES('interrupted', ?, ?, ?, 'http://remote.invalid', ?, 'owned-tag', 'UNKNOWN', 'INTENDED')",
                  (original["task_id"], original["attempt_id"], job_id, DIGEST))
    with pytest.raises(sqlite3.IntegrityError, match="QUEUE_REMOTE_RECONCILIATION_REQUIRED"):
        with database.connection() as c:
            c.execute("UPDATE download_jobs SET state = 'PENDING' WHERE id = ?", (job_id,))
