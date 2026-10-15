"""Drive the EH torrent route from selection through delivery.

The torrent branch is the preferred original-quality source: the payload is the
uploader's own archive and it costs no GP, unlike Archive Download. What makes
it different from every other provider is that the transfer does not happen in
this process — qBittorrent owns it — so a job cannot be finished inside one
worker turn. It is pushed, parked in ``WAITING_TORRENT``, and advanced by the
poller here until the client reports a complete payload.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
from pathlib import Path
import time

import httpx

from app.db.database import Database
from app.downloads.execution import attempt_path, current_execution, execution_scope, failure_transaction, run_sync
from app.downloads.queue import QueueControl, STALE_EXECUTION_ERRORS, task_snapshot
from app.torrent.client import QBittorrentClient
from app.torrent.delivery import resolve_content_path, take_delivery
from app.torrent.fetcher import TorrentFileFetcher
from app.connections.runtime import current_account_context
from app.exhentai.errors import ExHentaiDownloadError
from app.exhentai.health import ExHentaiHealth
from app.torrent.models import (
    TorrentClientConfig,
    TorrentDelivery,
    TorrentError,
    TorrentStatus,
)


PROVIDER_NAME = "EH_TORRENT"

#: Written to ComicInfo so an original-grade book is distinguishable from a
#: preview-grade one without opening it.
SCAN_INFORMATION_SOURCE = "EH_TORRENT"


def _mebibytes(total_bytes: int) -> str:
    return f"{total_bytes / (1024 * 1024):.1f}MiB"


def scan_information(size_bytes: int) -> str:
    return f"{SCAN_INFORMATION_SOURCE} original {_mebibytes(size_bytes)}"


class TorrentService:
    def __init__(
        self,
        database: Database,
        work_path: Path,
        *,
        config_provider,
        credentials_provider,
        http_client: httpx.AsyncClient | None = None,
        client_http_client: httpx.AsyncClient | None = None,
        poll_seconds: float = 15.0,
        work_path_provider=None,
        auto_pack=None,
        notify=None,
        account_scope=None,
    ) -> None:
        self._database = database
        self._work_path = work_path
        # Read per operation so a settings change applies without a restart.
        self._config_provider = config_provider
        self._credentials_provider = credentials_provider
        self._http_client = http_client
        self._client_http_client = client_http_client
        self._poll_seconds = poll_seconds
        self._work_path_provider = work_path_provider
        # Injected rather than imported so this module keeps knowing nothing
        # about the conversion queue; the torrent route only says "it is ready".
        self._auto_pack = auto_pack
        self._notify = notify
        self._account_scope = account_scope
        self._poller_task: asyncio.Task[None] | None = None
        self._active_attempts: dict[str, asyncio.Task] = {}

    async def _effective_work_path(self) -> Path:
        if self._work_path_provider is None:
            return self._work_path
        resolved = await self._work_path_provider()
        return resolved or self._work_path

    async def _config(self) -> TorrentClientConfig:
        config = await self._config_provider()
        if config is None or not config.is_configured:
            raise TorrentError(
                "TORRENT_CLIENT_NOT_CONFIG",
                "\u672a\u767b\u8bb0 qBittorrent \u5730\u5740",
            )
        return config

    def _client(self, config: TorrentClientConfig) -> QBittorrentClient:
        if self._client_http_client is None:
            raise TorrentError(
                "TORRENT_CLIENT_UNREACHABLE",
                "qBittorrent HTTP \u5ba2\u6237\u7aef\u672a\u914d\u7f6e",
            )
        return QBittorrentClient(self._client_http_client, config)

    async def check_connection(self) -> str:
        """Verify the operator's settings, used by the settings page button."""
        config = await self._config()
        return await self._client(config).version()

    async def push_for_candidate(self, candidate_id: int) -> dict:
        """Borrow EH credentials for the entire fetch/push, including intent writes."""
        if self._account_scope is not None and await run_sync(self._candidate_magnet_sync, candidate_id) is None:
            async with self._account_scope("EH", required=False):
                return await self._push_for_candidate(candidate_id)
        return await self._push_for_candidate(candidate_id)

    async def _push_for_candidate(self, candidate_id: int) -> dict:
        """Fetch the `.torrent` and hand it to the client.

        Returns the details the queue parks on the job. Nothing is downloaded
        here: the return of this call means the client accepted the torrent,
        not that a payload exists.
        """
        magnet = await run_sync(self._candidate_magnet_sync, candidate_id)
        if magnet is not None:
            magnet_url, digest = magnet
            config = await self._config()
            already_present = await self._push_owned(config, digest, magnet_url=magnet_url)
            logging.getLogger(__name__).info(
                "magnet_pushed candidate=%d hash=%s duplicate=%s",
                candidate_id,
                digest[:8],
                already_present,
            )
            details: dict = {
                "hash": digest,
                "magnet": True,
                "pushed_at": time.time(),
                "save_path": config.save_path,
            }
            if already_present:
                details["was_already_in_client"] = True
            return details
        gid, token, digest = await run_sync(
            self._candidate_torrent_sync, candidate_id
        )
        config = await self._config()
        credentials = await self._credentials_provider()
        if credentials is None:
            raise TorrentError(
                "TORRENT_FILE_FETCH_FAILED",
                "\u53d6 .torrent \u9700\u8981\u5df2\u914d\u7f6e\u7684 "
                "ExHentai Cookie",
            )
        if self._http_client is None:
            raise TorrentError(
                "TORRENT_FILE_FETCH_FAILED", "HTTP \u5ba2\u6237\u7aef\u672a\u914d\u7f6e"
            )
        try:
            await ExHentaiHealth(self._database).check_shared()
            payload = await TorrentFileFetcher(self._http_client).fetch(credentials, gid, token, digest)
        except ExHentaiDownloadError as exc:
            context = current_account_context.get()
            await ExHentaiHealth(self._database).record_failure(exc,
                account_id=context.account_id if context is not None else None,
                credential_revision=context.credential_revision if context is not None else None, capability="TORRENT")
            raise
        already_present = await self._push_owned(config, digest, payload=payload)
        logging.getLogger(__name__).info(
            "torrent_pushed candidate=%d hash=%s duplicate=%s",
            candidate_id,
            digest[:8],
            already_present,
        )
        details = {
            "hash": digest,
            "gid": gid,
            "pushed_at": time.time(),
            "save_path": config.save_path,
        }
        if already_present:
            # Surfaced rather than swallowed: the entry EhBot will read from was
            # created by someone else, so its save path and category are not the
            # ones just requested and delivery may look in the wrong place.
            details["was_already_in_client"] = True
        return details

    async def _push_owned(self, config, digest, *, magnet_url=None, payload=None) -> bool:
        """Persist a tagged intention before sending a remotely visible push."""
        client = self._client(config)
        execution = current_execution.get()
        if execution is None:
            if magnet_url is not None:
                return await client.add_magnet(magnet_url)
            return await client.add_torrent(payload, digest)
        present = await client.status(digest)
        tag = f"ehbot-{execution.attempt_id}"
        with self._database.connection() as connection:
            execution.check(connection)
        await run_sync(self._save_resource_sync, execution, config.base_url, digest, tag,
                       "SHARED" if present is not None else "UNKNOWN", "INTENDED")
        if present is not None:
            already_present = True
        else:
            request = asyncio.create_task(
                client.add_magnet(magnet_url, ownership_tag=tag) if magnet_url is not None
                else client.add_torrent(payload, digest, ownership_tag=tag)
            )
            cancelled = False
            try:
                while not request.done():
                    try:
                        await asyncio.shield(request)
                    except asyncio.CancelledError:
                        cancelled = True
                already_present = request.result()
            except TorrentError as exc:
                if exc.code in {"TORRENT_FILE_INVALID", "TORRENT_PUSH_REJECTED"}:
                    await run_sync(self._save_resource_sync, execution, config.base_url, digest, tag,
                                   "UNKNOWN", "STOP_CONFIRMED")
                raise
        if already_present:
            await run_sync(self._save_resource_sync, execution, config.base_url, digest, tag,
                           "SHARED", "STOP_CONFIRMED")
            raise TorrentError("TORRENT_RESOURCE_SHARED", "远端已有该种子且本项目不能独占停止，已拒绝附着为受管下载")
        else:
            registered = await client.status(digest)
            confirmed = registered is not None and tag in registered.tags
            ownership, resource_status = ("OWNED", "REGISTERED") if confirmed else ("UNKNOWN", "INTENDED")
        await run_sync(self._save_resource_sync, execution, config.base_url, digest, tag,
                       ownership, resource_status)
        if present is None and cancelled:
            raise asyncio.CancelledError
        return already_present

    def _save_resource_sync(self, execution, instance_url, digest, tag, ownership, status) -> None:
        # Resource bookkeeping must survive revocation while a push is in flight.
        # This narrow transaction changes no work, metadata or artifact.
        token = current_execution.set(None)
        try:
            with self._database.connection() as connection:
                if status == "INTENDED":
                    execution.check(connection)
                job_id = connection.execute("SELECT legacy_job_id FROM queue_tasks WHERE task_id = ?",
                                            (execution.task_id,)).fetchone()[0]
                connection.execute(
                    "INSERT INTO queue_remote_resources(resource_id, task_id, attempt_id, legacy_job_id, "
                    "instance_url, digest, ownership_tag, ownership, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(resource_id) DO UPDATE SET ownership = excluded.ownership, status = excluded.status, "
                    "updated_at = CURRENT_TIMESTAMP",
                    (execution.attempt_id, execution.task_id, execution.attempt_id, job_id, instance_url,
                     digest, tag, ownership, status),
                )
        finally:
            current_execution.reset(token)

    async def stop_for_operation(self, operation: dict) -> None:
        """Find interrupted pushes and confirm only resources this service owns."""
        task = self._active_attempts.get(operation["task_id"])
        if task is not None and task is not asyncio.current_task():
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        resources = await run_sync(self._resources_sync, operation["legacy_job_id"], operation["task_id"])
        details = json.loads(operation["resource_json"] or "{}")
        if not resources and not details.get("hash"):
            return
        config = await self._config()
        client = self._client(config)
        if not resources:
            if details.get("hash"):
                if await client.status(details["hash"]) is not None:
                    raise TorrentError("REMOTE_OWNERSHIP_UNKNOWN", "旧种子的资源归属不明，停止尚未确认")
            return
        for resource in resources:
            if resource["instance_url"] != config.base_url:
                raise TorrentError("REMOTE_INSTANCE_CHANGED", "远端实例已改变，必须恢复原连接才能确认停止")
            if resource["ownership"] != "SHARED":
                status = await client.status(resource["digest"])
                if status is None and resource["status"] == "INTENDED":
                    raise TorrentError("REMOTE_PUSH_UNCONFIRMED", "推送结果未知，不能仅凭暂时查不到种子确认停止")
                if status is not None:
                    if resource["ownership_tag"] not in status.tags:
                        raise TorrentError("REMOTE_OWNERSHIP_UNKNOWN", "远端资源归属无法核实，停止尚未确认")
                    await client.delete(resource["digest"], delete_files=False)
                    if await client.status(resource["digest"]) is not None:
                        raise TorrentError("REMOTE_STOP_NOT_CONFIRMED", "远端仍持有该任务，等待停止确认")
            await run_sync(self._confirm_resource_sync, resource["resource_id"])

    def _resources_sync(self, job_id: int, task_id: str | None = None) -> list[dict]:
        with self._database.connection() as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(
                "SELECT * FROM queue_remote_resources WHERE legacy_job_id = ? AND status <> 'STOP_CONFIRMED' "
                "AND (? IS NULL OR task_id = ?)",
                (job_id, task_id, task_id),
            )]

    def _confirm_resource_sync(self, resource_id: str) -> None:
        with self._database.connection() as connection:
            connection.execute("UPDATE queue_remote_resources SET status = 'STOP_CONFIRMED', "
                               "updated_at = CURRENT_TIMESTAMP WHERE resource_id = ?", (resource_id,))

    async def complete_if_ready(self, job_id: int) -> bool:
        """Finish a parked torrent whose payload is already readable on disk.

        The retry path tries the cheap outcome first: a job that failed with a
        content-read error is usually waiting on a path the operator has since
        fixed. If the client reports the transfer complete and the resolved
        save path is readable, the job is completed here without a fresh push.
        Any transient failure falls back to ``False`` so the caller re-pushes.
        Settings are re-read in this method so a corrected save path takes
        effect at once, satisfying the "re-read on every retry" contract.
        """
        candidate_id, details = await run_sync(
            self._job_candidate_details_sync, job_id
        )
        digest = str(details.get("hash") or "")
        if not digest:
            return False
        try:
            config = await self._config()
            status = await self._client(config).status(digest)
            if status is None or not status.is_complete or not status.content_path:
                return False
            content_path = resolve_content_path(
                status.content_path, config.save_path, config.local_save_path
            )
            if not os.path.exists(content_path) or not os.access(
                content_path, os.R_OK
            ):
                return False
            work_path = await self._effective_work_path()
            delivery = await run_sync(
                self._take_delivery_sync,
                candidate_id,
                status,
                config,
                work_path,
            )
            merged = self._merge_progress(details, status)
            merged["archive_path"] = delivery.archive_path
            merged["archive_bytes"] = delivery.size_bytes
            merged["packed_directory"] = delivery.was_directory
            merged["completed_at"] = time.time()
            merged["seeding"] = True
            merged["retried_complete"] = True
            await run_sync(
                self._complete_sync,
                job_id,
                candidate_id,
                delivery,
                merged,
            )
            if not config.keep_seeding:
                await self._cleanup_completed_resource(job_id, merged)
            if config.auto_pack:
                await self._queue_pack(candidate_id)
            return True
        except (TorrentError, httpx.HTTPError, OSError):
            # The client may be down or the payload still not quite readable;
            # either way the caller falls through to a normal re-push, which
            # surfaces the reason in the job's error state.
            return False

    async def abandon(self, job_id: int) -> None:
        """Legacy adapter to the same ownership check and confirmed stop path."""
        details = await run_sync(self._job_details_sync, job_id)
        with self._database.connection() as connection:
            snapshot = task_snapshot(connection, job_id)
        await self.stop_for_operation({"legacy_job_id": job_id, "task_id": snapshot["task_id"],
                                       "resource_json": json.dumps(details)})

    async def _cleanup_completed_resource(self, job_id: int, details: dict) -> None:
        try:
            await self.abandon(job_id)
        except TorrentError as exc:
            logging.getLogger(__name__).warning("torrent_cleanup_unconfirmed job=%d code=%s", job_id, exc.code)
            return
        details["seeding"] = False
        await run_sync(self._update_details_sync, job_id, details)

    async def start(self) -> None:
        if self._poller_task is not None:
            return
        self._poller_task = asyncio.create_task(
            self._run_poller(), name="torrent-poller"
        )

    async def stop(self) -> None:
        if self._poller_task is None:
            return
        self._poller_task.cancel()
        await asyncio.gather(self._poller_task, return_exceptions=True)
        self._poller_task = None

    async def _run_poller(self) -> None:
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - defensive worker loop
                logging.getLogger(__name__).exception(
                    "torrent_poller_error",
                    extra={"error_code": "TORRENT_POLLER_ERROR"},
                )
            await asyncio.sleep(self._poll_seconds)

    async def poll_once(self) -> int:
        """Advance every parked job once.

        Restart recovery needs no separate path: the parked jobs are read from
        the database each pass, so a process that comes back up re-attaches to
        whatever the client is still working on.
        """
        jobs = await run_sync(self._waiting_jobs_sync)
        if jobs:
            logging.getLogger(__name__).info(
                "torrent_poll_started jobs=%d", len(jobs)
            )
        logger = logging.getLogger(__name__)
        for job in jobs:
            job_context = {
                "job_id": job["job_id"],
                "candidate_id": job.get("candidate_id"),
            }
            try:
                await self._run_poll_attempt(job)
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
                continue
            except sqlite3.IntegrityError as exc:
                if str(exc) not in STALE_EXECUTION_ERRORS:
                    raise
                continue
            except TorrentError as exc:
                # A mapped provider error: the original message and code are
                # what an operator needs, but the traceback is not interesting
                # (it is one raise in `_advance_job`) and would only add noise.
                logger.warning(
                    "torrent_job_failed",
                    extra={
                        **job_context,
                        "error_code": exc.code,
                        "error_message": exc.public_message,
                    },
                )
                await self._fail_poll_attempt(
                    job,
                    job["job_id"],
                    exc.code,
                    exc.public_message,
                )
            except Exception as exc:  # noqa: BLE001 - provider boundary
                # The catch-all: the traceback is the only signal that says
                # which client call failed and why. `.exception(...)` keeps it
                # in the JSON payload as the `exception` field.
                logger.exception(
                    "torrent_job_exception",
                    extra={
                        **job_context,
                        "error_code": str(
                            getattr(exc, "code", "TORRENT_POLL_FAILED")
                        ),
                    },
                )
                await self._fail_poll_attempt(
                    job,
                    job["job_id"],
                    str(getattr(exc, "code", "TORRENT_POLL_FAILED")),
                    str(getattr(exc, "public_message", exc)),
                )
            if self._notify is not None:
                self._notify(job_id=job["job_id"], candidate_id=job["candidate_id"])
        return len(jobs)

    async def _fail_poll_attempt(self, job, job_id, code, message) -> None:
        with execution_scope(self._database.path, job):
            try:
                await run_sync(self._mark_failed_sync, job_id, code, message)
            except sqlite3.IntegrityError as exc:
                if str(exc) not in STALE_EXECUTION_ERRORS:
                    raise

    async def _run_poll_attempt(self, job: dict) -> None:
        async def advance():
            with execution_scope(self._database.path, job) as execution:
                with self._database.connection() as connection:
                    execution.check(connection)
                await self._advance_job(job)
        task = asyncio.create_task(advance(), name=f"torrent-{job['attempt_id']}")
        self._active_attempts[job["task_id"]] = task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            raise
        finally:
            self._active_attempts.pop(job["task_id"], None)

    async def _advance_job(self, job: dict) -> None:
        details = job["details"]
        digest = str(details.get("hash") or "")
        if not digest:
            raise TorrentError(
                "TORRENT_VANISHED", "\u4efb\u52a1\u6ca1\u6709\u8bb0\u5f55 infohash"
            )
        config = await self._config()
        status = await self._client(config).status(digest)
        if status is None:
            raise TorrentError(
                "TORRENT_VANISHED",
                f"qBittorrent \u91cc\u5df2\u65e0\u8be5\u79cd\u5b50 "
                f"{digest[:8]}\u2026",
            )
        if status.is_failed:
            raise TorrentError(
                "TORRENT_PUSH_REJECTED",
                f"qBittorrent \u62a5\u544a\u9519\u8bef\u72b6\u6001: "
                f"{status.state}",
            )
        await run_sync(self._observe_resource_sync, job["job_id"], status, config.base_url)
        merged = self._merge_progress(details, status)
        logging.getLogger(__name__).info(
            "torrent_progress job=%d hash=%s state=%s progress=%.1f%% "
            "seeds=%d dlspeed=%d",
            job["job_id"],
            digest[:8],
            status.state,
            status.progress * 100,
            status.num_seeds,
            status.dlspeed,
        )
        if not status.is_complete:
            await run_sync(
                self._update_details_sync, job["job_id"], merged
            )
            return
        delivery = await run_sync(
            self._take_delivery_sync,
            job["candidate_id"],
            status,
            config,
            await self._effective_work_path(),
        )
        merged["archive_path"] = delivery.archive_path
        merged["archive_bytes"] = delivery.size_bytes
        merged["packed_directory"] = delivery.was_directory
        merged["completed_at"] = time.time()
        # Recorded before the removal attempt so the finished job states what
        # was meant to happen to the seed even if the removal itself fails.
        merged["seeding"] = True
        await run_sync(
            self._complete_sync,
            job["job_id"],
            job["candidate_id"],
            delivery,
            merged,
        )
        if not config.keep_seeding:
            await self._cleanup_completed_resource(job["job_id"], merged)
        logging.getLogger(__name__).info(
            "torrent_download_completed candidate=%d bytes=%d",
            job["candidate_id"],
            delivery.size_bytes,
        )
        if config.auto_pack:
            await self._queue_pack(job["candidate_id"])

    def _observe_resource_sync(self, job_id: int, status: TorrentStatus, instance_url: str) -> None:
        """Confirm this attempt's remote identity before progress or delivery."""
        execution = current_execution.get()
        with self._database.connection() as connection:
            resource = connection.execute(
                "SELECT instance_url, ownership_tag FROM queue_remote_resources "
                "WHERE legacy_job_id = ? AND task_id = ? AND attempt_id = ? "
                "AND digest = ? AND ownership <> 'SHARED' AND status <> 'STOP_CONFIRMED'",
                (job_id, execution.task_id, execution.attempt_id, status.hash),
            ).fetchone()
            if resource is None or resource[1] not in status.tags:
                raise TorrentError("REMOTE_OWNERSHIP_UNKNOWN", "远端资源归属无法核实，不能提交进度或产物")
            if resource[0] != instance_url:
                raise TorrentError("REMOTE_INSTANCE_CHANGED", "远端实例已改变，不能提交进度或产物")
            connection.execute(
                "UPDATE queue_remote_resources SET ownership = 'OWNED', status = 'REGISTERED', "
                "updated_at = CURRENT_TIMESTAMP WHERE resource_id = ?",
                (execution.attempt_id,),
            )

    async def _queue_pack(self, candidate_id: int) -> None:
        """Hand a finished download to the conversion queue.

        A failure here is logged rather than raised: the download itself
        succeeded and its artifact is registered, so turning a packing hiccup
        into a failed download would misreport what happened and hide the
        archive the operator can still convert by hand.
        """
        if self._auto_pack is None:
            return
        try:
            await self._auto_pack(candidate_id)
        except Exception as exc:  # noqa: BLE001 - queue boundary
            logging.getLogger(__name__).warning(
                "torrent_auto_pack_failed candidate=%d error=%s",
                candidate_id,
                exc,
                extra={"error_code": "TORRENT_AUTO_PACK_FAILED"},
            )
            return
        logging.getLogger(__name__).info(
            "torrent_auto_pack_queued candidate=%d", candidate_id
        )

    @staticmethod
    def _merge_progress(details: dict, status: TorrentStatus) -> dict:
        """Fold one client observation into the job's stored progress.

        `stalled_since` is kept rather than recomputed so the dashboard can
        show how long a torrent has had no seeder, which is the number an
        operator needs to decide whether to keep waiting.
        """
        merged = dict(details)
        merged.update(
            {
                "state": status.state,
                "progress": round(status.progress, 4),
                "num_seeds": status.num_seeds,
                "dlspeed": status.dlspeed,
                "upspeed": status.upspeed,
                "eta": status.eta,
                "size": status.size,
                "polled_at": time.time(),
            }
        )
        if status.is_stalled:
            merged.setdefault("stalled_since", time.time())
        else:
            merged.pop("stalled_since", None)
        return merged

    def _take_delivery_sync(
        self,
        candidate_id: int,
        status: TorrentStatus,
        config: TorrentClientConfig,
        work_path: Path,
    ) -> TorrentDelivery:
        content_path = resolve_content_path(
            status.content_path, config.save_path, config.local_save_path
        )
        return take_delivery(
            content_path, attempt_path(work_path / "torrent" / "payload").parent, candidate_id
        )

    def _candidate_magnet_sync(
        self, candidate_id: int
    ) -> tuple[str, str] | None:
        """Return (magnet_url, btih) when the candidate was added by magnet."""
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT magnet_url, torrent_hash FROM candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()
        if row is None:
            raise TorrentError(
                "CANDIDATE_NOT_FOUND", "候选不存在或已被删除"
            )
        magnet_url = str(row[0]).strip() if row[0] else ""
        digest = str(row[1]).strip().lower() if row[1] else ""
        if not magnet_url:
            return None
        if not digest:
            raise TorrentError(
                "TORRENT_NOT_AVAILABLE",
                "磁力链接没有可用的 btih 哈希",
            )
        return magnet_url, digest

    def _candidate_torrent_sync(
        self, candidate_id: int
    ) -> tuple[int, str, str]:
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT ex_gid, ex_gallery_token, torrent_count, "
                "torrent_hash FROM candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()
        if row is None:
            raise TorrentError(
                "CANDIDATE_NOT_FOUND", "\u5019\u9009\u4e0d\u5b58\u5728\u6216\u5df2\u88ab\u5220\u9664"
            )
        if row[0] is None or not row[1]:
            raise TorrentError(
                "TORRENT_NOT_AVAILABLE",
                "\u5019\u9009\u6ca1\u6709\u5173\u8054\u7684 ExHentai \u753b\u5eca",
            )
        if not row[3]:
            # torrent_count is NULL until gdata answers, so the two cases are
            # reported apart: one is worth a metadata refresh, one never will be.
            raise TorrentError(
                "TORRENT_NOT_AVAILABLE",
                "\u8be5\u753b\u5eca\u65e0\u53ef\u7528\u79cd\u5b50"
                if row[2] is not None
                else "\u5c1a\u672a\u62c9\u53d6 gdata\uff0c\u79cd\u5b50\u4fe1\u606f\u672a\u77e5",
            )
        return int(row[0]), str(row[1]), str(row[3])

    def _job_candidate_details_sync(self, job_id: int) -> tuple[int, dict]:
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT candidate_id, details_json FROM download_jobs "
                "WHERE id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            return 0, {}
        try:
            details = json.loads(str(row[1] or "{}"))
        except ValueError:
            details = {}
        return int(row[0]), details if isinstance(details, dict) else {}

    def _job_details_sync(self, job_id: int) -> dict:
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT details_json FROM download_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            return {}
        try:
            details = json.loads(str(row[0] or "{}"))
        except ValueError:
            return {}
        return details if isinstance(details, dict) else {}

    def _waiting_jobs_sync(self) -> tuple[dict, ...]:
        with self._database.connection() as connection:
            rows = connection.execute(
                "SELECT id, candidate_id, details_json FROM download_jobs "
                "WHERE state = 'WAITING_TORRENT' AND provider = ? "
                "ORDER BY id",
                (PROVIDER_NAME,),
            ).fetchall()
            jobs: list[dict] = []
            for row in rows:
                try:
                    details = json.loads(str(row[2] or "{}"))
                except ValueError:
                    details = {}
                jobs.append({"job_id": int(row[0]), "candidate_id": int(row[1]),
                             "details": details if isinstance(details, dict) else {},
                             **task_snapshot(connection, int(row[0]))})
        return tuple(jobs)

    def _update_details_sync(self, job_id: int, details: dict) -> None:
        with self._database.connection() as connection:
            connection.execute(
                "UPDATE download_jobs SET details_json = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (
                    json.dumps(
                        details, separators=(",", ":"), ensure_ascii=False
                    ),
                    job_id,
                ),
            )

    def _complete_sync(
        self,
        job_id: int,
        candidate_id: int,
        delivery: TorrentDelivery,
        details: dict,
    ) -> None:
        """Register the archive and finish the job in one transaction.

        The artifact and the terminal state have to land together: a completed
        job with no artifact would make the conversion queue pick up the
        previous download instead of this one.
        """
        destination = Path(delivery.archive_path)
        sha256 = hashlib.sha256()
        with destination.open("rb") as handle:
            while True:
                chunk = handle.read(64 * 1024)
                if not chunk:
                    break
                sha256.update(chunk)
        details["sha256"] = sha256.hexdigest()
        with self._database.connection() as connection:
            connection.execute(
                "UPDATE download_jobs SET state = 'COMPLETED', "
                "details_json = ?, error_code = NULL, error_message = NULL, "
                "lease_owner = NULL, lease_expires_at = NULL, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (
                    json.dumps(
                        details, separators=(",", ":"), ensure_ascii=False
                    ),
                    job_id,
                ),
            )
            connection.execute(
                "INSERT INTO artifacts "
                "(job_id, artifact_type, path, sha256, size_bytes) "
                "VALUES (?, 'ARCHIVE', ?, ?, ?) "
                "ON CONFLICT(job_id, artifact_type) DO UPDATE SET "
                "path = excluded.path, sha256 = excluded.sha256, "
                "size_bytes = excluded.size_bytes",
                (
                    job_id,
                    delivery.archive_path,
                    sha256.hexdigest(),
                    int(delivery.size_bytes),
                ),
            )
            connection.execute(
                "INSERT INTO metadata_values "
                "(candidate_id, field_name, field_value, value_source, "
                "confidence, is_manual) "
                "VALUES (?, 'ScanInformation', ?, ?, 1.0, 0) "
                "ON CONFLICT(candidate_id, field_name, value_source) "
                "DO UPDATE SET field_value = excluded.field_value, "
                "created_at = CURRENT_TIMESTAMP "
                "WHERE metadata_values.is_manual = 0",
                (
                    candidate_id,
                    scan_information(delivery.size_bytes),
                    PROVIDER_NAME,
                ),
            )
            connection.execute(
                "UPDATE candidates SET status = 'DOWNLOADED', "
                "filter_reason = '', updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ?",
                (candidate_id,),
            )

    def _mark_failed_sync(
        self, job_id: int, code: str, message: str
    ) -> None:
        with failure_transaction(self._database) as connection:
            connection.execute(
                "UPDATE download_jobs SET state = 'FAILED', error_code = ?, "
                "error_message = ?, lease_owner = NULL, "
                "lease_expires_at = NULL, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ?",
                (code, message, job_id),
            )
            connection.execute(
                "UPDATE candidates SET status = 'FAILED', "
                "filter_reason = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = (SELECT candidate_id FROM download_jobs "
                "WHERE id = ?)",
                (message, job_id),
            )
            if connection.execute(
                "SELECT 1 FROM queue_remote_resources WHERE legacy_job_id = ? AND status <> 'STOP_CONFIRMED'",
                (job_id,),
            ).fetchone() is not None:
                current = task_snapshot(connection, job_id)
                QueueControl._operation(connection, job_id, "cancel", current, {})
                connection.execute("UPDATE download_jobs SET state = 'STOPPING', updated_at = CURRENT_TIMESTAMP "
                                   "WHERE id = ?", (job_id,))


__all__ = [
    "PROVIDER_NAME",
    "SCAN_INFORMATION_SOURCE",
    "TorrentError",
    "TorrentService",
    "scan_information",
]
