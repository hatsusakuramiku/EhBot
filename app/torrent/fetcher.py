"""Fetch the `.torrent` file for a gallery from ExHentai.

gdata publishes only the infohash, so the file itself has to come from the
logged-in `gallerytorrents.php` page. Two things follow from that, and both are
enforced here rather than left to the caller:

* the link template is never hard-coded — it is parsed off the page, because a
  guessed URL silently rots when the site changes;
* the downloaded file embeds the account's tracker passkey, so it is a
  credential: it stays in the work directory and never reaches the library, the
  logs, or the audit trail.
"""

from __future__ import annotations

from html.parser import HTMLParser
from http.cookies import SimpleCookie
import re
from urllib.parse import urljoin

import httpx

from app.connections.exhentai import ExHentaiCredentials
from app.torrent.bencode import MAX_TORRENT_BYTES, infohash
from app.torrent.models import TorrentError


TORRENT_PAGE_URL = "https://exhentai.org/gallerytorrents.php"

_HASH_PATTERN = re.compile(r"\b([0-9a-f]{40})\b", re.IGNORECASE)


class _TorrentLinkParser(HTMLParser):
    """Collect every link on the torrent page in document order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag != "a":
            return
        for name, value in attrs:
            if name == "href" and value:
                self.links.append(value.strip())


def torrent_links(html: str) -> tuple[str, ...]:
    parser = _TorrentLinkParser()
    parser.feed(html)
    parser.close()
    return tuple(
        link for link in parser.links if ".torrent" in link.lower()
    )


def select_link(html: str, digest: str) -> str:
    """Find the download link belonging to one infohash.

    The page lists every torrent for the gallery, so the hash decides which link
    is taken. A hash the page does not mention is refused rather than falling
    back to the first link, because downloading the wrong torrent would be
    detected only after the client had already started fetching it.
    """
    wanted = digest.lower()
    links = torrent_links(html)
    if not links:
        raise TorrentError(
            "TORRENT_FILE_FETCH_FAILED",
            "种子页面没有可下载的 torrent 链接",
        )
    for link in links:
        if wanted in link.lower():
            return link
    # EH does not always put the hash in the URL. Fall back to the hash's own
    # position in the page: the nearest following torrent link belongs to it.
    # The links come from the parser rather than a second regex, so quoted and
    # unquoted `href` attributes are treated alike.
    lowered = html.lower()
    position = lowered.find(wanted)
    if position != -1:
        best: tuple[int, str] | None = None
        for link in links:
            at = lowered.find(link.lower(), position)
            if at != -1 and (best is None or at < best[0]):
                best = (at, link)
        if best is not None:
            return best[1]
    raise TorrentError(
        "TORRENT_FILE_FETCH_FAILED",
        f"种子页面没有 infohash 为 {digest[:8]}… 的条目",
    )


def page_hashes(html: str) -> tuple[str, ...]:
    """Every infohash the page mentions, lowercased."""
    return tuple(
        dict.fromkeys(match.group(1).lower() for match in _HASH_PATTERN.finditer(html))
    )


class TorrentFileFetcher:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    @staticmethod
    def _cookie_header(credentials: ExHentaiCredentials) -> str:
        cookie = SimpleCookie()
        for name, value in credentials.as_cookies().items():
            cookie[name] = value
        return cookie.output(header="", sep=";").strip()

    async def fetch(
        self,
        credentials: ExHentaiCredentials,
        gid: int,
        token: str,
        digest: str,
    ) -> bytes:
        """Download and verify the `.torrent` for one gallery torrent."""
        cookie_header = self._cookie_header(credentials)
        headers = {"Cookie": cookie_header}
        try:
            page = await self._client.get(
                TORRENT_PAGE_URL,
                params={"gid": int(gid), "t": token},
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise TorrentError(
                "TORRENT_FILE_FETCH_FAILED",
                f"无法访问种子页面: {exc}",
            ) from exc
        if page.status_code != 200:
            raise TorrentError(
                "TORRENT_FILE_FETCH_FAILED",
                f"种子页面返回 HTTP {page.status_code}",
            )
        link = urljoin(str(page.url), select_link(page.text, digest))
        try:
            response = await self._client.get(
                link, headers=headers, follow_redirects=True
            )
        except httpx.HTTPError as exc:
            raise TorrentError(
                "TORRENT_FILE_FETCH_FAILED",
                f"下载 torrent 文件失败: {exc}",
            ) from exc
        if response.status_code != 200:
            raise TorrentError(
                "TORRENT_FILE_FETCH_FAILED",
                f"torrent 文件返回 HTTP {response.status_code}",
            )
        payload = response.content
        if not payload:
            raise TorrentError(
                "TORRENT_FILE_FETCH_FAILED", "torrent 文件是空文件"
            )
        if len(payload) > MAX_TORRENT_BYTES:
            raise TorrentError(
                "TORRENT_FILE_INVALID", "torrent 文件超过大小上限"
            )
        # The local hash is the only thing that proves the page parsing picked
        # the right entry and that the file was not truncated in transit.
        computed = infohash(payload)
        if computed != digest.lower():
            raise TorrentError(
                "TORRENT_FILE_INVALID",
                f"torrent infohash 不符：期望 {digest[:8]}…，实际 {computed[:8]}…",
            )
        return payload


__all__ = [
    "TORRENT_PAGE_URL",
    "TorrentFileFetcher",
    "page_hashes",
    "select_link",
    "torrent_links",
]