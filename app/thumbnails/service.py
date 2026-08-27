"""Thumbnail proxy service: hashing, cache lookup, on-demand fetch.

The service is content-addressed: the hash is a SHA-256 digest of the source
identity (URL + variant), so the URL is derivable before the image has been
fetched, and ``immutable`` cache headers are truthful.

In-flight requests are deduplicated per hash so a 50-cover first paint does
not stampede upstream. A global semaphore (``PROXY_CONCURRENCY``) limits
concurrent outbound fetches.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable

import httpx

from app.thumbnails import (
    MAX_INBOUND_BYTES,
    PROXY_CONCURRENCY,
    THUMBNAIL_KIND_CANDIDATE_COVER,
    THUMBNAIL_STATE_FAILED,
    THUMBNAIL_STATE_PENDING,
    THUMBNAIL_STATE_READY,
    THUMBNAIL_VARIANT_CARD,
)
from app.thumbnails.errors import ThumbnailError
from app.thumbnails.identity import disk_path, identity_hash
from app.thumbnails.models import ThumbnailResult, ThumbnailRow
from app.thumbnails.render import render_card

logger = logging.getLogger(__name__)


def _map_row(row: tuple) -> ThumbnailRow:
    """Map a sqlite3 row tuple to a ``ThumbnailRow``."""
    return ThumbnailRow(
        hash=str(row[0]),
        kind=str(row[1]),
        variant=str(row[2]),
        source_url=str(row[3]) if row[3] is not None else None,
        source_path=str(row[4]) if row[4] is not None else None,
        state=str(row[5]),
        content_type=str(row[6]) if row[6] is not None else None,
        byte_size=int(row[7]) if row[7] is not None else None,
        width=int(row[8]) if row[8] is not None else None,
        height=int(row[9]) if row[9] is not None else None,
        error_code=str(row[10]) if row[10] is not None else None,
        attempt_count=int(row[11]),
    )


_SELECT_ROW = (
    "SELECT hash, kind, variant, source_url, source_path, "
    "state, content_type, byte_size, width, height, "
    "error_code, attempt_count FROM thumbnails WHERE hash = ?"
)


class ThumbnailService:
    """Orchestrates thumbnail cache lookup, fetch and storage.

    Thread safety
    -------------
    This service is shared across requests. The in-flight dedup dict is
    protected by ``asyncio`` primitives (``Event``, ``Semaphore``) and is
    not touched from worker threads. The database methods are synchronous
    and run via ``asyncio.to_thread``.
    """

    def __init__(
        self,
        database: Any,
        thumbnail_dir: Path,
        http_client: httpx.AsyncClient,
        image_url_checker: Callable[[str], str] | None = None,
    ) -> None:
        self._database = database
        self._thumbnail_dir = thumbnail_dir
        self._http_client = http_client
        self._image_url_checker = image_url_checker

        #: In-flight fetch dedup: ``hash -> asyncio.Event``.
        self._pending_fetches: dict[str, asyncio.Event] = {}

        #: Global concurrency limiter for outbound fetches.
        self._fetch_semaphore = asyncio.Semaphore(PROXY_CONCURRENCY)

        self._thumbnail_dir.mkdir(parents=True, exist_ok=True)

    async def get_or_create(
        self,
        hash_str: str,
        *,
        source_url: str | None = None,
        kind: str = THUMBNAIL_KIND_CANDIDATE_COVER,
        variant: str = THUMBNAIL_VARIANT_CARD,
    ) -> ThumbnailResult:
        """Return the thumbnail result for *hash_str*, fetching if needed.

        Parameters
        ----------
        hash_str
            The 64-char SHA-256 hex digest from ``identity_hash``.
        source_url
            Required when the row does not yet exist in the database. Ignored
            (and may be ``None``) when the row already exists.
        kind, variant
            Used when creating a new row. Ignored for existing rows.

        Returns
        -------
        ThumbnailResult
            The file is on disk when ``state == READY``, or a placeholder
            response with ``state == FAILED`` / ``PENDING``.
        """
        row = await self._row_by_hash(hash_str)

        if row is None:
            if source_url is None:
                return ThumbnailResult(
                    file_path=None,
                    content_type="image/webp",
                    byte_size=None,
                    width=None,
                    height=None,
                    state=THUMBNAIL_STATE_FAILED,
                    error_code="NO_SOURCE",
                )
            row = await self._create_pending(
                hash_str, kind, variant, source_url
            )

        if row.state == THUMBNAIL_STATE_READY:
            result = await self._serve_from_disk(row)
            if result is not None:
                return result
            row = await self._mark_pending(row.hash)

        if row.state == THUMBNAIL_STATE_PENDING:
            await self._ensure_fetch(row)
            row = await self._row_by_hash(hash_str)
            if row is not None and row.state == THUMBNAIL_STATE_READY:
                result = await self._serve_from_disk(row)
                if result is not None:
                    return result

        return ThumbnailResult(
            file_path=None,
            content_type="image/webp",
            byte_size=None,
            width=None,
            height=None,
            state=row.state if row is not None else THUMBNAIL_STATE_FAILED,
            error_code=row.error_code if row is not None else "UNKNOWN",
        )

    # ------------------------------------------------------------------
    # Database helpers
    # ------------------------------------------------------------------

    async def _row_by_hash(self, hash_str: str) -> ThumbnailRow | None:
        return await asyncio.to_thread(
            self._row_by_hash_sync, hash_str
        )

    def _row_by_hash_sync(self, hash_str: str) -> ThumbnailRow | None:
        with self._database._connect() as connection:
            row = connection.execute(
                _SELECT_ROW, (hash_str,)
            ).fetchone()
        return _map_row(row) if row is not None else None

    async def _create_pending(
        self, hash_str: str, kind: str, variant: str, source_url: str
    ) -> ThumbnailRow:
        return await asyncio.to_thread(
            self._create_pending_sync, hash_str, kind, variant, source_url
        )

    def _create_pending_sync(
        self, hash_str: str, kind: str, variant: str, source_url: str
    ) -> ThumbnailRow:
        with self._database._connect() as connection:
            connection.execute(
                "INSERT INTO thumbnails "
                "(hash, kind, variant, source_url, state) "
                "VALUES (?, ?, ?, ?, 'PENDING') "
                "ON CONFLICT(hash) DO NOTHING",
                (hash_str, kind, variant, source_url),
            )
            row = connection.execute(
                _SELECT_ROW, (hash_str,)
            ).fetchone()
        return _map_row(row)

    async def _mark_ready(
        self,
        hash_str: str,
        content_type: str,
        byte_size: int,
        width: int,
        height: int,
    ) -> None:
        await asyncio.to_thread(
            self._mark_ready_sync,
            hash_str,
            content_type,
            byte_size,
            width,
            height,
        )

    def _mark_ready_sync(
        self,
        hash_str: str,
        content_type: str,
        byte_size: int,
        width: int,
        height: int,
    ) -> None:
        with self._database._connect() as connection:
            connection.execute(
                "UPDATE thumbnails SET state = 'READY', "
                "content_type = ?, byte_size = ?, width = ?, height = ?, "
                "error_code = NULL, attempt_count = attempt_count + 1, "
                "fetched_at = CURRENT_TIMESTAMP, "
                "updated_at = CURRENT_TIMESTAMP "
                "WHERE hash = ?",
                (content_type, byte_size, width, height, hash_str),
            )

    async def _mark_failed(
        self, hash_str: str, error_code: str
    ) -> None:
        await asyncio.to_thread(
            self._mark_failed_sync, hash_str, error_code
        )

    def _mark_failed_sync(
        self, hash_str: str, error_code: str
    ) -> None:
        with self._database._connect() as connection:
            connection.execute(
                "UPDATE thumbnails SET state = 'FAILED', "
                "error_code = ?, attempt_count = attempt_count + 1, "
                "updated_at = CURRENT_TIMESTAMP "
                "WHERE hash = ?",
                (error_code, hash_str),
            )

    async def _mark_pending(self, hash_str: str) -> ThumbnailRow:
        return await asyncio.to_thread(
            self._mark_pending_sync, hash_str
        )

    def _mark_pending_sync(self, hash_str: str) -> ThumbnailRow:
        with self._database._connect() as connection:
            connection.execute(
                "UPDATE thumbnails SET state = 'PENDING', "
                "updated_at = CURRENT_TIMESTAMP "
                "WHERE hash = ?",
                (hash_str,),
            )
            row = connection.execute(
                _SELECT_ROW, (hash_str,)
            ).fetchone()
        return _map_row(row)

    # ------------------------------------------------------------------
    # Disk
    # ------------------------------------------------------------------

    async def _serve_from_disk(
        self, row: ThumbnailRow
    ) -> ThumbnailResult | None:
        """Return a ``ThumbnailResult`` if the file is on disk, else ``None``."""
        path = disk_path(self._thumbnail_dir, row.hash)
        if not path.exists() or not path.is_file():
            return None
        return ThumbnailResult(
            file_path=str(path),
            content_type=row.content_type or "image/webp",
            byte_size=path.stat().st_size,
            width=row.width,
            height=row.height,
            state=THUMBNAIL_STATE_READY,
            error_code=None,
        )

    # ------------------------------------------------------------------
    # Fetch orchestration
    # ------------------------------------------------------------------

    async def _ensure_fetch(self, row: ThumbnailRow) -> None:
        """Wait for an in-flight fetch or start one."""
        if row.hash in self._pending_fetches:
            event = self._pending_fetches[row.hash]
            await event.wait()
            return

        event = asyncio.Event()
        self._pending_fetches[row.hash] = event
        try:
            await self._fetch_and_store(row)
        finally:
            self._pending_fetches.pop(row.hash, None)
            event.set()

    async def _fetch_and_store(self, row: ThumbnailRow) -> None:
        """Fetch the upstream image, render, write to disk, update DB."""
        if row.source_url is None:
            await self._mark_failed(row.hash, "NO_SOURCE")
            return

        # SSRF gate — reuse the telegraph guard.
        if self._image_url_checker is not None:
            try:
                self._image_url_checker(row.source_url)
            except Exception as exc:
                code = getattr(exc, "code", "URL_BLOCKED")
                logger.warning(
                    "thumbnail_url_blocked",
                    extra={"url": row.source_url, "code": code},
                )
                await self._mark_failed(row.hash, code)
                return

        async with self._fetch_semaphore:
            try:
                data = await self._fetch_bytes(row.source_url)
            except ThumbnailError as exc:
                await self._mark_failed(row.hash, exc.code)
                return
            except Exception as exc:
                logger.warning(
                    "thumbnail_fetch_exception",
                    extra={"error": str(exc)},
                )
                await self._mark_failed(row.hash, "FETCH_FAILED")
                return

        try:
            webp_bytes, content_type, width, height = render_card(data)
        except ThumbnailError as exc:
            await self._mark_failed(row.hash, exc.code)
            return

        path = disk_path(self._thumbnail_dir, row.hash, mkdir=True)
        try:
            path.write_bytes(webp_bytes)
        except OSError as exc:
            logger.error(
                "thumbnail_write_failed",
                extra={"path": str(path), "error": str(exc)},
            )
            await self._mark_failed(row.hash, "DISK_WRITE_FAILED")
            return

        await self._mark_ready(
            row.hash, content_type, len(webp_bytes), width, height
        )

    async def _fetch_bytes(self, url: str) -> bytes:
        """Fetch up to ``MAX_INBOUND_BYTES`` from *url*.

        Two-stage hotlink handling: first request without ``Referer``, retry
        with one if the server returns 4xx.
        """
        for attempt, headers in enumerate(
            [
                {},
                {"Referer": "https://exhentai.org/"},
            ]
        ):
            try:
                response = await self._http_client.get(
                    url,
                    headers=headers,
                    follow_redirects=True,
                    timeout=30.0,
                )
                response.raise_for_status()
                content = response.content
                if len(content) > MAX_INBOUND_BYTES:
                    raise ThumbnailError(
                        "INBOUND_TOO_LARGE",
                        "上游图片体积超过限制",
                    )
                return content
            except httpx.HTTPStatusError as exc:
                if attempt == 0 and exc.response.status_code in (403, 404):
                    continue
                raise ThumbnailError(
                    "UPSTREAM_ERROR",
                    f"上游返回 {exc.response.status_code}",
                ) from exc
            except httpx.TimeoutException as exc:
                raise ThumbnailError(
                    "UPSTREAM_TIMEOUT", "上游连接超时"
                ) from exc
            except httpx.RequestError as exc:
                raise ThumbnailError(
                    "UPSTREAM_NETWORK_ERROR", "上游网络错误"
                ) from exc

        raise ThumbnailError(
            "UPSTREAM_ERROR",
            "两次尝试后上游仍返回错误",
        )


__all__ = [
    "ThumbnailService",
    "disk_path",
    "identity_hash",
]