from __future__ import annotations

import asyncio

import httpx

from app.exhentai.gdata import (
    GalleryData,
    parse_gdata_entry,
)


GDATA_ENDPOINT = "https://api.e-hentai.org/api.php"

# The gdata API accepts at most 25 galleries per request.
MAX_GALLERIES_PER_REQUEST = 25

# E-Hentai asks clients to pace bursts; wait between batches.
BATCH_PAUSE_SECONDS = 1.0


class GdataError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message


class GdataClient:
    """Fetch structured gallery metadata from the public gdata API.

    The endpoint needs no Cookie, so it works even when ExHentai access is
    unavailable. Callers fall back to HTML scraping for expunged galleries.
    """

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def fetch_many(
        self, refs: list[tuple[int, str]]
    ) -> dict[int, GalleryData]:
        results: dict[int, GalleryData] = {}
        batches = [
            refs[index : index + MAX_GALLERIES_PER_REQUEST]
            for index in range(0, len(refs), MAX_GALLERIES_PER_REQUEST)
        ]
        for position, batch in enumerate(batches):
            if position:
                await asyncio.sleep(BATCH_PAUSE_SECONDS)
            for gallery in await self._fetch_batch(batch):
                results[gallery.gid] = gallery
        return results

    async def fetch_one(self, gid: int, token: str) -> GalleryData:
        galleries = await self._fetch_batch([(gid, token)])
        for gallery in galleries:
            if gallery.gid == gid:
                return gallery
        raise GdataError(
            "EXHENTAI_GDATA_NOT_FOUND",
            "gdata 未返回该画廊的元数据，可能已被删除",
        )

    async def _fetch_batch(
        self, batch: list[tuple[int, str]]
    ) -> list[GalleryData]:
        if not batch:
            return []
        payload = {
            "method": "gdata",
            "gidlist": [[int(gid), str(token)] for gid, token in batch],
            "namespace": 1,
        }
        try:
            response = await self._client.post(
                GDATA_ENDPOINT, json=payload
            )
        except httpx.HTTPError as exc:
            raise GdataError(
                "EXHENTAI_GDATA_UNREACHABLE",
                "无法连接 E-Hentai 元数据接口",
            ) from exc
        if response.status_code == 429:
            raise GdataError(
                "EXHENTAI_GDATA_RATE_LIMITED",
                "E-Hentai 元数据接口触发限流，请稍后重试",
            )
        if response.status_code != 200:
            raise GdataError(
                "EXHENTAI_GDATA_HTTP",
                f"元数据接口返回 HTTP {response.status_code}",
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise GdataError(
                "EXHENTAI_GDATA_INVALID",
                "元数据接口返回了无法解析的内容",
            ) from exc
        if not isinstance(body, dict):
            raise GdataError(
                "EXHENTAI_GDATA_INVALID",
                "元数据接口返回了无法解析的内容",
            )
        if body.get("error"):
            raise GdataError(
                "EXHENTAI_GDATA_REJECTED",
                f"元数据接口拒绝了请求: {body['error']}",
            )
        entries = body.get("gmetadata")
        if not isinstance(entries, list):
            raise GdataError(
                "EXHENTAI_GDATA_INVALID",
                "元数据接口未返回 gmetadata",
            )
        galleries: list[GalleryData] = []
        for entry in entries:
            gallery = parse_gdata_entry(entry)
            if gallery is not None:
                galleries.append(gallery)
        return galleries


__all__ = [
    "BATCH_PAUSE_SECONDS",
    "GDATA_ENDPOINT",
    "GdataClient",
    "GdataError",
    "MAX_GALLERIES_PER_REQUEST",
]
