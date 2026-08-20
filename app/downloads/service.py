from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path

from app.connections.models import ProviderConnectionError
from app.connections.telegram import TelegramBotApi
from app.db.database import Database
from app.downloads.models import (
    ACTIVE_DOWNLOAD_STATES,
    DOWNLOAD_STATE_CANCELLED,
    DOWNLOAD_STATE_COMPLETED,
    DOWNLOAD_STATE_DOWNLOADING,
    DOWNLOAD_STATE_FAILED,
    DOWNLOAD_STATE_PAUSED,
    DOWNLOAD_STATE_PENDING,
    OPEN_DOWNLOAD_STATES,
    PERMANENT_DOWNLOAD_ERRORS,
    PROVIDER_EXHENTAI,
    PROVIDER_TELEGRAM,
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
        exhentai_download=None,
        work_path_provider=None,
    ) -> None:
        self._database = database
        self._work_path = work_path
        self._telegram_client_factory = telegram_client_factory
        self._exhentai_download = exhentai_download
        # Resolved per job so an operator directory change applies without a
        # restart; the constructor value stays the default.
        self._work_path_provider = work_path_provider
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

    async def enqueue_exhentai_download(
        self, candidate_id: int
    ) -> DownloadEnqueueResult:
        return await self._enqueue(
            candidate_id,
            PROVIDER_EXHENTAI,
            f"exhentai:{candidate_id}",
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
        with self._database._connect() as connection:  # noqa: SLF001
            candidate_row = connection.execute(
                "SELECT status FROM candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()
            if candidate_row is None:
                raise DownloadError(
                    "CANDIDATE_NOT_FOUND",
                    "Candidate does not exist",
                )
            if str(candidate_row[0]) != "APPROVED":
                raise DownloadError(
                    "CANDIDATE_NOT_DOWNLOADABLE",
                    "Only approved candidates can be queued for download",
                )
            before = connection.total_changes
            connection.execute(
                "INSERT INTO download_jobs "
                "(candidate_id, idempotency_key, provider, state, "
                "details_json) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(idempotency_key) DO NOTHING",
                (
                    candidate_id,
                    idempotency_key,
                    provider,
                    DOWNLOAD_STATE_PENDING,
                    details_json,
                ),
            )
            created = connection.total_changes > before
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

    async def list_active_jobs(self) -> tuple[DownloadJobSummary, ...]:
        return await asyncio.to_thread(self._list_active_jobs_sync)

    def _list_active_jobs_sync(self) -> tuple[DownloadJobSummary, ...]:
        states = tuple(sorted(OPEN_DOWNLOAD_STATES))
        placeholders = ",".join("?" for _ in states)
        return self._fetch_jobs(
            "WHERE state IN ("
            + placeholders
            + ") ORDER BY id DESC LIMIT 50",
            states,
        )

    async def retry_job(self, job_id: int) -> str:
        """Requeue a failed or paused job without creating a duplicate.

        The original row is reused so the `idempotency_key` contract holds and
        the attempt history is preserved.
        """
        return await asyncio.to_thread(self._retry_job_sync, job_id)

    def _retry_job_sync(self, job_id: int) -> str:
        with self._database._connect() as connection:
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
            # The candidate went to FAILED when the job did, so it has to be
            # eligible again or the worker would refuse to process the retry.
            connection.execute(
                "UPDATE candidates SET status = 'APPROVED', "
                "filter_reason = '', updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND status IN ('FAILED', 'PROCESSING')",
                (int(row[2]),),
            )
        return DOWNLOAD_STATE_PENDING

    async def pause_job(self, job_id: int) -> str:
        """Hold a queued job so the worker skips it until it is resumed."""
        return await asyncio.to_thread(self._pause_job_sync, job_id)

    def _pause_job_sync(self, job_id: int) -> str:
        with self._database._connect() as connection:
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
        with self._database._connect() as connection:
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
        return await asyncio.to_thread(self._cancel_job_sync, job_id)

    def _cancel_job_sync(self, job_id: int) -> str:
        with self._database._connect() as connection:
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
        with self._database._connect() as connection:
            rows = connection.execute(
                "SELECT id, candidate_id, provider, state, attempt_count, "
                "error_code, error_message, "
                "(SELECT path FROM artifacts WHERE job_id = download_jobs.id "
                " AND artifact_type = 'ARCHIVE' LIMIT 1), "
                "(SELECT size_bytes FROM artifacts WHERE job_id = download_jobs.id "
                " AND artifact_type = 'ARCHIVE' LIMIT 1), "
                "created_at, updated_at FROM download_jobs " + where_sql,
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
                created_at=str(row[9]),
                updated_at=str(row[10]),
            )
            for row in rows
        )

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
        return True

    def _claim_pending_job_sync(self) -> dict | None:
        with self._database._connect() as connection:
            row = connection.execute(
                "SELECT id, candidate_id, provider, idempotency_key, "
                "details_json, attempt_count FROM download_jobs "
                "WHERE state = ? AND provider IN (?, ?) "
                "ORDER BY id LIMIT 1",
                (
                    DOWNLOAD_STATE_PENDING,
                    PROVIDER_TELEGRAM,
                    PROVIDER_EXHENTAI,
                ),
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
        if job["provider"] == PROVIDER_EXHENTAI:
            if self._exhentai_download is None:
                await asyncio.to_thread(
                    self._mark_job_failed_sync,
                    job["job_id"],
                    "EXHENTAI_NOT_CONFIG",
                    "ExHentai 下载服务未配置",
                )
                return
            try:
                await self._exhentai_download(job["candidate_id"])
            except Exception as exc:  # noqa: BLE001 - provider boundary
                await asyncio.to_thread(
                    self._mark_job_failed_sync,
                    job["job_id"],
                    str(getattr(exc, "code", "EXHENTAI_DOWNLOAD_FAILED")),
                    str(getattr(exc, "public_message", exc)),
                )
                return
            await asyncio.to_thread(
                self._mark_job_completed_sync, job["job_id"]
            )
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
        except ProviderConnectionError as exc:
            await asyncio.to_thread(
                self._mark_job_failed_sync,
                job["job_id"],
                exc.code,
                exc.public_message,
            )

    async def _telegram_api(self) -> TelegramBotApi | None:
        if self._telegram_client_factory is None:
            return None
        token, client = await self._telegram_client_factory()
        if not token or client is None:
            return None
        return TelegramBotApi(token, client)

    def _mark_job_completed_sync(self, job_id: int) -> None:
        with self._database._connect() as connection:
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
        with self._database._connect() as connection:
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
        with self._database._connect() as connection:
            connection.execute(
                "UPDATE download_jobs SET state = ?, error_code = ?, "
                "error_message = ?, lease_owner = NULL, "
                "lease_expires_at = NULL, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (DOWNLOAD_STATE_FAILED, code, message, job_id),
            )
            connection.execute(
                "UPDATE candidates SET status = 'FAILED', "
                "filter_reason = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = (SELECT candidate_id FROM download_jobs "
                "WHERE id = ?)",
                (message, job_id),
            )


__all__ = [
    "DownloadError",
    "DownloadService",
    "DownloadState",
]
