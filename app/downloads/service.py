from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
import time

from app.connections.models import ProviderConnectionError
from app.connections.telegram import TelegramBotApi
from app.db.database import Database
from app.downloads.models import (
    ACTIVE_DOWNLOAD_STATES,
    CONVERSION_STATE_COMPLETED,
    CONVERSION_STATE_FAILED,
    DOWNLOAD_STATE_CANCELLED,
    DOWNLOAD_STATE_COMPLETED,
    DOWNLOAD_STATE_DOWNLOADING,
    DOWNLOAD_STATE_FAILED,
    DOWNLOAD_STATE_PAUSED,
    DOWNLOAD_STATE_PENDING,
    DOWNLOAD_STATE_WAITING_TORRENT,
    MAX_JOB_PRIORITY,
    MIN_JOB_PRIORITY,
    NEEDS_INFO_DOWNLOAD_ERRORS,
    OPEN_DOWNLOAD_STATES,
    PERMANENT_DOWNLOAD_ERRORS,
    PROVIDER_CONVERSION,
    PROVIDER_EH_TORRENT,
    PROVIDER_EXHENTAI,
    PROVIDER_TELEGRAM,
    PROVIDER_TELEGRAM_USER,
    PROVIDER_TELEGRAPH,
    SUPPORTED_PROVIDERS,
    TERMINAL_DOWNLOAD_STATES,
    DownloadEnqueueResult,
    DownloadJobSummary,
    DownloadState,
)


class DownloadError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message


class DownloadService:
    def __init__(
        self,
        database: Database,
        work_path: Path,
        telegram_client_factory=None,
        telegram_user_client=None,
        exhentai_download=None,
        telegraph_download=None,
        torrent_push=None,
        torrent_abandon=None,
        torrent_verify=None,
        auto_pack=None,
        auto_pack_enabled=None,
        work_path_provider=None,
        notify=None,
    ) -> None:
        self._database = database
        self._work_path = work_path
        self._telegram_client_factory = telegram_client_factory
        # Resolved per delivery, like the bot token: the operator can log the
        # user account in, or Telegram can revoke the session, between one job
        # and the next. Returns None when no account is configured, which is the
        # normal state for a deployment that only ever fetches small files.
        self._telegram_user_client = telegram_user_client
        self._exhentai_download = exhentai_download
        self._telegraph_download = telegraph_download
        # Pushing hands the transfer to qBittorrent; abandoning takes it back
        # out when the operator cancels or switches sources. Verifying asks the
        # torrent service whether a finished payload is already readable on
        # disk so a retry can complete without a fresh push. Auto-pack hands a
        # finished download to the conversion queue.
        self._torrent_push = torrent_push
        self._torrent_abandon = torrent_abandon
        self._torrent_verify = torrent_verify
        self._auto_pack = auto_pack
        self._auto_pack_enabled = auto_pack_enabled
        # Resolved per job so an operator directory change applies without a
        # restart; the constructor value stays the default.
        self._work_path_provider = work_path_provider
        # Called with a job id after the worker finishes a delivery, so the
        # interface can refresh without polling. Optional, and deliberately
        # fire-and-forget: this service must keep working with nothing attached.
        self._notify = notify
        self._worker_task: asyncio.Task[None] | None = None

    async def _effective_work_path(self) -> Path:
        if self._work_path_provider is None:
            return self._work_path
        resolved = await self._work_path_provider()
        return resolved or self._work_path

    async def enqueue_telegram_download(
        self, candidate_id: int, attachment: dict
    ) -> DownloadEnqueueResult:
        file_id = str(attachment.get("file_id") or "")
        if not file_id:
            raise DownloadError(
                "ATTACHMENT_INVALID",
                "Candidate attachment is missing a Telegram file id",
            )
        idempotency_key = (
            f"telegram:{candidate_id}:"
            f"{attachment.get('file_unique_id') or file_id}"
        )
        details = {
            "file_id": file_id,
            "file_name": attachment.get("file_name"),
            "file_unique_id": attachment.get("file_unique_id"),
        }
        return await self._enqueue(
            candidate_id,
            PROVIDER_TELEGRAM,
            idempotency_key,
            json.dumps(details, separators=(",", ":")),
        )

    async def enqueue_telegram_user_download(
        self, candidate_id: int, attachment: dict
    ) -> DownloadEnqueueResult:
        """Queue the MTProto route for one attachment.

        The idempotency key names the provider, so the same attachment can hold
        one bot job and one user job: an operator whose 20 MB attempt failed asks
        for the user route on the same candidate, and a shared key would make
        that a no-op that looks like nothing happened.
        """
        file_unique_id = str(attachment.get("file_unique_id") or "")
        chat_id = attachment.get("chat_id")
        message_id = attachment.get("message_id")
        if chat_id is None or message_id is None:
            # Not fatal: `locate_candidate_message` recovers both numbers from
            # `source_messages` for attachments ingested before they were stored
            # inline. Resolved at enqueue time so a job that can never run is
            # refused where the operator is looking, not hours later in a worker.
            located = await self._database.locate_candidate_message(
                candidate_id, file_unique_id or None
            )
            if located is None:
                raise DownloadError(
                    "ATTACHMENT_INVALID",
                    "找不到该附件所在的源消息，无法用用户账户下载",
                )
            chat_id, message_id = located
        details = {
            "chat_id": int(chat_id),
            "message_id": int(message_id),
            "file_name": attachment.get("file_name"),
            "file_unique_id": attachment.get("file_unique_id"),
            "size_bytes": int(attachment.get("size_bytes") or 0),
        }
        return await self._enqueue(
            candidate_id,
            PROVIDER_TELEGRAM_USER,
            f"telegram-user:{candidate_id}:{file_unique_id or message_id}",
            json.dumps(details, separators=(",", ":")),
        )

    async def enqueue_exhentai_download(
        self, candidate_id: int
    ) -> DownloadEnqueueResult:
        return await self._enqueue(
            candidate_id,
            PROVIDER_EXHENTAI,
            f"exhentai:{candidate_id}",
            "{}",
        )

    async def enqueue_telegraph_download(
        self, candidate_id: int
    ) -> DownloadEnqueueResult:
        """Queue the preview-page fallback for a candidate.

        Reading-grade by design: the page images are 1280 px re-encodes, so
        this route is only entered once the original-quality routes are out.
        """
        return await self._enqueue(
            candidate_id,
            PROVIDER_TELEGRAPH,
            f"telegraph:{candidate_id}",
            "{}",
        )

    async def enqueue_torrent_download(
        self, candidate_id: int
    ) -> DownloadEnqueueResult:
        """Queue the EH torrent route, the preferred original-quality source.

        Free where Archive Download costs GP, and not subject to the 20 MB Bot
        API limit, so this is the first choice for an oversized book.
        """
        return await self._enqueue(
            candidate_id,
            PROVIDER_EH_TORRENT,
            f"torrent:{candidate_id}",
            "{}",
        )

    async def _enqueue(
        self,
        candidate_id: int,
        provider: str,
        idempotency_key: str,
        details_json: str,
    ) -> DownloadEnqueueResult:
        return await asyncio.to_thread(
            self._enqueue_sync,
            candidate_id,
            provider,
            idempotency_key,
            details_json,
        )

    def _enqueue_sync(
        self,
        candidate_id: int,
        provider: str,
        idempotency_key: str,
        details_json: str,
    ) -> DownloadEnqueueResult:
        with self._database.connection() as connection:
            candidate_row = connection.execute(
                "SELECT status FROM candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()
            if candidate_row is None:
                raise DownloadError(
                    "CANDIDATE_NOT_FOUND",
                    "Candidate does not exist",
                )
            # A candidate may spawn a new download while it is APPROVED and
            # while it is DOWNLOADED (an operator re-fetching another source or
            # re-running a finished job). Blocking DOWNLOADED is what produced
            # the misleading "必须审批后才能进入下载队列" error after a job had
            # already been approved and completed.
            if str(candidate_row[0]) not in {"APPROVED", "DOWNLOADED"}:
                raise DownloadError(
                    "CANDIDATE_NOT_DOWNLOADABLE",
                    "该候选尚未通过审批，不能进入下载队列",
                )
            existing = connection.execute(
                "SELECT state FROM download_jobs WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            # A second enqueue of the same source updates the existing row back
            # to PENDING when that row is FAILED or CANCELLED, instead of doing
            # nothing. `DO NOTHING` made the whole recovery path silent: a
            # requeued candidate that was approved again reported success while
            # its dead job row stayed FAILED, so the book never downloaded and
            # the page showed a failure the operator had just acted on.
            #
            # COMPLETED and the open states are left alone deliberately -- the
            # first is `redownload_work`'s job (it bumps `attempt_count` and is
            # an explicit operator decision), and re-pending a job the worker
            # holds would hand the same transfer out twice.
            connection.execute(
                "INSERT INTO download_jobs "
                "(candidate_id, idempotency_key, provider, state, "
                "details_json) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(idempotency_key) DO UPDATE SET "
                "  state = excluded.state, "
                "  details_json = excluded.details_json, "
                "  error_code = NULL, "
                "  error_message = NULL, "
                "  lease_owner = NULL, "
                "  lease_expires_at = NULL, "
                "  retry_at = NULL, "
                "  updated_at = CURRENT_TIMESTAMP "
                "WHERE download_jobs.state IN (?, ?)",
                (
                    candidate_id,
                    idempotency_key,
                    provider,
                    DOWNLOAD_STATE_PENDING,
                    details_json,
                    DOWNLOAD_STATE_FAILED,
                    DOWNLOAD_STATE_CANCELLED,
                ),
            )
            # Read from the pre-flight SELECT rather than from the row count:
            # the upsert changes a row when it revives one too, and `created`
            # answers 「这是一条新任务吗」 for the caller's flash message.
            created = existing is None
            row = connection.execute(
                "SELECT id, state FROM download_jobs "
                "WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            return DownloadEnqueueResult(
                job_id=int(row[0]),
                created=created,
                state=str(row[1]),
            )

    async def list_jobs_for_candidate(
        self, candidate_id: int
    ) -> tuple[DownloadJobSummary, ...]:
        return await asyncio.to_thread(
            self._list_jobs_sync, candidate_id
        )

    def _list_jobs_sync(
        self, candidate_id: int
    ) -> tuple[DownloadJobSummary, ...]:
        return self._fetch_jobs(
            "WHERE candidate_id = ? ORDER BY id DESC",
            (candidate_id,),
        )

    async def list_active_pack_jobs(self) -> tuple[DownloadJobSummary, ...]:
        """In-flight conversion (packaging) jobs for the downloads dashboard."""
        return await asyncio.to_thread(self._list_active_pack_jobs_sync)

    def _list_active_pack_jobs_sync(self) -> tuple[DownloadJobSummary, ...]:
        return self._fetch_jobs(
            "WHERE provider = ? AND state IN (?, ?, ?, ?) "
            "ORDER BY id DESC LIMIT 50",
            (
                PROVIDER_CONVERSION,
                "CONVERSION_PENDING",
                "CONVERSION_RUNNING",
                "CONVERSION_WAITING_VOLUMES",
                "CONVERSION_WAITING_PASSWORD",
            ),
        )

    async def list_history_jobs(
        self, limit: int = 100
    ) -> tuple[DownloadJobSummary, ...]:
        """Past, finished download tasks for the history/archive page.

        Terminal rows are never deleted from ``download_jobs``, so the archive
        is a query here rather than a separate table: every COMPLETED / FAILED
        / CANCELLED job shows up, newest first.
        """
        return await asyncio.to_thread(
            self._list_history_jobs_sync, int(limit)
        )

    def _list_history_jobs_sync(self, limit: int) -> tuple[DownloadJobSummary, ...]:
        return self._fetch_jobs(
            "WHERE state IN (?, ?, ?) ORDER BY id DESC LIMIT ?",
            (
                DOWNLOAD_STATE_COMPLETED,
                DOWNLOAD_STATE_FAILED,
                DOWNLOAD_STATE_CANCELLED,
                limit,
            ),
        )

    async def list_active_jobs(self) -> tuple[DownloadJobSummary, ...]:
        return await asyncio.to_thread(self._list_active_jobs_sync)

    def _list_active_jobs_sync(self) -> tuple[DownloadJobSummary, ...]:
        states = tuple(sorted(OPEN_DOWNLOAD_STATES))
        placeholders = ",".join("?" for _ in states)
        # A completed torrent whose payload is still being shared is included
        # even though the job itself is done: the client is still using the
        # operator's bandwidth and disk for it, so it stays visible until the
        # seed is removed. Other providers have nothing left running.
        return self._fetch_jobs(
            "WHERE state IN ("
            + placeholders
            + ") OR (state = ? AND provider = ? "
            "AND details_json LIKE '%\"seeding\":true%') "
            "ORDER BY id DESC LIMIT 50",
            states + (DOWNLOAD_STATE_COMPLETED, PROVIDER_EH_TORRENT),
        )

    async def retry_job(self, job_id: int) -> str:
        """Requeue a failed or paused job without creating a duplicate.

        The torrent route tries the cheap outcome first: if the saved payload
        is already readable on disk (an operator corrected the save path after
        a content-read failure), the job is completed in place instead of being
        pushed again. Otherwise the stale client entry is removed and the job
        re-enters the queue, which re-reads the latest settings on the next
        push. The original row is reused so the `idempotency_key` contract
        holds and the attempt history is preserved.
        """
        provider, state = await asyncio.to_thread(
            self._job_provider_state_sync, job_id
        )
        if (
            provider == PROVIDER_EH_TORRENT
            and self._torrent_verify is not None
        ):
            if await self._torrent_verify(job_id):
                return DOWNLOAD_STATE_COMPLETED
        # A retry re-pushes the torrent, and qBittorrent absorbs a duplicate
        # hash, so the stale entry is removed first to keep one job to one
        # client entry rather than relying on that.
        await self._abandon_torrent(job_id)
        return await asyncio.to_thread(self._retry_job_sync, job_id)

    def _retry_job_sync(self, job_id: int) -> str:
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT state, error_code, candidate_id FROM download_jobs "
                "WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise DownloadError("JOB_NOT_FOUND", "下载任务不存在")
            state = str(row[0])
            if state not in {
                DOWNLOAD_STATE_FAILED,
                DOWNLOAD_STATE_PAUSED,
                DOWNLOAD_STATE_CANCELLED,
            }:
                raise DownloadError(
                    "JOB_NOT_RETRYABLE",
                    "只有已失败、已暂停或已取消的任务可以重试",
                )
            if row[1] is not None and str(row[1]) in PERMANENT_DOWNLOAD_ERRORS:
                raise DownloadError(
                    "JOB_PERMANENTLY_FAILED",
                    "该任务的失败原因无法通过重试解决",
                )
            connection.execute(
                "UPDATE download_jobs SET state = ?, error_code = NULL, "
                "error_message = NULL, lease_owner = NULL, "
                "lease_expires_at = NULL, retry_at = NULL, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (DOWNLOAD_STATE_PENDING, job_id),
            )
            # The candidate went to FAILED, or to NEEDS_INFO when the failure
            # was a missing input, when the job did. Either way it has to be
            # eligible again or the worker would refuse to process the retry.
            connection.execute(
                "UPDATE candidates SET status = 'APPROVED', "
                "filter_reason = '', updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND status IN "
                "('FAILED', 'PROCESSING', 'NEEDS_INFO')",
                (int(row[2]),),
            )
        return DOWNLOAD_STATE_PENDING

    async def pause_job(self, job_id: int) -> str:
        """Hold a queued job so the worker skips it until it is resumed."""
        return await asyncio.to_thread(self._pause_job_sync, job_id)

    def _pause_job_sync(self, job_id: int) -> str:
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT state FROM download_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise DownloadError("JOB_NOT_FOUND", "下载任务不存在")
            if str(row[0]) != DOWNLOAD_STATE_PENDING:
                # An in-flight transfer cannot be suspended mid-stream; the
                # honest options are to let it finish or to cancel it.
                raise DownloadError(
                    "JOB_NOT_PAUSABLE",
                    "只有排队中的任务可以暂停；"
                    "正在下载的任务请使用取消",
                )
            connection.execute(
                "UPDATE download_jobs SET state = ?, lease_owner = NULL, "
                "lease_expires_at = NULL, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (DOWNLOAD_STATE_PAUSED, job_id),
            )
        return DOWNLOAD_STATE_PAUSED

    async def resume_job(self, job_id: int) -> str:
        """Return a paused job to the queue."""
        return await asyncio.to_thread(self._resume_job_sync, job_id)

    def _resume_job_sync(self, job_id: int) -> str:
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT state FROM download_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise DownloadError("JOB_NOT_FOUND", "下载任务不存在")
            if str(row[0]) != DOWNLOAD_STATE_PAUSED:
                raise DownloadError(
                    "JOB_NOT_PAUSED", "只有已暂停的任务可以继续"
                )
            connection.execute(
                "UPDATE download_jobs SET state = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (DOWNLOAD_STATE_PENDING, job_id),
            )
        return DOWNLOAD_STATE_PENDING

    async def cancel_job(self, job_id: int) -> str:
        """Cancel a job and release its candidate back to manual review."""
        await self._abandon_torrent(job_id)
        return await asyncio.to_thread(self._cancel_job_sync, job_id)

    async def set_job_priority(self, job_id: int, priority: int) -> int:
        """Move a job up or down the queue.

        Applies to both queues, since both claims order by `priority, id`. It is
        accepted on a job that is already running rather than refused: the value
        is what the *next* claim reads, so setting it on an in-flight job is
        harmless and means a retry of that job inherits the operator's intent
        instead of silently reverting to the default.
        """
        return await asyncio.to_thread(
            self._set_job_priority_sync, job_id, priority
        )

    def _set_job_priority_sync(self, job_id: int, priority: int) -> int:
        if priority < MIN_JOB_PRIORITY or priority > MAX_JOB_PRIORITY:
            raise DownloadError(
                "PRIORITY_OUT_OF_RANGE",
                f"优先级需在 {MIN_JOB_PRIORITY} 到 {MAX_JOB_PRIORITY} 之间"
                "，数值越小越靠前",
            )
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT state FROM download_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise DownloadError("JOB_NOT_FOUND", "下载任务不存在")
            if str(row[0]) in TERMINAL_DOWNLOAD_STATES or str(row[0]) in {
                CONVERSION_STATE_COMPLETED,
                CONVERSION_STATE_FAILED,
            }:
                # Reordering something that will never be claimed again would
                # look like it did something. Retry first, then reprioritise.
                raise DownloadError(
                    "JOB_NOT_QUEUED",
                    "已结束的任务没有队列位置；请先重试再调整优先级",
                )
            connection.execute(
                "UPDATE download_jobs SET priority = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (int(priority), job_id),
            )
        return int(priority)

    async def stop_seeding(self, job_id: int) -> str:
        """Stop sharing a finished torrent, keeping the archived file.

        Seeding is deliberate — it repays the swarm the payload came from — so
        it ends only when the operator says so. The job stays COMPLETED because
        the download itself succeeded; only the client entry goes away.
        """
        provider, state = await asyncio.to_thread(
            self._job_provider_state_sync, job_id
        )
        if provider != PROVIDER_EH_TORRENT or state != DOWNLOAD_STATE_COMPLETED:
            raise DownloadError(
                "JOB_NOT_SEEDING",
                "只有已完成的种子任务"
                "可以停止做种",
            )
        # Called directly rather than through `_abandon_torrent`, which only
        # touches parked jobs: here the job is finished and the client entry is
        # exactly what has to go. Files are never deleted, so the archive the
        # library already registered survives.
        if self._torrent_abandon is not None:
            await self._torrent_abandon(job_id)
        await asyncio.to_thread(self._clear_seeding_flag_sync, job_id)
        return DOWNLOAD_STATE_COMPLETED

    def _clear_seeding_flag_sync(self, job_id: int) -> None:
        """Drop the seeding marker so the job leaves the dashboard."""
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT details_json FROM download_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                return
            details = self._safe_details(row[0])
            details["seeding"] = False
            details["seeding_stopped_at"] = time.time()
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

    async def _abandon_torrent(self, job_id: int) -> None:
        """Take a parked torrent back out of the client.

        Leaving it behind would keep a torrent the operator abandoned running
        in qBittorrent forever. The removal never deletes files, so an
        already-finished payload the operator wants is still on disk.
        """
        if self._torrent_abandon is None:
            return
        provider, state = await asyncio.to_thread(
            self._job_provider_state_sync, job_id
        )
        if (
            provider == PROVIDER_EH_TORRENT
            and state == DOWNLOAD_STATE_WAITING_TORRENT
        ):
            await self._torrent_abandon(job_id)

    def _job_provider_state_sync(
        self, job_id: int
    ) -> tuple[str | None, str | None]:
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT provider, state FROM download_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            return None, None
        return str(row[0]), str(row[1])

    async def switch_source(self, job_id: int, provider: str) -> int:
        """Abandon a parked torrent and queue a different source instead.

        This is the operator's answer to a stalled torrent. The switch is
        explicit rather than automatic because dropping to preview grade or
        spending GP are both decisions a service should not make on its own.
        """
        if provider not in {PROVIDER_TELEGRAPH, PROVIDER_EXHENTAI}:
            raise DownloadError(
                "PROVIDER_UNSUPPORTED",
                f"\u4e0d\u652f\u6301\u5207\u6362\u5230 {provider}",
            )
        candidate_id = await asyncio.to_thread(
            self._job_candidate_sync, job_id
        )
        await self.cancel_job(job_id)
        # The candidate went back to PENDING_REVIEW on cancel, and only an
        # approved candidate can be queued, so the approval is restored here.
        await asyncio.to_thread(self._reapprove_candidate_sync, candidate_id)
        if provider == PROVIDER_TELEGRAPH:
            result = await self.enqueue_telegraph_download(candidate_id)
        else:
            result = await self.enqueue_exhentai_download(candidate_id)
        return result.job_id

    def _job_candidate_sync(self, job_id: int) -> int:
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT candidate_id FROM download_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise DownloadError(
                "JOB_NOT_FOUND", "\u4e0b\u8f7d\u4efb\u52a1\u4e0d\u5b58\u5728"
            )
        return int(row[0])

    def _reapprove_candidate_sync(self, candidate_id: int) -> None:
        with self._database.connection() as connection:
            connection.execute(
                "UPDATE candidates SET status = 'APPROVED', "
                "filter_reason = '', updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND status IN "
                "('PENDING_REVIEW', 'PROCESSING', 'FAILED', 'NEEDS_INFO')",
                (candidate_id,),
            )

    def _cancel_job_sync(self, job_id: int) -> str:
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT state, candidate_id FROM download_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise DownloadError("JOB_NOT_FOUND", "下载任务不存在")
            if str(row[0]) == DOWNLOAD_STATE_COMPLETED:
                raise DownloadError(
                    "JOB_ALREADY_COMPLETED", "已完成的任务无法取消"
                )
            connection.execute(
                "UPDATE download_jobs SET state = ?, lease_owner = NULL, "
                "lease_expires_at = NULL, retry_at = NULL, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (DOWNLOAD_STATE_CANCELLED, job_id),
            )
            connection.execute(
                "UPDATE candidates SET status = 'PENDING_REVIEW', "
                "filter_reason = '', updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND status IN ('APPROVED', 'PROCESSING', 'FAILED')",
                (int(row[1]),),
            )
        return DOWNLOAD_STATE_CANCELLED

    def _fetch_jobs(self, where_sql: str, params) -> tuple[DownloadJobSummary, ...]:
        with self._database.connection() as connection:
            rows = connection.execute(
                "SELECT id, candidate_id, provider, state, attempt_count, "
                "error_code, error_message, "
                "(SELECT path FROM artifacts WHERE job_id = download_jobs.id "
                " AND artifact_type = 'ARCHIVE' LIMIT 1), "
                "(SELECT size_bytes FROM artifacts WHERE job_id = download_jobs.id "
                " AND artifact_type = 'ARCHIVE' LIMIT 1), "
                "(SELECT path FROM artifacts WHERE job_id = download_jobs.id "
                " AND artifact_type = 'CBZ' LIMIT 1), "
                "created_at, updated_at, details_json, priority "
                "FROM download_jobs "
                + where_sql,
                params,
            ).fetchall()
        return tuple(
            DownloadJobSummary(
                job_id=int(row[0]),
                candidate_id=int(row[1]),
                provider=str(row[2]),
                state=str(row[3]),
                attempt_count=int(row[4]),
                error_code=str(row[5]) if row[5] is not None else None,
                error_message=str(row[6]) if row[6] is not None else None,
                artifact_path=str(row[7]) if row[7] is not None else None,
                artifact_size=int(row[8]) if row[8] is not None else None,
                artifact_cbz_path=(
                    str(row[9]) if row[9] is not None else None
                ),
                created_at=str(row[10]),
                updated_at=str(row[11]),
                details=self._safe_details(row[12]),
                priority=int(row[13]),
            )
            for row in rows
        )

    @staticmethod
    def _safe_details(raw) -> dict:
        """Details are provider-written, so a bad value must not break the page."""
        try:
            parsed = json.loads(str(raw or "{}"))
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    async def start(self) -> None:
        if self._worker_task is not None:
            return
        self._worker_task = asyncio.create_task(
            self._run_worker(), name="download-worker"
        )

    async def stop(self) -> None:
        if self._worker_task is None:
            return
        self._worker_task.cancel()
        await asyncio.gather(self._worker_task, return_exceptions=True)
        self._worker_task = None

    async def _run_worker(self) -> None:
        while True:
            try:
                processed = await self._process_one()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - defensive worker loop
                logging.getLogger(__name__).exception(
                    "download_worker_error",
                    extra={"error_code": "DOWNLOAD_WORKER_ERROR"},
                )
                processed = False
            if not processed:
                await asyncio.sleep(1.0)

    async def _process_one(self) -> bool:
        job = await asyncio.to_thread(self._claim_pending_job_sync)
        if job is None:
            return False
        # One claimed / one finished record per job, logged here because this is
        # the single point every delivery passes through -- the same reason
        # `_announce` lives here. Without the pair, 「why is this book not
        # downloaded」 had to be answered from the queue's current state alone,
        # which says nothing about how long the attempt took or how many there
        # have been.
        logger = logging.getLogger(__name__)
        job_context = {
            "job_id": job["job_id"],
            "candidate_id": job["candidate_id"],
            "provider": job["provider"],
            "attempt": job.get("attempt_count"),
        }
        logger.info("download_job_claimed", extra=job_context)
        started = time.monotonic()
        try:
            await self._handle_job(job)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            await asyncio.to_thread(
                self._mark_job_failed_sync,
                job["job_id"],
                "DOWNLOAD_WORKER_EXCEPTION",
                str(exc),
            )
            # `exception` rather than `error`: this is the branch that means a
            # provider raised something nobody mapped to an error code, so the
            # traceback is the only thing that identifies it.
            logger.exception(
                "download_job_exception",
                extra={
                    **job_context,
                    "error_code": "DOWNLOAD_WORKER_EXCEPTION",
                    "duration_ms": int((time.monotonic() - started) * 1000),
                },
            )
        else:
            # Same shape as the conversion worker: `_handle_job` returns
            # normally whether it completed, parked (WAITING_TORRENT), was
            # cancelled, or failed, and only the row tells the difference.
            # Read it back so a download failure is not reported as
            # `download_job_finished`.
            final_state, error_code, error_message = await asyncio.to_thread(
                self._read_final_state_sync, job["job_id"]
            )
            duration_ms = int((time.monotonic() - started) * 1000)
            base = {**job_context, "duration_ms": duration_ms}
            if final_state == DOWNLOAD_STATE_COMPLETED:
                logger.info("download_job_completed", extra=base)
            elif final_state == DOWNLOAD_STATE_WAITING_TORRENT:
                logger.info(
                    "download_job_waiting_torrent",
                    extra={**base, "status": final_state},
                )
            elif final_state == DOWNLOAD_STATE_CANCELLED:
                logger.info(
                    "download_job_cancelled",
                    extra={**base, "status": final_state},
                )
            else:
                logger.warning(
                    "download_job_failed",
                    extra={
                        **base,
                        "status": final_state,
                        "error_code": error_code,
                        "error_message": error_message,
                    },
                )
        # Announced here rather than at each of the dozen places that write a
        # terminal state: every delivery leaves the worker through this point,
        # so one call cannot miss a transition the way a per-branch hook would.
        self._announce(job["job_id"], job["candidate_id"])
        return True

    def _announce(self, job_id: int, candidate_id: int) -> None:
        """Tell the interface a job moved, if anything is listening.

        Never allowed to disturb the worker: notification is a convenience, and
        a subscriber problem must not fail or delay a download that already
        succeeded.
        """
        if self._notify is None:
            return
        try:
            self._notify(job_id=job_id, candidate_id=candidate_id)
        except Exception:  # noqa: BLE001 - notification is best-effort
            logging.getLogger(__name__).warning(
                "download_notify_failed",
                extra={"error_code": "DOWNLOAD_NOTIFY_FAILED"},
            )

    def _claim_pending_job_sync(self) -> dict | None:
        """Take the next queued job, lowest `priority` value first.

        `priority` before `id` rather than instead of it: within one priority
        the queue stays FIFO, so raising one job's priority reorders that job
        and nothing else. The default is 100, which leaves room to promote
        (lower) and demote (higher) without renumbering the queue.
        """
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT id, candidate_id, provider, idempotency_key, "
                "details_json, attempt_count FROM download_jobs "
                "WHERE state = ? AND provider IN ("
                + ",".join("?" for _ in SUPPORTED_PROVIDERS)
                + ") ORDER BY priority, id LIMIT 1",
                (DOWNLOAD_STATE_PENDING, *SUPPORTED_PROVIDERS),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE download_jobs SET state = ?, "
                "lease_owner = 'worker', "
                "lease_expires_at = datetime('now', '+5 minutes'), "
                "attempt_count = attempt_count + 1, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (DOWNLOAD_STATE_DOWNLOADING, int(row[0])),
            )
            connection.execute(
                "UPDATE candidates SET status = 'PROCESSING', "
                "filter_reason = '', updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ?",
                (int(row[1]),),
            )
            return {
                "job_id": int(row[0]),
                "candidate_id": int(row[1]),
                "provider": str(row[2]),
                "idempotency_key": str(row[3]),
                "details_json": str(row[4] or "{}"),
                "attempt_count": int(row[5]) + 1,
            }

    async def _handle_job(self, job: dict) -> None:
        if job["provider"] == PROVIDER_EH_TORRENT:
            await self._push_torrent_job(job)
            return
        if job["provider"] == PROVIDER_EXHENTAI:
            await self._run_delegated_provider(
                job,
                self._exhentai_download,
                missing_code="EXHENTAI_NOT_CONFIG",
                missing_message="ExHentai 下载服务未配置",
                default_error="EXHENTAI_DOWNLOAD_FAILED",
            )
            return
        if job["provider"] == PROVIDER_TELEGRAPH:
            await self._run_delegated_provider(
                job,
                self._telegraph_download,
                missing_code="TELEGRAPH_NOT_CONFIG",
                missing_message="预览页下载服务未配置",
                default_error="TELEGRAPH_PAGE_UNREACHABLE",
            )
            return
        if job["provider"] == PROVIDER_TELEGRAM_USER:
            await self._run_telegram_user_job(job)
            return
        if job["provider"] != PROVIDER_TELEGRAM:
            await asyncio.to_thread(
                self._mark_job_failed_sync,
                job["job_id"],
                "PROVIDER_UNSUPPORTED",
                f"Provider {job['provider']!r} is not yet supported",
            )
            return
        try:
            details = json.loads(job["details_json"])
            file_id = str(details["file_id"])
            file_name = str(details.get("file_name") or f"{file_id}.bin")
            api = await self._telegram_api()
            if api is None:
                await asyncio.to_thread(
                    self._mark_job_failed_sync,
                    job["job_id"],
                    "TELEGRAM_NOT_CONFIG",
                    "Telegram Bot is not connected",
                )
                return
            file_info = await api.get_file(file_id)
            work_path = await self._effective_work_path()
            destination = (
                work_path
                / "downloads"
                / f"job-{job['job_id']}-{Path(file_name).name}"
            )
            size = await api.download_file(
                file_info.file_path, destination
            )
            await asyncio.to_thread(
                self._record_artifact_sync,
                job["job_id"],
                destination,
                size,
                file_info.file_unique_id,
            )
            await asyncio.to_thread(
                self._mark_job_completed_sync, job["job_id"]
            )
            await self._maybe_auto_pack(job["candidate_id"])
        except ProviderConnectionError as exc:
            await asyncio.to_thread(
                self._mark_job_failed_sync,
                job["job_id"],
                exc.code,
                exc.public_message,
            )

    async def _run_telegram_user_job(self, job: dict) -> None:
        """Fetch an attachment with the operator's own Telegram account.

        The same shape as the bot branch -- resolve, download, record, complete --
        differing only in what does the fetching and in the ceiling it is subject
        to. It is kept as its own method rather than folded into the bot branch
        because the two share no error vocabulary: a bot failure is about a token
        and a 20 MB limit, a user failure is about a session and a message that
        may have been deleted.
        """
        try:
            details = json.loads(job["details_json"])
            chat_id = int(details["chat_id"])
            message_id = int(details["message_id"])
            file_name = str(
                details.get("file_name") or f"message-{message_id}.bin"
            )
        except (KeyError, TypeError, ValueError):
            await asyncio.to_thread(
                self._mark_job_failed_sync,
                job["job_id"],
                "ATTACHMENT_INVALID",
                "任务缺少源消息位置，无法用用户账户下载",
            )
            return
        client = None
        if self._telegram_user_client is not None:
            client = await self._telegram_user_client()
        if client is None:
            await asyncio.to_thread(
                self._mark_job_failed_sync,
                job["job_id"],
                "TELEGRAM_USER_NOT_CONFIG",
                "尚未登录 Telegram 用户账户，无法下载大文件",
            )
            return
        work_path = await self._effective_work_path()
        destination = (
            work_path / "downloads" / f"job-{job['job_id']}-{Path(file_name).name}"
        )
        try:
            size = await client.download_message_media(
                chat_id, message_id, destination
            )
        except ProviderConnectionError as exc:
            await asyncio.to_thread(
                self._mark_job_failed_sync,
                job["job_id"],
                exc.code,
                exc.public_message,
            )
            return
        await asyncio.to_thread(
            self._record_artifact_sync,
            job["job_id"],
            destination,
            size,
            str(details.get("file_unique_id") or f"{chat_id}:{message_id}"),
        )
        await asyncio.to_thread(self._mark_job_completed_sync, job["job_id"])
        await self._maybe_auto_pack(job["candidate_id"])

    async def _push_torrent_job(self, job: dict) -> None:
        """Hand the torrent to the client and park the job on peers.

        This provider is the one that cannot finish inside a worker turn: the
        transfer belongs to qBittorrent. Parking in `WAITING_TORRENT` releases
        the concurrency slot so a long, seederless torrent does not block the
        queue, and `TorrentService`'s poller advances it from there.
        """
        if self._torrent_push is None:
            await asyncio.to_thread(
                self._mark_job_failed_sync,
                job["job_id"],
                "TORRENT_CLIENT_NOT_CONFIG",
                "\u79cd\u5b50\u4e0b\u8f7d\u670d\u52a1\u672a\u914d\u7f6e",
            )
            return
        try:
            details = await self._torrent_push(job["candidate_id"])
        except Exception as exc:  # noqa: BLE001 - provider boundary
            await asyncio.to_thread(
                self._mark_job_failed_sync,
                job["job_id"],
                str(getattr(exc, "code", "TORRENT_PUSH_REJECTED")),
                str(getattr(exc, "public_message", exc)),
            )
            return
        await asyncio.to_thread(
            self._park_job_sync, job["job_id"], details or {}
        )

    def _park_job_sync(self, job_id: int, details: dict) -> None:
        """Move a pushed job to WAITING_TORRENT, merging the push details.

        The existing details are merged rather than replaced so a retry keeps
        whatever the previous attempt learned, and the infohash is what the
        poller re-attaches by after a restart.
        """
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT details_json FROM download_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            merged: dict = {}
            if row is not None:
                try:
                    existing = json.loads(str(row[0] or "{}"))
                except ValueError:
                    existing = {}
                if isinstance(existing, dict):
                    merged.update(existing)
            merged.update(details)
            connection.execute(
                "UPDATE download_jobs SET state = ?, details_json = ?, "
                "error_code = NULL, error_message = NULL, "
                "lease_owner = NULL, lease_expires_at = NULL, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (
                    DOWNLOAD_STATE_WAITING_TORRENT,
                    json.dumps(
                        merged, separators=(",", ":"), ensure_ascii=False
                    ),
                    job_id,
                ),
            )

    async def _run_delegated_provider(
        self,
        job: dict,
        handler,
        *,
        missing_code: str,
        missing_message: str,
        default_error: str,
    ) -> None:
        """Run a provider that owns its own transfer and artifact registration.

        ExHentai and Telegraph both register the finished archive themselves,
        so the worker only has to record the outcome. The error code is lifted
        off the exception because every provider error carries `code` and
        `public_message`; anything else falls back to the provider default.
        """
        if handler is None:
            await asyncio.to_thread(
                self._mark_job_failed_sync,
                job["job_id"],
                missing_code,
                missing_message,
            )
            return
        try:
            await handler(job["candidate_id"])
        except Exception as exc:  # noqa: BLE001 - provider boundary
            await asyncio.to_thread(
                self._mark_job_failed_sync,
                job["job_id"],
                str(getattr(exc, "code", default_error)),
                str(getattr(exc, "public_message", exc)),
            )
            return
        await asyncio.to_thread(
            self._mark_job_completed_sync, job["job_id"]
        )
        await self._maybe_auto_pack(job["candidate_id"])

    async def _maybe_auto_pack(self, candidate_id: int) -> None:
        """Hand a finished download to the conversion queue, if one is wired.

        A failure here is logged rather than raised, matching the torrent
        route's contract: the download itself succeeded and its artifact is
        registered, so a packing hiccup must not turn into a failed download or
        hide the archive the operator can still convert by hand.
        """
        if self._auto_pack is None:
            return
        _enabled = self._auto_pack_enabled
        if _enabled is not None and not await _enabled():
            return
        try:
            await self._auto_pack(candidate_id)
        except Exception as exc:  # noqa: BLE001 - queue boundary
            logging.getLogger(__name__).warning(
                "download_auto_pack_failed candidate=%d error=%s",
                candidate_id,
                exc,
                extra={"error_code": "DOWNLOAD_AUTO_PACK_FAILED"},
            )

    async def _telegram_api(self) -> TelegramBotApi | None:
        if self._telegram_client_factory is None:
            return None
        token, client = await self._telegram_client_factory()
        if not token or client is None:
            return None
        return TelegramBotApi(token, client)

    def _mark_job_completed_sync(self, job_id: int) -> None:
        with self._database.connection() as connection:
            connection.execute(
                "UPDATE download_jobs SET state = ?, error_code = NULL, "
                "error_message = NULL, lease_owner = NULL, "
                "lease_expires_at = NULL, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (DOWNLOAD_STATE_COMPLETED, job_id),
            )
            connection.execute(
                "UPDATE candidates SET status = 'DOWNLOADED', "
                "filter_reason = '', updated_at = CURRENT_TIMESTAMP "
                "WHERE id = (SELECT candidate_id FROM download_jobs "
                "WHERE id = ?)",
                (job_id,),
            )

    def _record_artifact_sync(
        self,
        job_id: int,
        destination: Path,
        size: int,
        file_unique_id: str,
    ) -> None:
        sha256 = hashlib.sha256()
        with destination.open("rb") as handle:
            while True:
                chunk = handle.read(64 * 1024)
                if not chunk:
                    break
                sha256.update(chunk)
        with self._database.connection() as connection:
            connection.execute(
                "INSERT INTO artifacts "
                "(job_id, artifact_type, path, sha256, size_bytes) "
                "VALUES (?, 'ARCHIVE', ?, ?, ?) "
                "ON CONFLICT(job_id, artifact_type) DO UPDATE SET "
                "path = excluded.path, sha256 = excluded.sha256, "
                "size_bytes = excluded.size_bytes",
                (
                    job_id,
                    str(destination),
                    sha256.hexdigest(),
                    int(size),
                ),
            )

    def _mark_job_failed_sync(
        self, job_id: int, code: str, message: str
    ) -> None:
        """Record a failure, routing "needs a human" apart from "broken".

        A short preview page is not a defect the service can retry its way out
        of: someone has to supply the second page link. Those candidates go to
        NEEDS_INFO, which is a reviewable state, while everything else goes to
        FAILED as before. The job row itself is FAILED either way so the retry
        button still applies once the input has been supplied.
        """
        candidate_status = (
            "NEEDS_INFO" if code in NEEDS_INFO_DOWNLOAD_ERRORS else "FAILED"
        )
        with self._database.connection() as connection:
            connection.execute(
                "UPDATE download_jobs SET state = ?, error_code = ?, "
                "error_message = ?, lease_owner = NULL, "
                "lease_expires_at = NULL, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (DOWNLOAD_STATE_FAILED, code, message, job_id),
            )
            connection.execute(
                "UPDATE candidates SET status = ?, "
                "filter_reason = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = (SELECT candidate_id FROM download_jobs "
                "WHERE id = ?)",
                (candidate_status, message, job_id),
            )

    def _read_final_state_sync(
        self, job_id: int
    ) -> tuple[str, str | None, str | None]:
        """Read the row back so the worker loop can log the real outcome.

        `_handle_job` returns normally whether the job downloaded, was
        parked in WAITING_TORRENT, was cancelled, or failed; only the row
        tells the difference. Reading it here keeps the terminal log line
        in lock-step with what the page is about to render.
        """
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT state, error_code, error_message "
                "FROM download_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            return DOWNLOAD_STATE_FAILED, None, "job row vanished"
        return str(row[0]), row[1], row[2]


__all__ = [
    "DownloadError",
    "DownloadService",
    "DownloadState",
]
