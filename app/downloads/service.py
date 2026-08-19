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
    DOWNLOAD_STATE_COMPLETED,
    DOWNLOAD_STATE_DOWNLOADING,
    DOWNLOAD_STATE_FAILED,
    DOWNLOAD_STATE_PENDING,
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
    ) -> None:
        self._database = database
        self._work_path = work_path
        self._telegram_client_factory = telegram_client_factory
        self._worker_task: asyncio.Task[None] | None = None

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
            if str(candidate_row[0]) not in {"APPROVED", "PENDING_REVIEW"}:
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
        placeholders = ",".join("?" for _ in ACTIVE_DOWNLOAD_STATES)
        return self._fetch_jobs(
            "WHERE state IN ("
            + placeholders
            + ") ORDER BY id DESC LIMIT 50",
            tuple(ACTIVE_DOWNLOAD_STATES),
        )

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
                "WHERE state = ? ORDER BY id LIMIT 1",
                (DOWNLOAD_STATE_PENDING,),
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
            return {
                "job_id": int(row[0]),
                "candidate_id": int(row[1]),
                "provider": str(row[2]),
                "idempotency_key": str(row[3]),
                "details_json": str(row[4] or "{}"),
                "attempt_count": int(row[5]) + 1,
            }

    async def _handle_job(self, job: dict) -> None:
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
            destination = (
                self._work_path
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


__all__ = [
    "DownloadError",
    "DownloadService",
    "DownloadState",
]