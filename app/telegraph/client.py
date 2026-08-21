"""Read the ordered image list out of a Telegraph page.

The official ``getPage`` API returns the page as a node tree, which is both
cheaper and more reliable than scraping the rendered HTML, so it is tried
first. HTML parsing remains as a fallback for the case where the API refuses
a page it still serves to browsers.
"""

from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

from app.telegraph.guard import check_page_url, page_path
from app.telegraph.models import TelegraphError, TelegraphPage


API_ENDPOINT = "https://api.telegra.ph/getPage"

PAGE_BASE = "https://telegra.ph"

#: Telegraph rejects a plain client on some edges, and the image hosts always
#: do, so a browser-shaped agent is used for both.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _absolute(src: str) -> str | None:
    """Resolve one ``img`` source against the Telegraph origin.

    Telegraph stores its own uploads as ``/file/<name>``; channel-run proxies
    appear as absolute URLs. ``/embed/`` is skipped because it is an embedded
    tweet or video frame, never a page image.
    """
    candidate = src.strip()
    if not candidate:
        return None
    if candidate.startswith("//"):
        candidate = f"https:{candidate}"
    elif candidate.startswith("/"):
        if candidate.startswith("/embed/"):
            return None
        candidate = urljoin(f"{PAGE_BASE}/", candidate.lstrip("/"))
    parts = urlsplit(candidate)
    if parts.scheme.lower() not in {"http", "https"}:
        return None
    if parts.path.startswith("/embed/"):
        return None
    if not parts.hostname:
        return None
    # Telegraph's own uploads are served over https even when linked bare.
    if parts.scheme.lower() == "http":
        candidate = candidate.replace("http://", "https://", 1)
    return candidate


def _collect_from_nodes(nodes, found: list[str]) -> None:
    """Walk the node tree in document order, appending each image once."""
    if not isinstance(nodes, list):
        return
    for node in nodes:
        if isinstance(node, str):
            continue
        if not isinstance(node, dict):
            continue
        if node.get("tag") == "img":
            attrs = node.get("attrs")
            src = ""
            if isinstance(attrs, dict):
                src = str(attrs.get("src") or "")
            resolved = _absolute(src)
            if resolved is not None and resolved not in found:
                found.append(resolved)
        _collect_from_nodes(node.get("children"), found)


class _ImageHtmlParser(HTMLParser):
    """Fallback extraction from the rendered page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sources: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag != "img":
            return
        for name, value in attrs:
            if name != "src" or not value:
                continue
            resolved = _absolute(value)
            if resolved is not None and resolved not in self.sources:
                self.sources.append(resolved)


def images_from_nodes(nodes) -> tuple[str, ...]:
    found: list[str] = []
    _collect_from_nodes(nodes, found)
    return tuple(found)


def images_from_html(html: str) -> tuple[str, ...]:
    parser = _ImageHtmlParser()
    parser.feed(html)
    parser.close()
    return tuple(parser.sources)


class TelegraphClient:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def fetch_page(self, url: str) -> TelegraphPage:
        canonical = check_page_url(url)
        path = page_path(canonical)
        page = await self._fetch_via_api(path, canonical)
        if page is not None:
            return page
        return await self._fetch_via_html(path, canonical)

    async def _fetch_via_api(
        self, path: str, canonical: str
    ) -> TelegraphPage | None:
        try:
            response = await self._client.get(
                f"{API_ENDPOINT}/{path}",
                params={"return_content": "true"},
                headers={"User-Agent": USER_AGENT},
            )
        except httpx.HTTPError:
            # Reachability is decided by the HTML attempt; a transport error
            # here is not yet a verdict.
            return None
        if response.status_code != 200:
            return None
        try:
            body = response.json()
        except ValueError:
            return None
        if not isinstance(body, dict) or not body.get("ok"):
            return None
        result = body.get("result")
        if not isinstance(result, dict):
            return None
        images = images_from_nodes(result.get("content"))
        if not images:
            return None
        return TelegraphPage(
            path=path,
            url=canonical,
            title=str(result.get("title") or "") or None,
            author=str(result.get("author_name") or "") or None,
            image_urls=images,
        )

    async def _fetch_via_html(
        self, path: str, canonical: str
    ) -> TelegraphPage:
        try:
            response = await self._client.get(
                canonical,
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            raise TelegraphError(
                "TELEGRAPH_PAGE_UNREACHABLE",
                f"无法访问预览页 {canonical}",
            ) from exc
        if response.status_code != 200:
            raise TelegraphError(
                "TELEGRAPH_PAGE_UNREACHABLE",
                f"预览页返回 HTTP {response.status_code}",
            )
        images = images_from_html(response.text)
        if not images:
            raise TelegraphError(
                "TELEGRAPH_NO_IMAGES",
                "预览页没有可下载的图片",
            )
        return TelegraphPage(
            path=path,
            url=canonical,
            title=None,
            author=None,
            image_urls=images,
        )


__all__ = [
    "API_ENDPOINT",
    "PAGE_BASE",
    "USER_AGENT",
    "TelegraphClient",
    "images_from_html",
    "images_from_nodes",
]