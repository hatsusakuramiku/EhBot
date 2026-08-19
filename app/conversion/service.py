from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from app.conversion.convert import (
    ConversionError,
    detect_format,
    is_supported,
    stream_zip_to_cbz,
)
from app.db.database import Database
from app.downloads.models import (
    DOWNLOAD_STATE_COMPLETED,
    DOWNLOAD_STATE_FAILED,
    DOWNLOAD_STATE_PENDING,
    DownloadState,
)
from app.review.models import (
    METADATA_FIELDS,
    STATUS_APPROVED,
)


CONVERSION_STATE_PENDING = "CONVERSION_PENDING"
CONVERSION_STATE_RUNNING = "CONVERSION_RUNNING"
CONVERSION_STATE_COMPLETED = "CONVERSION_COMPLETED"
CONVERSION_STATE_FAILED = "CONVERSION_FAILED"


def _metadata_lookup(metadata, field_name: str) -> str | None:
    for entry in metadata:
        if entry.field_name == field_name:
            return entry.field_value
    return None


class ConversionService:
    def __init__(
        self,
        database: Database,
        work_path: Path,
        library_path: Path,
    ) -> None:
        self._database = database
        self._work_path = work_path
        self._library_path = library_path
        self._worker_task: asyncio.Task[None] | None = None

    async def enqueue_for_candidate(self, candidate_id: int) -> int:
        return await asyncio.to_thread(
            self._enqueue_sync, candidate_id
        )

    def _enqueue_sync(self, candidate_id: int) -> int:
        with self._database._connect() as connection:  # noqa: SLF001
            candidate_row = connection.execute(
                "SELECT status FROM candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()
            if candidate_row is None:
                raise ConversionError(
                    "CANDIDATE_NOT_FOUND",
                    "Candidate does not exist",
                )
            if str(candidate_row[0]) != STATUS_APPROVED:
                raise ConversionError(
                    "CANDIDATE_NOT_READY",
                    "Only approved candidates can be converted",
                )
            artifact_row = connection.execute(
                "SELECT a.path FROM download_jobs dj "
                "JOIN artifacts a ON a.job_id = dj.id "
                "WHERE dj.candidate_id = ? AND dj.state = ? "
                "AND a.artifact_type = 'ARCHIVE' "
                "ORDER BY dj.id DESC LIMIT 1",
                (candidate_id, DOWNLOAD_STATE_COMPLETED),
            ).fetchone()
            if artifact_row is None:
                raise ConversionError(
                    "ARCHIVE_NOT_READY",
                    "No downloaded archive available for conversion",
                )
            before = connection.total_changes
            connection.execute(
                "INSERT INTO download_jobs "
                "(candidate_id, idempotency_key, provider, state, "
                "details_json) VALUES (?, ?, 'CONVERSION', ?, '{}') "
                "ON CONFLICT(idempotency_key) DO NOTHING",
                (
                    candidate_id,
                    f"convert:{candidate_id}",
                    CONVERSION_STATE_PENDING,
                ),
            )
            created = connection.total_changes > before
            row = connection.execute(
                "SELECT id FROM download_jobs WHERE idempotency_key = ?",
                (f"convert:{candidate_id}",),
            ).fetchone()
            return int(row[0]) if created else int(row[0])

    async def start(self) -> None:
        if self._worker_task is not None:
            return
        self._worker_task = asyncio.create_task(
            self._run_worker(), name="conversion-worker"
        )

    async def stop(self) -> None:
        if self._worker_task is None:
            return
        self._worker_task.cancel()
        await asyncio.gather(
            self._worker_task, return_exceptions=True
        )
        self._worker_task = None

    async def _run_worker(self) -> None:
        while True:
            try:
                processed = await self._process_one()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logging.getLogger(__name__).exception(
                    "conversion_worker_error",
                    extra={"error_code": "CONVERSION_WORKER_ERROR"},
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
                self._mark_failed_sync, job["job_id"], "WORKER_EXCEPTION", str(exc)
            )
        return True

    def _claim_pending_job_sync(self) -> dict | None:
        with self._database._connect() as connection:
            row = connection.execute(
                "SELECT id, candidate_id FROM download_jobs "
                "WHERE state = ? ORDER BY id LIMIT 1",
                (CONVERSION_STATE_PENDING,),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE download_jobs SET state = ?, "
                "attempt_count = attempt_count + 1, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (CONVERSION_STATE_RUNNING, int(row[0])),
            )
            return {
                "job_id": int(row[0]),
                "candidate_id": int(row[1]),
            }

    async def _handle_job(self, job: dict) -> None:
        source_artifact = await asyncio.to_thread(
            self._fetch_source_artifact_sync, job["candidate_id"]
        )
        if source_artifact is None:
            await asyncio.to_thread(
                self._mark_failed_sync,
                job["job_id"],
                "ARCHIVE_NOT_READY",
                "Archive artifact no longer exists",
            )
            return
        source_path = Path(source_artifact["path"])
        if not source_path.exists():
            await asyncio.to_thread(
                self._mark_failed_sync,
                job["job_id"],
                "ARCHIVE_MISSING",
                "Archive file was removed before conversion",
            )
            return
        if not is_supported(source_path):
            await asyncio.to_thread(
                self._mark_failed_sync,
                job["job_id"],
                "FORMAT_UNSUPPORTED",
                f"压缩格式 {detect_format(source_path)} 暂不支持转换",
            )
            return

        metadata = await asyncio.to_thread(
            self._fetch_metadata_sync, job["candidate_id"]
        )
        title = (
            _metadata_lookup(metadata, "Title")
            or f"Candidate {job['candidate_id']}"
        )
        library_target = (
            self._library_path / f"{title}.cbz"
        )
        try:
            page_count = await asyncio.to_thread(
                stream_zip_to_cbz,
                source_path,
                library_target,
                title=title,
                artist=_metadata_lookup(metadata, "Artist"),
                language=_metadata_lookup(metadata, "Language"),
                category=_metadata_lookup(metadata, "Category"),
                tags=(
                        _metadata_lookup(metadata, "Tags").split(", ")
                        if _metadata_lookup(metadata, "Tags")
                        else ()
                    ),
                rating=(
                    float(_metadata_lookup(metadata, "Rating"))
                    if _metadata_lookup(metadata, "Rating")
                    else None
                ),
                description=_metadata_lookup(metadata, "Description"),
                japanese_title=_metadata_lookup(metadata, "JapaneseTitle"),
                group=_metadata_lookup(metadata, "Group"),
                parody=_metadata_lookup(metadata, "Parody"),
                character=_metadata_lookup(metadata, "Character"),
                web=_metadata_lookup(metadata, "Web"),
            )
        except ConversionError as exc:
            await asyncio.to_thread(
                self._mark_failed_sync,
                job["job_id"],
                exc.code,
                exc.public_message,
            )
            return
        await asyncio.to_thread(
            self._record_cbz_artifact_sync,
            job["job_id"],
            library_target,
            page_count,
        )
        await asyncio.to_thread(
            self._mark_completed_sync, job["job_id"]
        )

    def _fetch_source_artifact_sync(
        self, candidate_id: int
    ) -> dict | None:
        with self._database._connect() as connection:
            row = connection.execute(
                "SELECT a.path FROM download_jobs dj "
                "JOIN artifacts a ON a.job_id = dj.id "
                "WHERE dj.candidate_id = ? AND dj.state = ? "
                "AND a.artifact_type = 'ARCHIVE' "
                "ORDER BY dj.id DESC LIMIT 1",
                (candidate_id, DOWNLOAD_STATE_COMPLETED),
            ).fetchone()
            return {"path": str(row[0])} if row else None

    def _fetch_metadata_sync(self, candidate_id: int) -> list:
        with self._database._connect() as connection:
            rows = connection.execute(
                "SELECT field_name, field_value FROM metadata_values "
                "WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchall()
        return [
            type("MetaRow", (), {"field_name": r[0], "field_value": r[1]})
            for r in rows
        ]

    def _record_cbz_artifact_sync(
        self,
        job_id: int,
        destination: Path,
        page_count: int,
    ) -> None:
        with self._database._connect() as connection:
            connection.execute(
                "INSERT INTO artifacts "
                "(job_id, artifact_type, path, size_bytes) "
                "VALUES (?, 'CBZ', ?, ?) "
                "ON CONFLICT(job_id, artifact_type) DO UPDATE SET "
                "path = excluded.path, size_bytes = excluded.size_bytes",
                (
                    job_id,
                    str(destination),
                    page_count,
                ),
            )

    def _mark_completed_sync(self, job_id: int) -> None:
        with self._database._connect() as connection:
            connection.execute(
                "UPDATE download_jobs SET state = ?, error_code = NULL, "
                "error_message = NULL, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ?",
                (CONVERSION_STATE_COMPLETED, job_id),
            )

    def _mark_failed_sync(
        self, job_id: int, code: str, message: str
    ) -> None:
        with self._database._connect() as connection:
            connection.execute(
                "UPDATE download_jobs SET state = ?, error_code = ?, "
                "error_message = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ?",
                (CONVERSION_STATE_FAILED, code, message, job_id),
            )


__all__ = [
    "ConversionError",
    "ConversionService",
    "CONVERSION_STATE_PENDING",
    "CONVERSION_STATE_RUNNING",
    "CONVERSION_STATE_COMPLETED",
    "CONVERSION_STATE_FAILED",
]