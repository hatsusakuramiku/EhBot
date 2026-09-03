"""Download a candidate's Telegraph preview page as an archive artifact.

The service deliberately produces nothing but a ZIP plus an ``ARCHIVE``
artifact row, because that is exactly what ``ConversionService`` already
consumes. Everything after this point — safety validation, CBZ packing,
ComicInfo injection, atomic publication — is the existing pipeline unchanged.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import replace
from pathlib import Path

import httpx

from app.db.database import Database
from app.telegraph.client import TelegraphClient
from app.telegraph.fetcher import FetchLimits, TelegraphFetcher
from app.telegraph.models import (
    TelegraphError,
    TelegraphFetchResult,
    TelegraphPage,
)
from app.telegraph.packer import pack_images


PROVIDER_NAME = "TELEGRAPH"

#: Written to ComicInfo so a reading-grade book can be found and replaced
#: later; the value states the source, the width, the page count and the size.
SCAN_INFORMATION_SOURCE = "TELEGRAPH_PREVIEW"


def _mebibytes(total_bytes: int) -> str:
    return f"{total_bytes / (1024 * 1024):.1f}MiB"


def scan_information(image_count: int, total_bytes: int) -> str:
    return (
        f"{SCAN_INFORMATION_SOURCE} w1280 {image_count}p "
        f"{_mebibytes(total_bytes)}"
    )


class TelegraphService:
    def __init__(
        self,
        database: Database,
        work_path: Path,
        http_client: httpx.AsyncClient | None = None,
        limits: FetchLimits | None = None,
        *,
        require_filecount_match: bool = True,
        work_path_provider=None,
        concurrency_provider=None,
        resolver=None,
    ) -> None:
        self._database = database
        self._work_path = work_path
        self._http_client = http_client
        self._limits = limits or FetchLimits()
        self._require_filecount_match = require_filecount_match
        # Resolved per download so an operator directory change applies
        # without a restart; the constructor value stays the default.
        self._work_path_provider = work_path_provider
        # Same contract for the image concurrency: the settings page writes a
        # number, and the next download honours it. Only this one limit is
        # operator-editable -- the byte and count ceilings are safety limits
        # against a hostile page, not a preference.
        self._concurrency_provider = concurrency_provider
        self._resolver = resolver

    async def _effective_limits(self) -> FetchLimits:
        if self._concurrency_provider is None:
            return self._limits
        concurrency = await self._concurrency_provider()
        if not concurrency:
            return self._limits
        return replace(self._limits, concurrency=int(concurrency))

    async def _effective_work_path(self) -> Path:
        if self._work_path_provider is None:
            return self._work_path
        resolved = await self._work_path_provider()
        return resolved or self._work_path

    def _client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            raise TelegraphError(
                "TELEGRAPH_PAGE_UNREACHABLE", "HTTP 客户端未配置"
            )
        return self._http_client

    async def download_for_candidate(
        self, candidate_id: int
    ) -> TelegraphFetchResult:
        preview_url, expected_pages = await asyncio.to_thread(
            self._candidate_preview_sync, candidate_id
        )
        client = self._client()
        page = await TelegraphClient(client).fetch_page(preview_url)
        self._check_page_count(page, expected_pages)
        images = await TelegraphFetcher(
            client, await self._effective_limits(), resolver=self._resolver
        ).fetch_all(page.url, page.image_urls)
        # The count is re-checked because a host can drop a page mid-fetch.
        if (
            self._require_filecount_match
            and expected_pages is not None
            and len(images) != expected_pages
        ):
            raise self._page_count_error(len(images), expected_pages)

        work_path = await self._effective_work_path()
        destination = (
            work_path / "telegraph" / f"candidate-{candidate_id}.zip"
        )
        total_bytes = await asyncio.to_thread(
            pack_images, images, destination
        )
        hosts = tuple(
            dict.fromkeys(
                httpx.URL(image.source_url).host for image in images
            )
        )
        result = TelegraphFetchResult(
            page=page,
            archive_path=str(destination),
            image_count=len(images),
            total_bytes=total_bytes,
            hosts=hosts,
        )
        await asyncio.to_thread(
            self._record_artifact_sync, candidate_id, destination, result
        )
        await asyncio.to_thread(
            self._record_provenance_sync, candidate_id, result
        )
        logging.getLogger(__name__).info(
            "telegraph_download_completed candidate=%d pages=%d bytes=%d",
            candidate_id,
            result.image_count,
            result.total_bytes,
        )
        return result

    def _check_page_count(
        self, page: TelegraphPage, expected_pages: int | None
    ) -> None:
        if not self._require_filecount_match or expected_pages is None:
            return
        if len(page.image_urls) != expected_pages:
            raise self._page_count_error(
                len(page.image_urls), expected_pages
            )

    @staticmethod
    def _page_count_error(found: int, expected: int) -> TelegraphError:
        """A short book is never published; the operator is asked for the rest.

        A preview page split into `Page-1`/`Page-2`, or an image host that has
        dropped a file, both land here. The message carries both counts so the
        review queue can state exactly what is missing.
        """
        return TelegraphError(
            "TELEGRAPH_PAGE_COUNT_MISMATCH",
            f"预览页只有 {found}/{expected} 页，请补充其余预览链接",
        )

    def _candidate_preview_sync(
        self, candidate_id: int
    ) -> tuple[str, int | None]:
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT preview_url FROM candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()
            if row is None:
                raise TelegraphError(
                    "CANDIDATE_NOT_FOUND", "候选不存在或已被删除"
                )
            if not row[0]:
                raise TelegraphError(
                    "TELEGRAPH_PAGE_UNREACHABLE",
                    "候选没有关联的预览页链接",
                )
            pages_row = connection.execute(
                "SELECT field_value FROM metadata_values "
                "WHERE candidate_id = ? AND field_name = 'Pages' "
                "ORDER BY is_manual DESC LIMIT 1",
                (candidate_id,),
            ).fetchone()
        expected: int | None = None
        if pages_row is not None and pages_row[0] is not None:
            try:
                expected = int(str(pages_row[0]).strip())
            except ValueError:
                expected = None
            if expected is not None and expected <= 0:
                expected = None
        return str(row[0]), expected

    def _record_artifact_sync(
        self,
        candidate_id: int,
        destination: Path,
        result: TelegraphFetchResult,
    ) -> None:
        """Register the ZIP as a completed download so conversion can start.

        The job is written directly in ``COMPLETED`` state with the same
        `idempotency_key` shape the queue uses, so a re-run updates one row
        instead of accumulating duplicates.
        """
        sha256 = hashlib.sha256()
        with destination.open("rb") as handle:
            while True:
                chunk = handle.read(64 * 1024)
                if not chunk:
                    break
                sha256.update(chunk)
        size = destination.stat().st_size
        key = f"telegraph:{candidate_id}"
        details = json.dumps(
            {
                "page_url": result.page.url,
                "hosts": list(result.hosts),
                "image_count": result.image_count,
                "total_bytes": result.total_bytes,
                "path": str(destination),
                "sha256": sha256.hexdigest(),
            },
            separators=(",", ":"),
            ensure_ascii=False,
        )
        with self._database.connection() as connection:
            connection.execute(
                "INSERT INTO download_jobs "
                "(candidate_id, idempotency_key, provider, state, "
                "details_json) VALUES (?, ?, ?, 'COMPLETED', ?) "
                "ON CONFLICT(idempotency_key) DO UPDATE SET "
                "state = 'COMPLETED', details_json = excluded.details_json, "
                "error_code = NULL, error_message = NULL, "
                "updated_at = CURRENT_TIMESTAMP",
                (candidate_id, key, PROVIDER_NAME, details),
            )
            row = connection.execute(
                "SELECT id FROM download_jobs WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
            job_id = int(row[0])
            connection.execute(
                "INSERT INTO artifacts "
                "(job_id, artifact_type, path, sha256, size_bytes) "
                "VALUES (?, 'ARCHIVE', ?, ?, ?) "
                "ON CONFLICT(job_id, artifact_type) DO UPDATE SET "
                "path = excluded.path, sha256 = excluded.sha256, "
                "size_bytes = excluded.size_bytes",
                (job_id, str(destination), sha256.hexdigest(), int(size)),
            )

    def _record_provenance_sync(
        self, candidate_id: int, result: TelegraphFetchResult
    ) -> None:
        """Record the source grade in metadata, never in the file name.

        The name is left alone on purpose: replacing a preview-grade book with
        the original later must not break the library index.
        """
        with self._database.connection() as connection:
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
                    scan_information(
                        result.image_count, result.total_bytes
                    ),
                    PROVIDER_NAME,
                ),
            )


__all__ = [
    "PROVIDER_NAME",
    "SCAN_INFORMATION_SOURCE",
    "TelegraphError",
    "TelegraphService",
    "scan_information",
]