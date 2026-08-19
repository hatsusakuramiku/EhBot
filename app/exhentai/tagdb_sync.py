from __future__ import annotations

import gzip
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import httpx


LOGGER = logging.getLogger(__name__)

RELEASE_API = (
    "https://api.github.com/repos/EhTagTranslation/Database/releases/latest"
)
ASSET_NAME = "db.text.json.gz"
CACHE_FILE_NAME = "ehtag_db.json"
META_FILE_NAME = "ehtag_db.meta.json"

# The published database is a few MB gzipped; refuse anything absurd.
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024

# Upstream publishes at most a few releases per week, so a daily check is
# plenty and keeps restarts from hammering GitHub.
MIN_REFRESH_INTERVAL_SECONDS = 24 * 60 * 60


class TagDatabaseError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message


@dataclass(frozen=True, slots=True)
class SyncResult:
    """Outcome of one synchronisation attempt."""

    updated: bool
    from_cache: bool
    version: str | None
    entry_count: int
    reason: str


def _count_entries(payload: dict) -> int:
    total = 0
    for namespace in payload.get("data") or ():
        if isinstance(namespace, dict):
            total += len(namespace.get("data") or {})
    return total


class TagDatabaseSync:
    """Download and cache the EhTagTranslation database.

    Uses the GitHub release API to locate the current asset, then a
    conditional request so unchanged data is not re-downloaded. Any network
    failure degrades to the on-disk cache so ingestion keeps working offline.
    """

    def __init__(self, data_path: Path, client: httpx.AsyncClient) -> None:
        self._cache_path = data_path / CACHE_FILE_NAME
        self._meta_path = data_path / META_FILE_NAME
        self._client = client

    @property
    def cache_path(self) -> Path:
        return self._cache_path

    def load_cached(self) -> dict | None:
        """Return the cached database, or None when it is absent/corrupt."""
        if not self._cache_path.is_file():
            return None
        try:
            with self._cache_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            LOGGER.warning(
                "ehtag_cache_unreadable",
                extra={"error_code": "EHTAG_CACHE_UNREADABLE"},
            )
            return None
        return payload if isinstance(payload, dict) else None

    def _load_meta(self) -> dict:
        if not self._meta_path.is_file():
            return {}
        try:
            with self._meta_path.open("r", encoding="utf-8") as handle:
                meta = json.load(handle)
        except (OSError, ValueError):
            return {}
        return meta if isinstance(meta, dict) else {}

    def _store(self, payload: dict, etag: str | None) -> None:
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._cache_path.with_suffix(".part")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(
                payload, handle, ensure_ascii=False, separators=(",", ":")
            )
        temporary.replace(self._cache_path)
        meta = {
            "etag": etag,
            "version": str(payload.get("version") or ""),
            "checked_at": time.time(),
        }
        meta_temporary = self._meta_path.with_suffix(".part")
        with meta_temporary.open("w", encoding="utf-8") as handle:
            json.dump(meta, handle, separators=(",", ":"))
        meta_temporary.replace(self._meta_path)

    def _touch_checked_at(self, meta: dict) -> None:
        """Record a successful remote check without rewriting the database."""
        meta = dict(meta)
        meta["checked_at"] = time.time()
        try:
            self._meta_path.parent.mkdir(parents=True, exist_ok=True)
            with self._meta_path.open("w", encoding="utf-8") as handle:
                json.dump(meta, handle, separators=(",", ":"))
        except OSError:
            LOGGER.warning(
                "ehtag_meta_unwritable",
                extra={"error_code": "EHTAG_META_UNWRITABLE"},
            )

    def _is_fresh(self, meta: dict) -> bool:
        checked_at = meta.get("checked_at")
        if not isinstance(checked_at, (int, float)):
            return False
        age = time.time() - float(checked_at)
        return 0 <= age < MIN_REFRESH_INTERVAL_SECONDS

    def _cached_result(self, cached: dict, reason: str) -> SyncResult:
        return SyncResult(
            updated=False,
            from_cache=True,
            version=str(cached.get("version") or "") or None,
            entry_count=_count_entries(cached),
            reason=reason,
        )

    async def _resolve_asset_url(self) -> str:
        response = await self._client.get(
            RELEASE_API,
            headers={"Accept": "application/vnd.github+json"},
            follow_redirects=True,
        )
        if response.status_code != 200:
            raise TagDatabaseError(
                "EHTAG_RELEASE_HTTP",
                f"标签库发布信息返回 HTTP {response.status_code}",
            )
        try:
            release = response.json()
        except ValueError as exc:
            raise TagDatabaseError(
                "EHTAG_RELEASE_INVALID", "标签库发布信息无法解析"
            ) from exc
        for asset in release.get("assets") or ():
            if isinstance(asset, dict) and asset.get("name") == ASSET_NAME:
                url = str(asset.get("browser_download_url") or "")
                if url:
                    return url
        raise TagDatabaseError(
            "EHTAG_ASSET_MISSING", f"标签库发布中缺少 {ASSET_NAME}"
        )

    async def synchronize(self, *, force: bool = False) -> SyncResult:
        """Refresh the cache when the remote asset changed.

        Network problems degrade to the on-disk cache instead of raising, so
        an unreachable GitHub is never fatal once a cache exists.
        """
        cached = self.load_cached()
        meta = self._load_meta()
        if cached is not None and not force and self._is_fresh(meta):
            return self._cached_result(cached, "cache_fresh")
        try:
            asset_url = await self._resolve_asset_url()
            headers: dict[str, str] = {}
            known_etag = meta.get("etag")
            if cached is not None and known_etag and not force:
                headers["If-None-Match"] = str(known_etag)
            response = await self._client.get(
                asset_url, headers=headers, follow_redirects=True
            )
        except (httpx.HTTPError, TagDatabaseError) as exc:
            LOGGER.warning(
                "ehtag_sync_failed",
                extra={
                    "error_code": getattr(exc, "code", "EHTAG_UNREACHABLE")
                },
            )
            if cached is None:
                raise TagDatabaseError(
                    "EHTAG_UNAVAILABLE",
                    "无法获取标签翻译数据库，且本地没有缓存",
                ) from exc
            return self._cached_result(cached, "network_failed_using_cache")

        if response.status_code == 304 and cached is not None:
            self._touch_checked_at(meta)
            return self._cached_result(cached, "not_modified")
        if response.status_code != 200:
            if cached is None:
                raise TagDatabaseError(
                    "EHTAG_DOWNLOAD_HTTP",
                    f"标签库下载返回 HTTP {response.status_code}",
                )
            return self._cached_result(cached, "download_failed_using_cache")

        body = response.content
        if len(body) > MAX_DOWNLOAD_BYTES:
            raise TagDatabaseError(
                "EHTAG_TOO_LARGE", "标签库文件超出允许的大小"
            )
        try:
            payload = json.loads(gzip.decompress(body).decode("utf-8"))
        except (OSError, ValueError, EOFError) as exc:
            if cached is not None:
                LOGGER.warning(
                    "ehtag_payload_invalid",
                    extra={"error_code": "EHTAG_PAYLOAD_INVALID"},
                )
                return self._cached_result(
                    cached, "payload_invalid_using_cache"
                )
            raise TagDatabaseError(
                "EHTAG_PAYLOAD_INVALID", "标签库内容无法解析"
            ) from exc
        if not isinstance(payload, dict) or not payload.get("data"):
            raise TagDatabaseError(
                "EHTAG_PAYLOAD_INVALID", "标签库内容缺少 data 字段"
            )
        self._store(payload, response.headers.get("etag"))
        return SyncResult(
            updated=True,
            from_cache=False,
            version=str(payload.get("version") or "") or None,
            entry_count=_count_entries(payload),
            reason="downloaded",
        )


__all__ = [
    "ASSET_NAME",
    "CACHE_FILE_NAME",
    "META_FILE_NAME",
    "MIN_REFRESH_INTERVAL_SECONDS",
    "RELEASE_API",
    "SyncResult",
    "TagDatabaseError",
    "TagDatabaseSync",
]
