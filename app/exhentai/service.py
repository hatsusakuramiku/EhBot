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


PROVIDER_NAME = "EXHENTAI"


class ExHentaiService:
    def __init__(
        self,
        database: Database,
        work_path: Path,
        library_path: Path,
        credentials_provider,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._database = database
        self._work_path = work_path
        self._library_path = library_path
        self._credentials_provider = credentials_provider
        self._http_client = http_client

    async def fetch_metadata_for_candidate(self, candidate_id: int) -> dict:
        gid, token = await self._candidate_gid_token(candidate_id)
        credentials = await self._credentials_provider()
        if credentials is None:
            raise ExHentaiDownloadError(
                "EXHENTAI_NOT_CONFIG",
                "ExHentai Cookie 未配置",
            )
        async with self._http_session() as client:
            downloader = ExHentaiDownloader(client)
            metadata = await downloader.fetch_metadata(
                credentials, gid, token
            )
        await asyncio.to_thread(
            self._persist_metadata_sync, candidate_id, metadata
        )
        await self._database.re_evaluate_candidate_metadata_rules(
            candidate_id
        )
        return metadata

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
            destination = (
                self._work_path
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