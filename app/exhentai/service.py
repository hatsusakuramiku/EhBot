from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path

import httpx

from app.connections.exhentai import ExHentaiCredentials
from app.connections.models import ProviderConnectionError
from app.db.database import Database
from app.exhentai.downloader import (
    ExHentaiDownloadError,
    ExHentaiDownloader,
)
from app.exhentai.enrich import enrich_metadata
from app.exhentai.gdata_client import GdataClient, GdataError
from app.exhentai.tagdb import TagTranslator


PROVIDER_NAME = "EXHENTAI"


class ExHentaiService:
    def __init__(
        self,
        database: Database,
        work_path: Path,
        library_path: Path,
        credentials_provider,
        http_client: httpx.AsyncClient | None = None,
        translator: TagTranslator | None = None,
        work_path_provider=None,
    ) -> None:
        self._database = database
        self._work_path = work_path
        self._library_path = library_path
        self._credentials_provider = credentials_provider
        self._http_client = http_client
        self._translator = translator
        # Resolved per download so an operator directory change applies
        # without a restart; the constructor value stays the default.
        self._work_path_provider = work_path_provider

    async def _effective_work_path(self) -> Path:
        if self._work_path_provider is None:
            return self._work_path
        resolved = await self._work_path_provider()
        return resolved or self._work_path

    async def fetch_metadata_for_candidate(self, candidate_id: int) -> dict:
        gid, token = await self._candidate_gid_token(candidate_id)
        metadata = await self._fetch_metadata(gid, token)
        await asyncio.to_thread(
            self._persist_metadata_sync, candidate_id, metadata
        )
        await self._database.re_evaluate_candidate_metadata_rules(
            candidate_id
        )
        return metadata

    async def enrich_candidates_for_review(self, candidates: list) -> int:
        refs = await asyncio.to_thread(
            self._missing_metadata_refs_sync, candidates
        )
        if not refs:
            return 0

        async with self._http_session() as client:
            try:
                galleries = await GdataClient(client).fetch_many(
                    [(gid, token) for _, gid, token in refs]
                )
            except GdataError as exc:
                logging.getLogger(__name__).warning(
                    "review_metadata_enrichment_failed",
                    extra={"error_code": exc.code},
                )
                return 0

        enriched = 0
        for candidate_id, gid, token in refs:
            gallery = galleries.get(gid)
            if gallery is not None:
                metadata = enrich_metadata(gallery, self._translator)
            else:
                try:
                    metadata = await self._fetch_metadata(gid, token)
                except ExHentaiDownloadError as exc:
                    logging.getLogger(__name__).warning(
                        "review_metadata_fallback_failed",
                        extra={"error_code": exc.code},
                    )
                    continue
            await asyncio.to_thread(
                self._persist_metadata_sync, candidate_id, metadata
            )
            await self._database.re_evaluate_candidate_metadata_rules(
                candidate_id
            )
            enriched += 1
        return enriched

    def _missing_metadata_refs_sync(
        self, candidates: list
    ) -> list[tuple[int, int, str]]:
        refs = [
            (
                int(candidate.candidate_id),
                int(candidate.ex_gid),
                str(candidate.ex_gallery_token),
            )
            for candidate in candidates
            if candidate.ex_gid is not None and candidate.ex_gallery_token
        ]
        if not refs:
            return []
        placeholders = ",".join("?" for _ in refs)
        with self._database._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT candidate_id FROM metadata_values "
                "WHERE value_source = 'EXHENTAI' AND candidate_id IN ("
                + placeholders
                + ")",
                tuple(candidate_id for candidate_id, _, _ in refs),
            ).fetchall()
        existing = {int(row[0]) for row in rows}
        return [ref for ref in refs if ref[0] not in existing]

    async def _fetch_metadata(self, gid: int, token: str) -> dict:
        """Prefer the gdata API, falling back to authenticated HTML scraping.

        gdata needs no Cookie and returns namespaced tags, so it yields far
        more fields than the gallery page. Expunged galleries are missing
        from gdata, so HTML remains the fallback.
        """
        async with self._http_session() as client:
            try:
                gallery = await GdataClient(client).fetch_one(gid, token)
            except GdataError as exc:
                logging.getLogger(__name__).info(
                    "exhentai_gdata_fallback",
                    extra={"error_code": exc.code},
                )
            else:
                return enrich_metadata(gallery, self._translator)

            credentials = await self._credentials_provider()
            if credentials is None:
                raise ExHentaiDownloadError(
                    "EXHENTAI_NOT_CONFIG",
                    "gdata 未收录该画廊，需要配置 ExHentai Cookie 后重试",
                )
            return await ExHentaiDownloader(client).fetch_metadata(
                credentials, gid, token
            )

    async def download_archive_for_candidate(
        self, candidate_id: int
    ) -> dict:
        gid, token = await self._candidate_gid_token(candidate_id)
        credentials = await self._credentials_provider()
        if credentials is None:
            raise ExHentaiDownloadError(
                "EXHENTAI_NOT_CONFIG",
                "ExHentai Cookie 未配置",
            )
        async with self._http_session() as client:
            downloader = ExHentaiDownloader(client)
            archive_url = await downloader.request_archive_url(
                credentials, gid, token
            )
            work_path = await self._effective_work_path()
            destination = (
                work_path
                / "exhentai"
                / f"gallery-{gid}.zip"
            )
            size = await downloader.download_archive(
                credentials, archive_url, destination
            )
        await asyncio.to_thread(
            self._record_artifact_sync,
            candidate_id,
            destination,
            size,
            archive_url,
        )
        return {
            "path": str(destination),
            "size": size,
            "url": archive_url,
        }

    def _http_session(self):
        if self._http_client is None:
            raise ExHentaiDownloadError(
                "EXHENTAI_HTTP_CLIENT",
                "HTTP 客户端未配置",
            )
        return _HttpClientContext(self._http_client)

    async def _candidate_gid_token(
        self, candidate_id: int
    ) -> tuple[int, str]:
        return await asyncio.to_thread(
            self._candidate_gid_token_sync, candidate_id
        )

    def _candidate_gid_token_sync(
        self, candidate_id: int
    ) -> tuple[int, str]:
        with self._database._connect() as connection:  # noqa: SLF001
            row = connection.execute(
                "SELECT ex_gid, ex_gallery_token FROM candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()
            if row is None or row[0] is None:
                raise ExHentaiDownloadError(
                    "CANDIDATE_HAS_NO_EX_REFERENCE",
                    "候选没有关联的 ExHentai 画廊",
                )
            return int(row[0]), str(row[1])

    def _persist_metadata_sync(
        self, candidate_id: int, metadata: dict
    ) -> None:
        with self._database._connect() as connection:
            for field_name, value in metadata.items():
                if value is None or value == "":
                    continue
                confidence = 0.6
                connection.execute(
                    "INSERT INTO metadata_values "
                    "(candidate_id, field_name, field_value, value_source, "
                    "confidence, is_manual) "
                    "VALUES (?, ?, ?, 'EXHENTAI', ?, 0) "
                    "ON CONFLICT(candidate_id, field_name, value_source) "
                    "DO UPDATE SET field_value = excluded.field_value, "
                    "confidence = excluded.confidence, "
                    "is_manual = 0, created_at = CURRENT_TIMESTAMP "
                    "WHERE metadata_values.is_manual = 0",
                    (candidate_id, field_name, str(value), confidence),
                )

    def _record_artifact_sync(
        self,
        candidate_id: int,
        destination: Path,
        size: int,
        archive_url: str,
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
                "INSERT INTO download_jobs "
                "(candidate_id, idempotency_key, provider, state, "
                "details_json) VALUES (?, ?, ?, 'COMPLETED', ?) "
                "ON CONFLICT(idempotency_key) DO NOTHING",
                (
                    candidate_id,
                    f"exhentai:{candidate_id}",
                    PROVIDER_NAME,
                    json.dumps(
                        {
                            "url": archive_url,
                            "size": size,
                            "sha256": sha256.hexdigest(),
                            "path": str(destination),
                        },
                        separators=(",", ":"),
                    ),
                ),
            )
            row = connection.execute(
                "SELECT id FROM download_jobs WHERE idempotency_key = ?",
                (f"exhentai:{candidate_id}",),
            ).fetchone()
            job_id = int(row[0])
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


class _HttpClientContext:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def __aenter__(self) -> httpx.AsyncClient:
        return self._client

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


__all__ = ["ExHentaiService", "ExHentaiDownloadError"]
