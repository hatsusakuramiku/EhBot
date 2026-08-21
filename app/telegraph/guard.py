"""URL admission control for content pulled from a Telegraph page.

Every image URL on a preview page is attacker-controlled as far as this
application is concerned: anyone can publish a Telegraph page. The guard is
therefore applied to the page URL and to every image URL, and again to every
redirect hop, because a permitted host can redirect to a forbidden address.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

from app.candidates.links import PREVIEW_HOSTS
from app.telegraph.models import TelegraphError


#: Redirect hops allowed per image. Telegram file proxies behind Cloudflare
#: use at most one, so three leaves headroom without enabling a redirect loop.
MAX_REDIRECTS = 3


def _reject(message: str) -> TelegraphError:
    return TelegraphError("TELEGRAPH_IMAGE_BLOCKED", message)


def _resolve(host: str) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise TelegraphError(
            "TELEGRAPH_IMAGE_BLOCKED",
            f"无法解析图片主机 {host}",
        ) from exc
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        address = info[4][0]
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            continue
        if parsed not in addresses:
            addresses.append(parsed)
    if not addresses:
        raise _reject(f"图片主机 {host} 没有可用地址")
    return tuple(addresses)


def is_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Reject any address that could reach infrastructure instead of the internet.

    ``is_global`` alone is not enough: it accepts IPv4-mapped IPv6 forms of
    private space on some releases, so the mapped address is unwrapped first.
    """
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return is_public_address(address.ipv4_mapped)
        if address.is_site_local:
            return False
        # fc00::/7, which `is_private` covers, but state it for the reader.
        if address.packed[0] & 0xFE == 0xFC:
            return False
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def check_image_url(url: str, *, resolver=_resolve) -> str:
    """Return the URL if it may be fetched, else raise ``TelegraphError``.

    The resolver is injectable so tests can state an address without touching
    DNS; production callers use the real one.
    """
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise _reject(f"图片地址无法解析: {url}") from exc
    if parts.scheme.lower() != "https":
        raise _reject(f"图片地址不是 https: {url}")
    host = (parts.hostname or "").strip()
    if not host:
        raise _reject(f"图片地址缺少主机名: {url}")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not is_public_address(literal):
            raise _reject(f"图片地址指向内网: {url}")
        return url
    for address in resolver(host):
        if not is_public_address(address):
            raise _reject(f"图片主机 {host} 解析到内网地址 {address}")
    return url


def check_page_url(url: str) -> str:
    """Admit only a Telegraph page URL.

    The page host is a fixed allowlist rather than a DNS check, because the
    page is fetched through the official API and there is no reason to accept
    an arbitrary host here.
    """
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise TelegraphError(
            "TELEGRAPH_PAGE_PARSE", f"预览页地址无法解析: {url}"
        ) from exc
    if parts.scheme.lower() not in {"http", "https"}:
        raise TelegraphError(
            "TELEGRAPH_PAGE_PARSE", f"预览页地址不是 http(s): {url}"
        )
    host = (parts.hostname or "").lower().removeprefix("www.")
    if host not in PREVIEW_HOSTS:
        raise TelegraphError(
            "TELEGRAPH_PAGE_PARSE", f"不受支持的预览页主机: {host or url}"
        )
    path = parts.path.strip("/")
    if not path:
        raise TelegraphError(
            "TELEGRAPH_PAGE_PARSE", "预览页地址缺少页面路径"
        )
    return f"https://{host}/{path}"


def page_path(url: str) -> str:
    """The Telegraph path used by ``getPage``, taken from a checked URL."""
    return urlsplit(check_page_url(url)).path.strip("/")


__all__ = [
    "MAX_REDIRECTS",
    "check_image_url",
    "check_page_url",
    "is_public_address",
    "page_path",
]