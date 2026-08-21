"""Unit coverage for the Telegraph preview-page source."""

from __future__ import annotations

import ipaddress
from pathlib import Path
import zipfile

import httpx
import pytest

from app.telegraph.client import (
    TelegraphClient,
    images_from_html,
    images_from_nodes,
)
from app.telegraph.fetcher import (
    FetchLimits,
    TelegraphFetcher,
    image_extension,
)
from app.telegraph.guard import (
    check_image_url,
    check_page_url,
    is_public_address,
)
from app.telegraph.models import FetchedImage, TelegraphError
from app.telegraph.packer import pack_directory, pack_images


JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 32


def public_resolver(host: str):
    return (ipaddress.ip_address("93.184.216.34"),)


def test_node_tree_is_read_in_document_order_without_duplicates() -> None:
    nodes = [
        {"tag": "p", "children": ["intro"]},
        {
            "tag": "figure",
            "children": [{"tag": "img", "attrs": {"src": "/file/one.jpg"}}],
        },
        {"tag": "img", "attrs": {"src": "https://pic.example/two"}},
        {"tag": "img", "attrs": {"src": "/file/one.jpg"}},
    ]

    assert images_from_nodes(nodes) == (
        "https://telegra.ph/file/one.jpg",
        "https://pic.example/two",
    )


def test_embedded_frames_are_not_treated_as_pages() -> None:
    nodes = [
        {"tag": "img", "attrs": {"src": "/embed/twitter/123"}},
        {"tag": "iframe", "attrs": {"src": "/embed/youtube/abc"}},
        {"tag": "img", "attrs": {"src": "/file/real.png"}},
    ]

    assert images_from_nodes(nodes) == ("https://telegra.ph/file/real.png",)


def test_protocol_relative_and_bare_sources_become_https() -> None:
    nodes = [
        {"tag": "img", "attrs": {"src": "//cdn.example/a.jpg"}},
        {"tag": "img", "attrs": {"src": "http://cdn.example/b.jpg"}},
        {"tag": "img", "attrs": {"src": "javascript:alert(1)"}},
        {"tag": "img", "attrs": {"src": "   "}},
    ]

    assert images_from_nodes(nodes) == (
        "https://cdn.example/a.jpg",
        "https://cdn.example/b.jpg",
    )


def test_html_fallback_reads_the_same_images() -> None:
    html = (
        '<figure><img src="/file/a.jpg"></figure>'
        '<figure><img src="https://pic.example/b"></figure>'
        '<img src="/file/a.jpg">'
    )

    assert images_from_html(html) == (
        "https://telegra.ph/file/a.jpg",
        "https://pic.example/b",
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://telegra.ph/Sample-01-01",
        "http://www.telegra.ph/Sample-01-01/",
        "https://graph.org/Sample-01-01",
    ],
)
def test_supported_page_hosts_are_canonicalized(url: str) -> None:
    canonical = check_page_url(url)

    assert canonical.startswith("https://")
    assert canonical.endswith("/Sample-01-01")


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/Sample",
        "ftp://telegra.ph/Sample",
        "https://telegra.ph/",
    ],
)
def test_unsupported_page_urls_are_refused(url: str) -> None:
    with pytest.raises(TelegraphError) as excinfo:
        check_page_url(url)

    assert excinfo.value.code == "TELEGRAPH_PAGE_PARSE"


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.2.3",
        "192.168.0.5",
        "169.254.1.1",
        "::1",
        "fc00::1",
        "fe80::1",
        "::ffff:10.0.0.1",
    ],
)
def test_private_and_local_addresses_are_not_public(address: str) -> None:
    assert not is_public_address(ipaddress.ip_address(address))


def test_an_image_host_resolving_into_private_space_is_blocked() -> None:
    def internal(host: str):
        return (ipaddress.ip_address("10.0.0.7"),)

    with pytest.raises(TelegraphError) as excinfo:
        check_image_url("https://pic.example/a.jpg", resolver=internal)

    assert excinfo.value.code == "TELEGRAPH_IMAGE_BLOCKED"


def test_a_literal_private_address_is_blocked_without_dns() -> None:
    with pytest.raises(TelegraphError):
        check_image_url("https://127.0.0.1/a.jpg", resolver=public_resolver)


def test_plain_http_image_urls_are_blocked() -> None:
    with pytest.raises(TelegraphError):
        check_image_url("http://pic.example/a.jpg", resolver=public_resolver)


def test_the_served_content_type_names_the_page_suffix() -> None:
    # Channel proxies serve WebP from extension-less paths, so the URL alone
    # cannot name the format.
    assert image_extension("https://pic.example/abc", "image/webp") == ".webp"
    assert image_extension("https://pic.example/a.JPEG", None) == ".jpg"
    assert image_extension("https://pic.example/a.bin", None) == ".jpg"


def build_fetcher(handler, limits: FetchLimits | None = None) -> TelegraphFetcher:
    return TelegraphFetcher(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        limits or FetchLimits(concurrency=2),
        resolver=public_resolver,
    )


@pytest.mark.asyncio
async def test_pages_are_named_with_zero_padding_in_page_order() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=JPEG, headers={"content-type": "image/jpeg"}
        )

    images = await build_fetcher(handler).fetch_all(
        "https://telegra.ph/Sample",
        tuple(f"https://pic.example/{index}" for index in range(1, 13)),
    )

    assert [image.name for image in images][:2] == ["0001.jpg", "0002.jpg"]
    assert images[-1].name == "0012.jpg"


@pytest.mark.asyncio
async def test_a_hotlink_refusal_is_retried_with_a_telegraph_referer() -> None:
    seen: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("referer"))
        if request.headers.get("referer") is None:
            return httpx.Response(403)
        return httpx.Response(
            200, content=WEBP, headers={"content-type": "image/webp"}
        )

    images = await build_fetcher(handler).fetch_all(
        "https://telegra.ph/Sample", ("https://pic.example/a",)
    )

    assert seen == [None, "https://telegra.ph/"]
    assert images[0].name == "0001.webp"


@pytest.mark.asyncio
async def test_a_redirect_into_private_space_is_blocked_per_hop() -> None:
    def resolver(host: str):
        if host == "internal.example":
            return (ipaddress.ip_address("192.168.1.10"),)
        return (ipaddress.ip_address("93.184.216.34"),)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "pic.example":
            return httpx.Response(
                302, headers={"location": "https://internal.example/a.jpg"}
            )
        return httpx.Response(200, content=JPEG)

    fetcher = TelegraphFetcher(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        FetchLimits(),
        resolver=resolver,
    )

    with pytest.raises(TelegraphError) as excinfo:
        await fetcher.fetch_all(
            "https://telegra.ph/Sample", ("https://pic.example/a.jpg",)
        )

    assert excinfo.value.code == "TELEGRAPH_IMAGE_BLOCKED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        b'<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>',
        b"<!doctype html><html><body>nope</body></html>",
        b"",
    ],
)
async def test_a_payload_that_is_not_an_image_is_refused(body: bytes) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=body, headers={"content-type": "image/jpeg"}
        )

    with pytest.raises(TelegraphError) as excinfo:
        await build_fetcher(handler).fetch_all(
            "https://telegra.ph/Sample", ("https://pic.example/a",)
        )

    assert excinfo.value.code in {
        "TELEGRAPH_IMAGE_BLOCKED",
        "TELEGRAPH_IMAGE_FAILED",
    }


@pytest.mark.asyncio
async def test_too_many_images_is_refused_before_any_request() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should be made")

    with pytest.raises(TelegraphError) as excinfo:
        await build_fetcher(handler, FetchLimits(max_images=2)).fetch_all(
            "https://telegra.ph/Sample",
            ("https://pic.example/a", "https://pic.example/b", "https://pic.example/c"),
        )

    assert excinfo.value.code == "TELEGRAPH_LIMIT_EXCEEDED"


@pytest.mark.asyncio
async def test_a_single_oversized_image_is_refused() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=JPEG + b"\x00" * 4096)

    limits = FetchLimits(max_image_bytes=64)
    with pytest.raises(TelegraphError) as excinfo:
        await build_fetcher(handler, limits).fetch_all(
            "https://telegra.ph/Sample", ("https://pic.example/a",)
        )

    assert excinfo.value.code == "TELEGRAPH_LIMIT_EXCEEDED"


@pytest.mark.asyncio
async def test_the_total_byte_budget_is_enforced_across_pages() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=JPEG)

    limits = FetchLimits(max_total_bytes=len(JPEG) + 1)
    with pytest.raises(TelegraphError) as excinfo:
        await build_fetcher(handler, limits).fetch_all(
            "https://telegra.ph/Sample",
            ("https://pic.example/a", "https://pic.example/b"),
        )

    assert excinfo.value.code == "TELEGRAPH_LIMIT_EXCEEDED"


@pytest.mark.asyncio
async def test_the_api_is_preferred_and_html_is_the_fallback() -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.host == "api.telegra.ph":
            return httpx.Response(200, json={"ok": False, "error": "PAGE_NOT_FOUND"})
        return httpx.Response(200, text='<figure><img src="/file/x.jpg"></figure>')

    client = TelegraphClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    page = await client.fetch_page("https://telegra.ph/Sample-01-01")

    assert page.image_urls == ("https://telegra.ph/file/x.jpg",)
    assert calls[0].startswith("https://api.telegra.ph/getPage/Sample-01-01")


@pytest.mark.asyncio
async def test_an_unreachable_page_reports_its_own_error_code() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = TelegraphClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )

    with pytest.raises(TelegraphError) as excinfo:
        await client.fetch_page("https://telegra.ph/Sample")

    assert excinfo.value.code == "TELEGRAPH_PAGE_UNREACHABLE"


@pytest.mark.asyncio
async def test_a_page_with_no_images_is_not_an_unreachable_page() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.telegra.ph":
            return httpx.Response(200, json={"ok": True, "result": {"content": []}})
        return httpx.Response(200, text="<p>text only</p>")

    client = TelegraphClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )

    with pytest.raises(TelegraphError) as excinfo:
        await client.fetch_page("https://telegra.ph/Sample")

    assert excinfo.value.code == "TELEGRAPH_NO_IMAGES"


def test_packing_stores_pages_uncompressed_and_atomically(tmp_path: Path) -> None:
    images = (
        FetchedImage(name="0001.jpg", data=JPEG, source_url="https://pic.example/a"),
        FetchedImage(name="0002.png", data=PNG, source_url="https://pic.example/b"),
    )
    destination = tmp_path / "out" / "book.zip"

    total = pack_images(images, destination)

    assert total == len(JPEG) + len(PNG)
    assert not destination.with_suffix(".zip.part").exists()
    with zipfile.ZipFile(destination) as archive:
        assert archive.namelist() == ["0001.jpg", "0002.png"]
        assert all(
            info.compress_type == zipfile.ZIP_STORED
            for info in archive.infolist()
        )


def test_packing_a_directory_uses_natural_page_order(tmp_path: Path) -> None:
    source = tmp_path / "pages"
    source.mkdir()
    for name in ("10.jpg", "2.jpg", "1.jpg"):
        (source / name).write_bytes(JPEG)
    destination = tmp_path / "book.zip"

    pack_directory(source, destination)

    with zipfile.ZipFile(destination) as archive:
        assert archive.namelist() == ["0001.jpg", "0002.jpg", "0003.jpg"]


def test_packing_a_directory_refuses_non_image_members(tmp_path: Path) -> None:
    source = tmp_path / "pages"
    source.mkdir()
    (source / "1.jpg").write_bytes(JPEG)
    (source / "readme.txt").write_text("hello", encoding="utf-8")

    with pytest.raises(TelegraphError) as excinfo:
        pack_directory(source, tmp_path / "book.zip")

    assert excinfo.value.code == "TELEGRAPH_IMAGE_BLOCKED"