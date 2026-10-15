from __future__ import annotations

import html
import re
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx

from app.archive.formats import ZIP_SIGNATURES
from app.connections.exhentai import ExHentaiCredentials
from app.exhentai.errors import ExHentaiDownloadError, response_limit, credential_rejected, has_gallery_heading
from app.exhentai.metadata import (
    merge_metadata,
    parse_gallery_html,
)


#: Every anchor on a page, captured as href + label. The label alone cannot
#: identify the archive button: a gallery links each of its tags, and
#: `<a href="https://exhentai.org/?f_search=parody%3Aoriginal">original</a>` --
#: present on nearly every doujinshi -- reads exactly like an「Original」archive
#: button. Matching it downloads a search-results page, which is how a stray
#: HTML document ends up stored as `gallery-<gid>.zip`.
_ANCHOR_PATTERN = re.compile(
    r"<a\s[^>]*href=\"(?P<href>[^\"]+)\"[^>]*>(?P<label>[^<]*)</a>",
    re.IGNORECASE,
)

#: Relative hrefs are resolved against the site the request went to.
_SITE_ORIGIN = "https://exhentai.org/"

#: Href fragments that mark the archiver endpoint itself.
_ARCHIVE_HREF_MARKERS: tuple[str, ...] = ("archiver.php", "archiver?", "/archive/")

#: Href fragments that are definitely not an archive: tags and site navigation.
_NOT_ARCHIVE_HREF_MARKERS: tuple[str, ...] = (
    "f_search=",
    "/tag/",
    "/favorites",
    "/watched",
    "/popular",
    "/torrents",
    "/settings",
    "/u/",
)

#: Labels E-Hentai has put on the archive button over the years.
_ARCHIVE_LABELS: frozenset[str] = frozenset(
    {"download", "archive", "archive download", "original", "original archive"}
)


def _find_archive_link(document: str) -> str | None:
    """The archiver URL in the page ExHentai answered `dl=yes` with, or None.

    Href first, label second: an href on the archiver endpoint is unambiguous,
    while a label like "original" is also what the `parody:original` tag renders
    as. Returning None is the correct answer when neither is present -- the
    caller turns that into `EXHENTAI_ARCHIVE_LINK` instead of downloading
    whatever page happened to be in the document.

    The href is HTML-unescaped before it is returned: archive URLs arrive as
    `...?gid=..&amp;token=..&amp;or=..` in the markup, and requesting them with
    the entities intact garbles every parameter after the first `&` -- which is
    exactly how a request for an archive turns into a request for something
    else.
    """
    labelled: str | None = None
    for match in _ANCHOR_PATTERN.finditer(document):
        raw = html.unescape(match.group("href")).strip()
        if not raw or raw.startswith("#"):
            continue
        href = urljoin(_SITE_ORIGIN, raw)
        if not href.startswith(("http://", "https://")):
            continue
        lowered = href.lower()
        if any(marker in lowered for marker in _NOT_ARCHIVE_HREF_MARKERS):
            continue
        if any(marker in lowered for marker in _ARCHIVE_HREF_MARKERS):
            return href
        if labelled is None:
            label = " ".join(match.group("label").split()).lower()
            if label in _ARCHIVE_LABELS:
                labelled = href
    return labelled


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
                url, headers={"Cookie": cookie_header}, follow_redirects=False
            )
        except httpx.HTTPError as exc:
            raise ExHentaiDownloadError(
                "EXHENTAI_UNREACHABLE",
                "无法连接 ExHentai 获取元数据",
                failure_scope="site", retry_after=60,
            ) from exc
        limited = response_limit(response)
        if limited is not None:
            raise limited
        if credential_rejected(response):
            raise ExHentaiDownloadError(
                "EXHENTAI_METADATA_AUTH", "ExHentai Cookie 已失效，请重新验证或修改配置",
                failure_scope="credential",
            )
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
        # A generic error page's <title> must never become the gallery title.
        parsed = parse_gallery_html(response.text) if has_gallery_heading(response.text) else None
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
        cookie_header = self._cookie_header(credentials)
        try:
            response = await self._client.get(
                url,
                headers={"Cookie": cookie_header},
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise ExHentaiDownloadError(
                "EXHENTAI_UNREACHABLE",
                "无法连接 ExHentai 申请原档",
                failure_scope="site", retry_after=60,
            ) from exc
        # This is only discovery; the paid form belongs to archiver.php.
        from app.exhentai.archive import archive_response_error
        error = archive_response_error(response)
        if error is not None:
            raise error
        link = _find_archive_link(response.text)
        parsed = urlsplit(link or "")
        query = parse_qs(parsed.query)
        if (parsed.scheme != "https" or parsed.hostname not in {"exhentai.org", "e-hentai.org"}
                or parsed.username is not None or parsed.port not in {None, 443}
                or parsed.path != "/archiver.php" or query.get("gid") != [str(gid)] or query.get("token") != [token]):
            raise ExHentaiDownloadError(
                "EXHENTAI_ARCHIVE_LINK",
                "画廊未提供原档下载链接（原档可能尚未生成，或页面结构有变），"
                "请稍后重试",
            )
        return link

    async def archive_page(self, credentials, url, *, submit=False):
        """Read a provider page; True submits the paid original form once.

        submit=False is a read-only GET. True is allowed only at the HTTPS EH
        archiver endpoint. H@H landing pages receive an explicit empty Cookie,
        overriding any cookie jar on the shared client.
        """
        parsed = urlsplit(url)
        own_site = parsed.scheme == "https" and parsed.hostname in {"exhentai.org", "e-hentai.org"} and parsed.port in {None, 443}
        if (parsed.scheme not in {"http", "https"} or parsed.username is not None
                or (submit and (not own_site or parsed.path != "/archiver.php"))):
            raise ExHentaiDownloadError("EXHENTAI_ARCHIVE_URL", "EH 归档地址无效")
        headers = {"Cookie": self._cookie_header(credentials) if own_site else ""}
        try:
            if submit:
                return await self._client.post(url, headers=headers, follow_redirects=False,
                    data={"dltype": "org", "dlcheck": "Download Original Archive"})
            return await self._client.get(url, headers=headers, follow_redirects=False)
        except httpx.HTTPError:
            raise ExHentaiDownloadError("EXHENTAI_ARCHIVE_UNREACHABLE", "无法连接 EH 归档服务",
                                       failure_scope="site" if own_site else "operation", retry_after=60 if own_site else None) from None

    async def archive_file_url(self, credentials, continue_url):
        """Resolve the confirmed landing page to its file link, without a POST."""
        from app.exhentai.archive import ArchivePage, archive_response_error
        response = await self.archive_page(credentials, continue_url)
        error = archive_response_error(response)
        if error is not None:
            raise error
        link = ArchivePage(response.text, continue_url).download_url
        if link is None:
            raise ExHentaiDownloadError("EXHENTAI_ARCHIVE_DOWNLOAD_LINK", "EH 归档下载页未提供文件地址，请稍后核实")
        return link

    @staticmethod
    def _require_zip(destination: Path) -> None:
        """Refuse a response that is not a ZIP, and say so in those words.

        ExHentai's archive endpoint answers with an HTML page -- and HTTP 200 --
        when the cookie is stale, the request is rate-limited, or the H@H node
        refuses the client. Saving that page as `gallery-<gid>.zip` turns a
        login problem into an archive problem: the failure surfaces hours later
        inside the packer as 「无法读取 ZIP 压缩包」 (or, for RAR/7Z sources, as a
        7-Zip error), which says nothing about the cookie that actually needs
        fixing.
        """
        with destination.open("rb") as handle:
            head = handle.read(512)
        if head.startswith(ZIP_SIGNATURES):
            return
        prefix = head.lstrip()[:64].lower()
        if prefix.startswith(b"<") or b"<html" in head.lower():
            raise ExHentaiDownloadError(
                "EXHENTAI_ARCHIVE_NOT_ZIP",
                "原档下载返回的是网页而不是压缩包，通常是 ExHentai Cookie 已失效"
                "或被站点拦截；更新 Cookie 后重试",
            )
        raise ExHentaiDownloadError(
            "EXHENTAI_ARCHIVE_NOT_ZIP",
            "原档下载返回的内容不是 ZIP 压缩包，已丢弃；请稍后重试"
            "（若反复出现，检查网络或代理是否篡改了下载）",
        )

    async def download_archive(
        self,
        credentials: ExHentaiCredentials,
        archive_url: str,
        destination: Path,
    ) -> int:
        parsed = urlsplit(archive_url)
        own_site = parsed.scheme == "https" and parsed.hostname in {"exhentai.org", "e-hentai.org"} and parsed.port in {None, 443}
        cookie_header = self._cookie_header(credentials) if own_site else ""
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
            self._require_zip(destination)
            return copied
        except ExHentaiDownloadError:
            destination.unlink(missing_ok=True)
            raise
        except (httpx.HTTPError, OSError):
            destination.unlink(missing_ok=True)
            raise ExHentaiDownloadError(
                "EXHENTAI_ARCHIVE_DOWNLOAD",
                "原档下载失败，请核对网络或归档下载节点后重试",
            ) from None


# Re-export SimpleCookie so the imports above stay short
from http.cookies import SimpleCookie  # noqa: E402


__all__ = ["ExHentaiDownloader", "ExHentaiDownloadError"]
