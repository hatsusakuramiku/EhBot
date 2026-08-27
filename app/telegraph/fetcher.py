"""Bounded, guarded image retrieval for a Telegraph preview page.

Every limit here exists because the page content is untrusted: an attacker who
can publish a page can otherwise aim this fetcher at internal addresses, at an
endless stream of bytes, or at a payload that is not an image at all.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import PurePosixPath

import httpx

from app.archive.safety import (
    IMAGE_EXTENSIONS,
    header_matches_extension,
    looks_like_image,
)
from app.telegraph.client import PAGE_BASE, USER_AGENT
from app.telegraph.guard import MAX_REDIRECTS, check_image_url
from app.telegraph.models import FetchedImage, TelegraphError


@dataclass(frozen=True, slots=True)
class FetchLimits:
    concurrency: int = 3
    max_images: int = 400
    max_image_bytes: int = 20 * 1024 * 1024
    max_total_bytes: int = 1024 * 1024 * 1024
    timeout_seconds: float = 600.0
    attempts_per_image: int = 2


#: Content types the image hosts actually serve. SVG is absent deliberately:
#: it is a scripting surface, not a comic page.
_EXTENSION_BY_CONTENT_TYPE: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/avif": ".avif",
    "image/jxl": ".jxl",
}

def image_extension(url: str, content_type: str | None) -> str:
    """Pick a page suffix, preferring the served type over the URL.

    Channel image proxies serve WebP from paths with no extension at all, so
    the URL alone cannot be trusted to name the format.
    """
    if content_type:
        mapped = _EXTENSION_BY_CONTENT_TYPE.get(
            content_type.split(";")[0].strip().lower()
        )
        if mapped:
            return mapped
    suffix = PurePosixPath(url.split("?")[0]).suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return ".jpg" if suffix == ".jpeg" else suffix
    return ".jpg"


class TelegraphFetcher:
    def __init__(
        self,
        client: httpx.AsyncClient,
        limits: FetchLimits | None = None,
        *,
        resolver=None,
    ) -> None:
        self._client = client
        self._limits = limits or FetchLimits()
        self._resolver = resolver

    def _guard(self, url: str) -> str:
        if self._resolver is None:
            return check_image_url(url)
        return check_image_url(url, resolver=self._resolver)

    async def fetch_all(
        self, page_url: str, image_urls: tuple[str, ...]
    ) -> tuple[FetchedImage, ...]:
        limits = self._limits
        if not image_urls:
            raise TelegraphError(
                "TELEGRAPH_NO_IMAGES", "预览页没有可下载的图片"
            )
        if len(image_urls) > limits.max_images:
            raise TelegraphError(
                "TELEGRAPH_LIMIT_EXCEEDED",
                f"预览页有 {len(image_urls)} 张图片，超过上限 "
                f"{limits.max_images}",
            )
        for url in image_urls:
            self._guard(url)

        semaphore = asyncio.Semaphore(max(1, limits.concurrency))
        results: list[FetchedImage | None] = [None] * len(image_urls)

        async def worker(index: int, url: str) -> None:
            async with semaphore:
                results[index] = await self._fetch_one(index, url, page_url)

        try:
            async with asyncio.timeout(limits.timeout_seconds):
                await asyncio.gather(
                    *(
                        worker(index, url)
                        for index, url in enumerate(image_urls)
                    )
                )
        except TimeoutError as exc:
            raise TelegraphError(
                "TELEGRAPH_LIMIT_EXCEEDED",
                f"预览页抓取超过 {limits.timeout_seconds:.0f} 秒上限",
            ) from exc

        total = 0
        images: list[FetchedImage] = []
        for item in results:
            if item is None:
                raise TelegraphError(
                    "TELEGRAPH_IMAGE_FAILED", "有图片未能抓取"
                )
            total += len(item.data)
            if total > limits.max_total_bytes:
                raise TelegraphError(
                    "TELEGRAPH_LIMIT_EXCEEDED",
                    "预览页总字节数超过上限",
                )
            images.append(item)
        return tuple(images)

    async def _fetch_one(
        self, index: int, url: str, page_url: str
    ) -> FetchedImage:
        """Fetch one page image, retrying with a referer on a hotlink refusal.

        The first attempt carries no referer so a host that does not care is
        not told where the request came from; the second adds the Telegraph
        referer that the sampled hosts require. Redirects are followed by hand
        so each hop is re-checked against the address guard.
        """
        limits = self._limits
        last_error: TelegraphError | None = None
        for attempt in range(max(1, limits.attempts_per_image)):
            headers = {"User-Agent": USER_AGENT}
            if attempt:
                headers["Referer"] = f"{PAGE_BASE}/"
            try:
                response = await self._request_with_redirects(url, headers)
            except TelegraphError as exc:
                if exc.code == "TELEGRAPH_IMAGE_BLOCKED":
                    raise
                last_error = exc
                continue
            content_type = response.headers.get("content-type")
            data = response.content
            if len(data) > limits.max_image_bytes:
                raise TelegraphError(
                    "TELEGRAPH_LIMIT_EXCEEDED",
                    f"第 {index + 1} 张图片超过单图上限",
                )
            if not data:
                last_error = TelegraphError(
                    "TELEGRAPH_IMAGE_FAILED",
                    f"第 {index + 1} 张图片是空文件",
                )
                continue
            if not looks_like_image(data):
                # A wrong body is a decision, not a transient fault: retrying
                # a host that serves HTML will only serve HTML again.
                raise TelegraphError(
                    "TELEGRAPH_IMAGE_BLOCKED",
                    f"第 {index + 1} 张图片不是受支持的图片格式",
                )
            suffix = image_extension(url, content_type)
            name = f"{index + 1:04d}{suffix}"
            if not header_matches_extension(name, data[:16]):
                suffix = image_extension(url, None)
                name = f"{index + 1:04d}{suffix}"
            return FetchedImage(name=name, data=data, source_url=url)
        raise last_error or TelegraphError(
            "TELEGRAPH_IMAGE_FAILED",
            f"第 {index + 1} 张图片抓取失败",
        )

    async def _request_with_redirects(
        self, url: str, headers: dict[str, str]
    ) -> httpx.Response:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            try:
                response = await self._client.get(
                    current, headers=headers, follow_redirects=False
                )
            except httpx.HTTPError as exc:
                raise TelegraphError(
                    "TELEGRAPH_IMAGE_FAILED",
                    f"图片请求失败: {current}",
                ) from exc
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location", "")
                if not location:
                    raise TelegraphError(
                        "TELEGRAPH_IMAGE_FAILED",
                        f"图片重定向缺少目标: {current}",
                    )
                current = self._guard(
                    httpx.URL(current).join(location).__str__()
                )
                continue
            if response.status_code != 200:
                raise TelegraphError(
                    "TELEGRAPH_IMAGE_FAILED",
                    f"图片返回 HTTP {response.status_code}",
                )
            return response
        raise TelegraphError(
            "TELEGRAPH_IMAGE_FAILED",
            f"图片重定向超过 {MAX_REDIRECTS} 跳",
        )


__all__ = ["FetchLimits", "TelegraphFetcher", "image_extension"]