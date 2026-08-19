from __future__ import annotations

import re
from pathlib import Path

import httpx

from app.connections.exhentai import ExHentaiCredentials
from app.exhentai.metadata import (
    merge_metadata,
    parse_gallery_html,
)


class ExHentaiDownloadError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message


class ExHentaiDownloader:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    def _cookie_header(self, credentials: ExHentaiCredentials) -> str:
        cookie = SimpleCookie()
        for name, value in credentials.as_cookies().items():
            cookie[name] = value
        return cookie.output(header="", sep=";").strip()

    async def fetch_metadata(
        self,
        credentials: ExHentaiCredentials,
        gid: int,
        token: str,
    ) -> dict:
        url = f"https://exhentai.org/g/{int(gid)}/{token}/"
        cookie_header = self._cookie_header(credentials)
        try:
            response = await self._client.get(
                url, headers={"Cookie": cookie_header}
            )
        except httpx.HTTPError as exc:
            raise ExHentaiDownloadError(
                "EXHENTAI_UNREACHABLE",
                "无法连接 ExHentai 获取元数据",
            ) from exc
        if response.status_code != 200:
            raise ExHentaiDownloadError(
                "EXHENTAI_METADATA_HTTP",
                f"画廊页面返回 HTTP {response.status_code}",
            )
        if "ExHentai" not in response.text:
            raise ExHentaiDownloadError(
                "EXHENTAI_METADATA_AUTH",
                "ExHentai Cookie 已失效，无法访问画廊",
            )
        parsed = parse_gallery_html(response.text)
        if parsed is None:
            raise ExHentaiDownloadError(
                "EXHENTAI_METADATA_PARSE",
                "无法解析画廊页面，请稍后重试",
            )
        merged = merge_metadata(parsed)
        return {k: v for k, v in merged.items() if v is not None}

    async def request_archive_url(
        self,
        credentials: ExHentaiCredentials,
        gid: int,
        token: str,
    ) -> str:
        url = f"https://exhentai.org/g/{int(gid)}/{token}/"
        body = "dl=yes&p_p=0"
        cookie_header = self._cookie_header(credentials)
        try:
            response = await self._client.post(
                url,
                headers={
                    "Cookie": cookie_header,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                content=body,
            )
        except httpx.HTTPError as exc:
            raise ExHentaiDownloadError(
                "EXHENTAI_UNREACHABLE",
                "无法连接 ExHentai 申请原档",
            ) from exc
        if response.status_code != 200:
            raise ExHentaiDownloadError(
                "EXHENTAI_ARCHIVE_HTTP",
                f"原档申请返回 HTTP {response.status_code}",
            )
        link = re.search(
            r"<a[^>]+href=\"([^\"]+)\"[^>]*>(?:Download|Archive|Original)",
            response.text,
            flags=re.IGNORECASE,
        )
        if not link:
            raise ExHentaiDownloadError(
                "EXHENTAI_ARCHIVE_LINK",
                "画廊未提供原档下载链接",
            )
        return link.group(1)

    async def download_archive(
        self,
        credentials: ExHentaiCredentials,
        archive_url: str,
        destination: Path,
    ) -> int:
        cookie_header = self._cookie_header(credentials)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with self._client.stream(
                "GET",
                archive_url,
                headers={"Cookie": cookie_header},
                follow_redirects=True,
            ) as response:
                response.raise_for_status()
                with destination.open("wb") as target:
                    copied = 0
                    async for chunk in response.aiter_bytes(
                        chunk_size=64 * 1024
                    ):
                        if not chunk:
                            continue
                        target.write(chunk)
                        copied += len(chunk)
            return copied
        except (httpx.HTTPError, OSError) as exc:
            destination.unlink(missing_ok=True)
            raise ExHentaiDownloadError(
                "EXHENTAI_ARCHIVE_DOWNLOAD",
                f"原档下载失败: {exc}",
            ) from exc


# Re-export SimpleCookie so the imports above stay short
from http.cookies import SimpleCookie  # noqa: E402


__all__ = ["ExHentaiDownloader", "ExHentaiDownloadError"]