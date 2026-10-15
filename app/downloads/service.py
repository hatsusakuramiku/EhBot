from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
from contextlib import nullcontext
from pathlib import Path
import time
from uuid import uuid4

from app.connections.models import ProviderConnectionError, refusal_detail
from app.connections.telegram import TelegramBotApi
from app.db.database import Database, title_subquery
from app.downloads.models import (
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


from app.downloads.queue import DownloadError, QueueControl, STALE_EXECUTION_ERRORS, task_snapshot
from app.downloads.execution import attempt_path, execution_scope, failure_transaction, run_sync


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
        torrent_stop=None,
        stop_pack=None,
        cleanup_removals=None,
        account_scope=None,
        auto_pack=None,
        auto_pack_enabled=None,
        work_path_provider=None,
        notify=None,
        exhentai_preflight=None,
        telegraph_preflight=None,
    ) -> None:
        self._database = database
        self._queue_control = QueueControl(database, switch_preflight=self._preflight_switch)
        self._work_path = work_path
        self._telegram_client_factory = telegram_client_factory
        # Resolved per delivery, like the bot token: the operator can log the
        # user account in, or Telegram can revoke the session, between one job
        # and the next. Returns None when no account is configured, which is the
        # normal state for a deployment that only ever fetches small files.
        self._telegram_user_client = telegram_user_client
        self._exhentai_download = exhentai_download
        self._telegraph_download = telegraph_download
        self._exhentai_preflight = exhentai_preflight
        self._telegraph_preflight = telegraph_preflight
        # Pushing hands the transfer to qBittorrent; abandoning takes it back
        # out when the operator cancels or switches sources. Verifying asks the
        # torrent service whether a finished payload is already readable on
        # disk so a retry can complete without a fresh push. Auto-pack hands a
        # finished download to the conversion queue.
        self._torrent_push = torrent_push
        self._torrent_abandon = torrent_abandon
        self._torrent_verify = torrent_verify
        self._torrent_stop = torrent_stop
        self._stop_pack = stop_pack
        self._cleanup_removals = cleanup_removals
        self._account_scope = account_scope
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
        self._active_attempts: dict[str, asyncio.Task] = {}
        self._operation_task: asyncio.Task | None = None
        self._operation_lock = asyncio.Lock()

    async def _effective_work_path(self) -> Path:
        if self._work_path_provider is None:
            return self._work_path
        resolved = await self._work_path_provider()
        return resolved or self._work_path

    async def enqueue_telegram_download(
        self,
        candidate_id: int,
        attachment: dict,
        *,
        redownload: bool = False,
        automatic_match=None,
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
            **self._source_binding(attachment),
        }
        if self._account_scope is not None:
            await run_sync(self._validate_telegram_source_sync, candidate_id, details, "TG_BOT")
        return await self._enqueue(
            candidate_id,
            PROVIDER_TELEGRAM,
            idempotency_key,
            json.dumps(details, separators=(",", ":")),
            redownload=redownload,
            automatic_match=automatic_match,
        )

    async def enqueue_telegram_user_download(
        self,
        candidate_id: int,
        attachment: dict,
        *,
        redownload: bool = False,
        automatic_match=None,
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
            **self._source_binding(attachment),
        }
        if self._account_scope is not None:
            await run_sync(self._validate_telegram_source_sync, candidate_id, details, None)
        return await self._enqueue(
            candidate_id,
            PROVIDER_TELEGRAM_USER,
            f"telegram-user:{candidate_id}:{file_unique_id or message_id}",
            json.dumps(details, separators=(",", ":")),
            redownload=redownload,
            automatic_match=automatic_match,
        )

    @staticmethod
    def _source_binding(attachment: dict) -> dict:
        """Freeze source identity separately from a verified download account."""
        return {"observation_id": attachment.get("observation_id"),
                "observation_revision": attachment.get("observation_revision"),
                "origin_account_id": attachment.get("account_id"),
                "origin_account_kind": attachment.get("account_kind"), "peer_type": attachment.get("peer_type"),
                "account_id": attachment.get("account_id")}

    def _validate_telegram_source_sync(self, candidate_id: int, details: dict, kind: str | None, connection=None) -> None:
        """Reject stale or foreign observations before contacting a provider."""
        with nullcontext(connection) if connection is not None else self._database.connection() as connection:
            row = connection.execute(
                "SELECT sm.observation_revision, ta.external_account_id, ea.kind, sm.attachment_json, "
                "sm.chat_id, sm.message_id FROM source_messages sm "
                "JOIN candidate_messages cm ON cm.source_message_id = sm.id "
                "JOIN telegram_accounts ta ON ta.id = sm.account_id "
                "LEFT JOIN external_accounts ea ON ea.account_id = ta.external_account_id "
                "WHERE sm.id = ? AND cm.candidate_id = ? AND sm.message_state = 'ACTIVE'",
                (details.get("observation_id"), candidate_id),
            ).fetchone()
            if row is None or row[0] != details.get("observation_revision"):
                raise DownloadError("TELEGRAM_SOURCE_CHANGED", "所选来源已编辑、移除或不属于该作品")
            if not row[1] or row[1] != details.get("origin_account_id"):
                raise DownloadError("TELEGRAM_SOURCE_ACCOUNT_REQUIRED", "来源账号归属未知或不匹配，请选择已确认的消息来源")
            if kind is not None and (row[2] != kind or
                    (details.get("download_observation_id") is None and details.get("account_id") != row[1])):
                raise DownloadError("ACCOUNT_KIND_INVALID", "所选来源账号不支持该下载方式")
            attachments = json.loads(row[3])
            if not any(item.get("type") == "archive" and item.get("file_unique_id") == details.get("file_unique_id")
                       and (kind != "TG_BOT" or item.get("file_id") == details.get("file_id")) for item in attachments):
                raise DownloadError("ATTACHMENT_INVALID", "所选附件与保存的来源不匹配")
            if kind != "TG_BOT" and (row[4], row[5]) != (details.get("chat_id"), details.get("message_id")):
                raise DownloadError("ATTACHMENT_INVALID", "所选附件的源消息位置不匹配")

    async def enqueue_exhentai_download(
        self, candidate_id: int, *, redownload: bool = False, automatic_match=None
    ) -> DownloadEnqueueResult:
        return await self._enqueue(
            candidate_id,
            PROVIDER_EXHENTAI,
            f"exhentai:{candidate_id}",
            "{}",
            redownload=redownload,
            automatic_match=automatic_match,
        )

    async def enqueue_telegraph_download(
        self, candidate_id: int, *, redownload: bool = False, automatic_match=None
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
            redownload=redownload,
            automatic_match=automatic_match,
        )

    async def enqueue_torrent_download(
        self, candidate_id: int, *, redownload: bool = False, automatic_match=None
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
            redownload=redownload,
            automatic_match=automatic_match,
        )

    async def _enqueue(
        self,
        candidate_id: int,
        provider: str,
        idempotency_key: str,
        details_json: str,
        *,
        redownload: bool = False,
        automatic_match=None,
    ) -> DownloadEnqueueResult:
        return await run_sync(
            self._enqueue_sync,
            candidate_id,
            provider,
            idempotency_key,
            details_json,
            redownload,
            automatic_match,
        )

    def _enqueue_sync(
        self,
        candidate_id: int,
        provider: str,
        idempotency_key: str,
        details_json: str,
        redownload: bool = False,
        automatic_match=None,
    ) -> DownloadEnqueueResult:
        with self._database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._account_scope is not None and provider in {PROVIDER_TELEGRAM, PROVIDER_TELEGRAM_USER}:
                self._validate_telegram_source_sync(candidate_id, json.loads(details_json),
                    "TG_BOT" if provider == PROVIDER_TELEGRAM else None, connection)
            if connection.execute("SELECT 1 FROM candidates WHERE id = ?", (candidate_id,)).fetchone() is None:
                raise DownloadError("CANDIDATE_NOT_FOUND", "Candidate does not exist")
            if automatic_match is not None:
                from app.auto_approval.commit import begin_decision
                begin_decision(self._database, connection, candidate_id, automatic_match)
            if connection.execute("SELECT 1 FROM candidate_approvals WHERE candidate_id = ?", (candidate_id,)).fetchone() is None:
                raise DownloadError("CANDIDATE_NOT_DOWNLOADABLE", "该候选没有有效审批事实，不能进入下载队列")
            slot = connection.execute(
                "SELECT legacy_job_id, provider FROM queue_tasks WHERE candidate_id = ? AND kind = 'DOWNLOAD' "
                "AND status NOT IN ('COMPLETED', 'FAILED', 'CANCELLED', 'REMOVED')", (candidate_id,),
            ).fetchone()
            existing = connection.execute(
                "SELECT id, state FROM download_jobs WHERE idempotency_key = ?", (idempotency_key,),
            ).fetchone()
            if slot is not None:
                if existing is None or int(slot[0]) != int(existing[0]):
                    raise DownloadError("WORK_SLOT_OCCUPIED", "另一下载来源已占用该作品槽，请先停止原任务")
                if slot[1] != provider:
                    raise DownloadError("WORK_SLOT_OCCUPIED", "另一下载来源已占用该作品槽，请先停止原任务")
            revive = existing is not None and (
                existing[1] in {DOWNLOAD_STATE_FAILED, DOWNLOAD_STATE_CANCELLED}
                or (redownload and existing[1] == DOWNLOAD_STATE_COMPLETED)
            )
            if revive and provider == PROVIDER_EH_TORRENT:
                saved = connection.execute("SELECT details_json FROM download_jobs WHERE id = ?", (existing[0],)).fetchone()[0]
                if saved not in (None, "", "{}"):
                    raise DownloadError("REMOTE_RECONCILIATION_REQUIRED", "请通过重试操作核实旧远端任务结束后再入队")
            if existing is None:
                job_id = connection.execute(
                    "INSERT INTO download_jobs(candidate_id, idempotency_key, provider, state, details_json) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (candidate_id, idempotency_key, provider, DOWNLOAD_STATE_PENDING, details_json),
                ).lastrowid
            else:
                job_id = int(existing[0])
                if revive:
                    connection.execute(
                        "UPDATE download_jobs SET state = ?, details_json = ?, error_code = NULL, "
                        "error_message = NULL, lease_owner = NULL, lease_expires_at = NULL, retry_at = NULL, "
                        "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (DOWNLOAD_STATE_PENDING, details_json, job_id),
                    )
            snapshot = task_snapshot(connection, job_id)
            if automatic_match is not None:
                from app.auto_approval.commit import record_decision
                record_decision(connection, candidate_id, automatic_match, [job_id])
            return DownloadEnqueueResult(job_id=job_id, created=existing is None, state=snapshot["state"],
                                         task_id=snapshot["task_id"], revision=snapshot["revision"])

    async def list_jobs_for_candidate(
        self, candidate_id: int
    ) -> tuple[DownloadJobSummary, ...]:
        return await run_sync(
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
        return await run_sync(self._list_active_pack_jobs_sync)

    def _list_active_pack_jobs_sync(self) -> tuple[DownloadJobSummary, ...]:
        return self._fetch_jobs(
            "WHERE provider = ? AND state IN (?, ?, ?, ?, ?) "
            "ORDER BY id DESC LIMIT 50",
            (
                PROVIDER_CONVERSION,
                "CONVERSION_PENDING",
                "CONVERSION_RUNNING",
                "CONVERSION_WAITING_VOLUMES",
                "CONVERSION_WAITING_PASSWORD",
                "STOPPING",
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
        return await run_sync(
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
        return await run_sync(self._list_active_jobs_sync)

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

    async def command(self, job_id: int, action: str, *, task_id: str,
                      expected_revision: int, idempotency_key: str, **params) -> dict:
        """Apply a client command with its observed version and durable receipt."""
        return await self._queue_control.command(job_id, action, task_id=task_id,
            expected_revision=expected_revision, idempotency_key=idempotency_key, **params)

    async def get_task(self, task_id: str) -> dict:
        return await self._queue_control.get_task(task_id)

    async def retry_job(self, job_id: int) -> str:
        return await run_sync(self._retry_job_sync, job_id)

    def _retry_job_sync(self, job_id: int) -> str:
        return self._queue_control.internal_command(job_id, "retry")["state"]

    async def pause_job(self, job_id: int) -> str:
        return await run_sync(self._pause_job_sync, job_id)

    def _pause_job_sync(self, job_id: int) -> str:
        return self._queue_control.internal_command(job_id, "pause")["state"]

    async def resume_job(self, job_id: int) -> str:
        return await run_sync(self._resume_job_sync, job_id)

    def _resume_job_sync(self, job_id: int) -> str:
        return self._queue_control.internal_command(job_id, "resume")["state"]

    async def cancel_job(self, job_id: int) -> str:
        return await run_sync(self._cancel_job_sync, job_id)

    def _cancel_job_sync(self, job_id: int) -> str:
        return self._queue_control.internal_command(job_id, "cancel")["state"]

    async def set_job_priority(self, job_id: int, priority: int) -> int:
        return await run_sync(self._set_job_priority_sync, job_id, priority)

    def _set_job_priority_sync(self, job_id: int, priority: int) -> int:
        return self._queue_control.internal_command(job_id, "priority", priority=priority)["priority"]

    async def stop_seeding(self, job_id: int) -> str:
        result = await run_sync(self._queue_control.internal_command, job_id, "stop-seeding")
        return result["state"]

    async def switch_source(self, job_id: int, provider: str) -> int:
        with self._database.connection() as connection:
            current = task_snapshot(connection, job_id)
        result = await self.command(
            job_id, "switch-source", provider=provider, task_id=current["task_id"],
            expected_revision=current["revision"], idempotency_key=uuid4().hex,
        )
        return result["job_id"]

    async def _preflight_switch(self, candidate_id: int, provider: str) -> None:
        validator = self._exhentai_preflight if provider == PROVIDER_EXHENTAI else self._telegraph_preflight
        if validator is None:
            raise DownloadError(f"{provider}_NOT_CONFIG", "目标来源的预检服务未配置")
        try:
            await validator(candidate_id)
        except Exception as exc:
            if hasattr(exc, "code") and hasattr(exc, "public_message"):
                raise DownloadError(exc.code, exc.public_message) from exc
            raise

    def _job_provider_state_sync(self, job_id: int) -> tuple[str | None, str | None]:
        with self._database.connection() as connection:
            row = connection.execute("SELECT provider, state FROM download_jobs WHERE id = ?", (job_id,)).fetchone()
        return (str(row[0]), str(row[1])) if row else (None, None)

    def _job_candidate_sync(self, job_id: int) -> int:
        with self._database.connection() as connection:
            row = connection.execute("SELECT candidate_id FROM download_jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise DownloadError("JOB_NOT_FOUND", "Task does not exist")
        return int(row[0])

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
                "created_at, updated_at, details_json, priority, "
                + title_subquery("download_jobs.candidate_id")
                + ", "
                "(SELECT ex_gid FROM candidates "
                " WHERE id = download_jobs.candidate_id), task_id, "
                "(SELECT revision FROM queue_tasks WHERE task_id = download_jobs.task_id), "
                "(SELECT generation FROM queue_tasks WHERE task_id = download_jobs.task_id), "
                "(SELECT current_attempt_id FROM queue_tasks WHERE task_id = download_jobs.task_id), "
                "(SELECT action FROM queue_operations WHERE task_id = download_jobs.task_id "
                "AND status NOT IN ('COMPLETED', 'FAILED') LIMIT 1) "
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
                title=str(row[14]) if row[14] is not None else None,
                ex_gid=int(row[15]) if row[15] is not None else None,
                task_id=str(row[16]), revision=int(row[17]), generation=int(row[18]),
                attempt_id=str(row[19]) if row[19] is not None else None,
                pending_operation=str(row[20]) if row[20] is not None else None,
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
        # Before the worker takes anything new, give back what a previous
        # process was holding when it died. See `reclaim_expired_leases`.
        await self.reclaim_expired_leases()
        self._worker_task = asyncio.create_task(
            self._run_worker(), name="download-worker"
        )
        self._operation_task = asyncio.create_task(self._run_operations(), name="queue-operations")

    async def reclaim_expired_leases(self) -> int:
        """Return jobs whose lease outlived the process that held it.

        The lease columns were written and never read. `_claim_pending_job_sync`
        stamps `lease_expires_at` five minutes out, but nothing ever compared it
        to the clock, so a container killed mid-transfer left the row in
        `DOWNLOADING` and the candidate in `PROCESSING` -- and every route back
        was closed: the claim query only looks at `PENDING`, `retry_job` refuses
        anything that is not FAILED / PAUSED / CANCELLED, `redownload_work`
        refuses an open state, and `_enqueue` only revives FAILED / CANCELLED.
        The book had no button anywhere that could move it, which is the same
        dead end a failed download used to be.

        Called from `start`, which is once per process and before the worker
        claims anything: two workers cannot race here because there is one
        worker per process and its own lease is not expired yet. The expiry is
        what makes this safe rather than a blanket reset -- a live lease is left
        alone, so this can never take a job away from a running transfer.

        The candidate goes back to `APPROVED` rather than to `PENDING_REVIEW`:
        it *was* approved, nobody withdrew that, and the only thing that failed
        was the process. `attempt_count` is deliberately not decremented -- the
        attempt happened, and a book that has been interrupted four times should
        read as such.
        """
        reclaimed = await run_sync(self._reclaim_expired_leases_sync)
        if reclaimed:
            # Warning, not info: this only happens after an unclean stop, and
            # it is the one line that explains why a job's attempt count grew
            # without a failure being recorded against it.
            logging.getLogger(__name__).warning(
                "download_leases_reclaimed jobs=%d",
                reclaimed,
                extra={"error_code": "DOWNLOAD_LEASE_RECLAIMED"},
            )
        return reclaimed

    def _reclaim_expired_leases_sync(self) -> int:
        with self._database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT id, candidate_id, provider, details_json FROM download_jobs "
                "WHERE state = ? AND lease_expires_at IS NOT NULL "
                "AND lease_expires_at < datetime('now')",
                (DOWNLOAD_STATE_DOWNLOADING,),
            ).fetchall()
            for job_id, candidate_id, provider, details_json in rows:
                current = task_snapshot(connection, job_id)
                if provider == PROVIDER_EH_TORRENT and (
                    self._safe_details(details_json).get("hash") or connection.execute(
                        "SELECT 1 FROM queue_remote_resources WHERE task_id = ? AND status <> 'STOP_CONFIRMED'",
                        (current["task_id"],),
                    ).fetchone() is not None
                ):
                    # A process may die after remote acceptance but before park.
                    # Never push a second transfer while its saved intent is unresolved.
                    self._queue_control._operation(connection, job_id, "restart", current, {})
                    connection.execute(
                        "UPDATE download_jobs SET state = 'STOPPING', lease_owner = NULL, lease_expires_at = NULL, "
                        "updated_at = CURRENT_TIMESTAMP WHERE id = ?", (job_id,),
                    )
                    continue
                connection.execute(
                    "UPDATE download_jobs SET state = ?, lease_owner = NULL, "
                    "lease_expires_at = NULL, retry_at = NULL, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (DOWNLOAD_STATE_PENDING, int(job_id)),
                )
                # Guarded by status so a candidate an operator has since moved
                # on -- rejected it, or approved another source -- is not
                # dragged back by a job row nobody is waiting for.
                connection.execute(
                    "UPDATE candidates SET status = 'APPROVED', "
                    "filter_reason = '', updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = ? AND status = 'PROCESSING'",
                    (int(candidate_id),),
                )
        return len(rows)

    async def stop(self) -> None:
        tasks = [task for task in (self._worker_task, self._operation_task) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._worker_task = None
        self._operation_task = None

    async def stop_attempt(self, task_id: str) -> None:
        """Cancel only the named execution and wait for its actual cleanup."""
        task = self._active_attempts.get(task_id)
        if task is None:
            return
        if not task.done() and not task.cancelling():
            task.cancel()
        await asyncio.shield(asyncio.gather(task, return_exceptions=True))

    async def process_operations(self) -> int:
        """Execute durable stop intentions; unconfirmed resources keep their slot."""
        completed = 0
        async with self._operation_lock:
            for operation in await run_sync(self._queue_control.pending_operations):
                try:
                    if operation["provider"] == PROVIDER_CONVERSION:
                        if self._stop_pack is None:
                            raise DownloadError("PACK_STOP_UNAVAILABLE", "打包停止服务尚未接通")
                        await self._stop_pack(operation["task_id"])
                    else:
                        await self.stop_attempt(operation["task_id"])
                    if operation["provider"] == PROVIDER_EH_TORRENT:
                        if self._torrent_stop is None:
                            raise DownloadError("REMOTE_STOP_UNAVAILABLE", "远端停止服务尚未接通")
                        await self._torrent_stop(operation)
                    await run_sync(self._queue_control.finish_operation, operation)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await run_sync(self._queue_control.operation_error, operation["operation_id"],
                                   str(getattr(exc, "code", "STOP_NOT_CONFIRMED")))
                    continue
                completed += 1
                self._announce(operation["legacy_job_id"], operation["candidate_id"])
            if self._cleanup_removals is not None:
                await self._cleanup_removals()
            from app.candidates.references import process_reference_changes
            for result in await run_sync(process_reference_changes, self._database):
                self._announce(None, result['result_candidate_id'] or result['candidate_id'])
                if result['result_candidate_id'] and result['result_candidate_id'] != result['candidate_id']:
                    self._announce(None, result['candidate_id'])
            from app.candidates.changes import process_observation_changes
            for candidate_id in await run_sync(process_observation_changes, self._database):
                self._announce(None, candidate_id)
        return completed



    async def _run_operations(self) -> None:
        while True:
            try:
                await self.process_operations()
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger(__name__).exception("queue_operation_error")
            await asyncio.sleep(1.0)

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
        job = await run_sync(self._claim_pending_job_sync)
        if job is None:
            return False
        task = asyncio.create_task(self._execute_attempt(job), name=f"download-{job['attempt_id']}")
        self._active_attempts[job["task_id"]] = task
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                if not task.done() and not task.cancelling():
                    task.cancel()
                # Shutdown can race an operator/account stop. Do not inject a
                # second cancellation into producer cleanup or drop its handle
                # while a later shutdown cancellation interrupts this waiter.
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not task.cancelled():
                    task.exception()
                raise
            # A stop operation cancelled the child, not the worker itself.
            return True
        finally:
            self._active_attempts.pop(job["task_id"], None)

    async def _execute_attempt(self, job: dict) -> bool:
        with execution_scope(self._database.path, job) as execution:
            try:
                await run_sync(self._check_execution_sync, execution)
                return await self._process_claimed_job(job)
            except sqlite3.IntegrityError as exc:
                if str(exc) not in STALE_EXECUTION_ERRORS:
                    raise
                return True

    def _check_execution_sync(self, execution) -> None:
        with self._database.connection() as connection:
            execution.check(connection)

    async def _process_claimed_job(self, job: dict) -> bool:
        self._announce(job["job_id"], job["candidate_id"])
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
            # `_handle_job` returns the untranslated cause behind a translated
            # refusal, or None. The terminal line below is the one place a
            # finished job is reported, so the cause has to travel back here
            # rather than be logged from the provider branch.
            detail = await self._handle_job(job)
        except asyncio.CancelledError:
            raise
        except sqlite3.IntegrityError as exc:
            if str(exc) not in STALE_EXECUTION_ERRORS:
                raise
            # A revoked legacy execution may still return before P3 physically
            # stops it. Reject its commit while keeping the scheduler alive.
            logger.info("download_attempt_commit_rejected", extra=job_context)
            return True
        except Exception as exc:  # noqa: BLE001
            await run_sync(
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
            final_state, error_code, error_message = await run_sync(
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
                        "error_detail": detail,
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
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id, candidate_id, provider, idempotency_key, "
                "details_json, attempt_count FROM download_jobs "
                "WHERE state = ? AND provider IN ("
                + ",".join("?" for _ in SUPPORTED_PROVIDERS)
                + ") AND candidate_id NOT IN (SELECT candidate_id FROM queue_blocked_candidates) "
                "ORDER BY priority, id LIMIT 1",
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
                **task_snapshot(connection, int(row[0])),
                "job_id": int(row[0]),
                "candidate_id": int(row[1]),
                "provider": str(row[2]),
                "idempotency_key": str(row[3]),
                "details_json": str(row[4] or "{}"),
                "attempt_count": int(row[5]) + 1,
            }

    async def _handle_job(self, job: dict) -> str | None:
        """Hold the selected account version through requests and final writes."""
        if self._account_scope is None:
            return await self._handle_job_unscoped(job)
        kinds = {PROVIDER_TELEGRAM: "TG_BOT", PROVIDER_TELEGRAM_USER: "TG_USER",
                 PROVIDER_EXHENTAI: "EH", PROVIDER_EH_TORRENT: "EH"}
        kind = kinds.get(job["provider"])
        if kind is None:
            return await self._handle_job_unscoped(job)
        try:
            details = self._safe_details(job["details_json"])
            scope_params = {"required": job["provider"] != PROVIDER_EH_TORRENT}
            if job["provider"] in {PROVIDER_TELEGRAM, PROVIDER_TELEGRAM_USER}:
                await run_sync(self._validate_telegram_source_sync, job["candidate_id"], details,
                               "TG_BOT" if job["provider"] == PROVIDER_TELEGRAM else None)
                if not details.get("account_id"):
                    raise DownloadError("TELEGRAM_SOURCE_ACCOUNT_REQUIRED", "任务没有确定的下载账号，请重新选择来源")
                if job["provider"] != PROVIDER_TELEGRAM_USER or details.get("origin_account_kind") == "TG_USER":
                    scope_params["account_id"] = details["account_id"]
            # Magnets may not need an EH account; .torrent fetch still rejects
            # missing credentials through its existing named provider error.
            async with self._account_scope(kind, **scope_params):
                return await self._handle_job_unscoped(job)
        except (ProviderConnectionError, DownloadError) as exc:
            await run_sync(self._mark_job_failed_sync, job["job_id"], exc.code, exc.public_message)
            return None

    async def _handle_job_unscoped(self, job: dict) -> str | None:
        """Run one delivery, returning the untranslated cause of a refusal.

        The interface must never see a provider's own words, but the log has to:
        a translated refusal whose code is a catch-all -- `TELEGRAM_USER_FAILED`
        is the one an operator has hit -- says nothing about what actually went
        wrong. The `raise ... from exc` chain still holds it, so it is handed
        back to the single terminal log line. Providers whose exception is
        already their message return nothing.
        """
        if job["provider"] == PROVIDER_EH_TORRENT:
            await self._push_torrent_job(job)
            return None
        if job["provider"] == PROVIDER_EXHENTAI:
            await self._run_delegated_provider(
                job,
                self._exhentai_download,
                missing_code="EXHENTAI_NOT_CONFIG",
                missing_message="ExHentai 下载服务未配置",
                default_error="EXHENTAI_DOWNLOAD_FAILED",
            )
            return None
        if job["provider"] == PROVIDER_TELEGRAPH:
            await self._run_delegated_provider(
                job,
                self._telegraph_download,
                missing_code="TELEGRAPH_NOT_CONFIG",
                missing_message="预览页下载服务未配置",
                default_error="TELEGRAPH_PAGE_UNREACHABLE",
            )
            return None
        if job["provider"] == PROVIDER_TELEGRAM_USER:
            return await self._run_telegram_user_job(job)
        if job["provider"] != PROVIDER_TELEGRAM:
            await run_sync(
                self._mark_job_failed_sync,
                job["job_id"],
                "PROVIDER_UNSUPPORTED",
                f"Provider {job['provider']!r} is not yet supported",
            )
            return None
        try:
            details = json.loads(job["details_json"])
            file_id = str(details["file_id"])
            file_name = str(details.get("file_name") or f"{file_id}.bin")
            api = await self._telegram_api()
            if api is None:
                await run_sync(
                    self._mark_job_failed_sync,
                    job["job_id"],
                    "TELEGRAM_NOT_CONFIG",
                    "Telegram Bot is not connected",
                )
                return None
            file_info = await api.get_file(file_id)
            work_path = await self._effective_work_path()
            destination = (
                work_path
                / "downloads"
                / f"job-{job['job_id']}-{Path(file_name).name}"
            )
            destination = attempt_path(destination)
            size = await api.download_file(
                file_info.file_path, destination
            )
            await run_sync(
                self._record_artifact_sync,
                job["job_id"],
                destination,
                size,
                file_info.file_unique_id,
            )
            await run_sync(
                self._mark_job_completed_sync, job["job_id"]
            )
            await self._maybe_auto_pack(job["candidate_id"])
        except ProviderConnectionError as exc:
            await run_sync(
                self._mark_job_failed_sync,
                job["job_id"],
                exc.code,
                exc.public_message,
            )
            return refusal_detail(exc)
        return None

    async def _run_telegram_user_job(self, job: dict) -> str | None:
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
            await run_sync(
                self._mark_job_failed_sync,
                job["job_id"],
                "ATTACHMENT_INVALID",
                "任务缺少源消息位置，无法用用户账户下载",
            )
            return None
        client = None
        if self._telegram_user_client is not None:
            client = await self._telegram_user_client()
        if client is None:
            await run_sync(
                self._mark_job_failed_sync,
                job["job_id"],
                "TELEGRAM_USER_NOT_CONFIG",
                "尚未登录 Telegram 用户账户，无法下载大文件",
            )
            return None
        work_path = await self._effective_work_path()
        destination = (
            work_path / "downloads" / f"job-{job['job_id']}-{Path(file_name).name}"
        )
        destination = attempt_path(destination)
        try:
            size = await client.download_message_media(
                chat_id, message_id, destination
            )
        except ProviderConnectionError as exc:
            await run_sync(
                self._mark_job_failed_sync,
                job["job_id"],
                exc.code,
                exc.public_message,
            )
            return refusal_detail(exc)
        await run_sync(
            self._record_artifact_sync,
            job["job_id"],
            destination,
            size,
            str(details.get("file_unique_id") or f"{chat_id}:{message_id}"),
        )
        await run_sync(self._mark_job_completed_sync, job["job_id"])
        await self._maybe_auto_pack(job["candidate_id"])
        return None

    async def _push_torrent_job(self, job: dict) -> None:
        """Hand the torrent to the client and park the job on peers.

        This provider is the one that cannot finish inside a worker turn: the
        transfer belongs to qBittorrent. Parking in `WAITING_TORRENT` releases
        the concurrency slot so a long, seederless torrent does not block the
        queue, and `TorrentService`'s poller advances it from there.
        """
        if self._torrent_push is None:
            await run_sync(
                self._mark_job_failed_sync,
                job["job_id"],
                "TORRENT_CLIENT_NOT_CONFIG",
                "\u79cd\u5b50\u4e0b\u8f7d\u670d\u52a1\u672a\u914d\u7f6e",
            )
            return
        try:
            details = await self._torrent_push(job["candidate_id"])
        except Exception as exc:  # noqa: BLE001 - provider boundary
            await run_sync(
                self._mark_job_failed_sync,
                job["job_id"],
                str(getattr(exc, "code", "TORRENT_PUSH_REJECTED")),
                str(getattr(exc, "public_message", exc)),
            )
            return
        await run_sync(
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
            await run_sync(
                self._mark_job_failed_sync,
                job["job_id"],
                missing_code,
                missing_message,
            )
            return
        try:
            await handler(job["candidate_id"])
        except Exception as exc:  # noqa: BLE001 - provider boundary
            await run_sync(
                self._mark_job_failed_sync,
                job["job_id"],
                str(getattr(exc, "code", default_error)),
                str(getattr(exc, "public_message", exc)),
            )
            return
        await run_sync(
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
            connection.execute("BEGIN IMMEDIATE")
            self._check_job_source_sync(connection, job_id)
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
            connection.execute("BEGIN IMMEDIATE")
            self._check_job_source_sync(connection, job_id)
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

    def _check_job_source_sync(self, connection, job_id: int) -> None:
        """A source edit cannot publish bytes fetched under an older revision."""
        if self._account_scope is None:
            return
        row = connection.execute("SELECT candidate_id, provider, details_json FROM download_jobs WHERE id = ?", (job_id,)).fetchone()
        if row is not None and row[1] in {PROVIDER_TELEGRAM, PROVIDER_TELEGRAM_USER}:
            self._validate_telegram_source_sync(row[0], self._safe_details(row[2]),
                "TG_BOT" if row[1] == PROVIDER_TELEGRAM else None, connection)

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
        with failure_transaction(self._database) as connection:
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
            # A failed push/poll may still own an external transfer. Preserve
            # the FAILED attempt, but retain the active slot for its cleanup.
            current = task_snapshot(connection, job_id)
            if connection.execute(
                "SELECT 1 FROM queue_remote_resources WHERE task_id = ? AND status <> 'STOP_CONFIRMED' "
                "UNION ALL SELECT 1 FROM download_jobs WHERE id = ? AND provider = 'EH_TORRENT' "
                "AND details_json NOT IN ('', '{}') LIMIT 1",
                (current["task_id"], job_id),
            ).fetchone() is not None:
                self._queue_control._operation(connection, job_id, "cancel", current, {})
                connection.execute("UPDATE download_jobs SET state = 'STOPPING', updated_at = CURRENT_TIMESTAMP "
                                   "WHERE id = ?", (job_id,))

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
