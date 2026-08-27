"""Integration coverage for the thumbnail proxy endpoint and the R2 readers.

The endpoint is a proxy, so most of what is worth asserting is what it
*refuses* to do: it never fetches a URL a caller supplied, it never returns a
404 an `<img>` would render as a broken icon, and it never reaches a host the
SSRF guard rejects.
"""

from __future__ import annotations

import asyncio
import io
import ipaddress
from pathlib import Path
import sqlite3

import httpx
from PIL import Image
from fastapi.testclient import TestClient
import pytest

from app.api.deps import CSRF_HEADER
from app.config import Settings
from app.db.database import Database
from app.main import create_app
from app.thumbnails import (
    THUMBNAIL_KIND_CANDIDATE_COVER,
    THUMBNAIL_VARIANT_CARD,
)
from app.thumbnails.identity import disk_path, identity_hash
from tests.integration.test_api_domains import csrf_token, seed
from tests.integration.test_api_v1 import log_in, make_settings


COVER_URL = "https://ehgt.org/ab/cd/coverhash-1234567-250-350-jpg_250.jpg"
COVER_DIGEST = identity_hash(COVER_URL, THUMBNAIL_VARIANT_CARD)


def jpeg(width: int = 250, height: int = 350) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (30, 60, 120)).save(buffer, format="JPEG")
    return buffer.getvalue()


def public_resolver(host: str):
    return (ipaddress.ip_address("93.184.216.34"),)


def private_resolver(host: str):
    """Every name lands inside the LAN — the shape of a DNS-rebind attempt."""
    return (ipaddress.ip_address("127.0.0.1"),)


def admit_cover(
    settings: Settings,
    *,
    source_url: str = COVER_URL,
) -> str:
    """Open a cache slot the way the scrape path does, and return its digest.

    The endpoint takes no URL, so a thumbnail only becomes fetchable when
    something upstream vouched for it by writing this row. Tests have to go
    through the same door.
    """
    database = Database(settings.data_path / "ehbot.db")
    asyncio.run(database.initialize())
    return admit_cover_into(database, source_url=source_url)


def admit_cover_into(database: Database, *, source_url: str = COVER_URL) -> str:
    """`admit_cover` for a database an async test has already initialised."""
    digest = identity_hash(source_url, THUMBNAIL_VARIANT_CARD)
    with sqlite3.connect(database.path) as connection:
        connection.execute(
            "INSERT INTO thumbnails (hash, kind, variant, source_url, state) "
            "VALUES (?, ?, ?, ?, 'PENDING') ON CONFLICT(hash) DO NOTHING",
            (
                digest,
                THUMBNAIL_KIND_CANDIDATE_COVER,
                THUMBNAIL_VARIANT_CARD,
                source_url,
            ),
        )
    return digest


class RecordingTransport(httpx.MockTransport):
    """A cover host that counts how many times it was actually asked."""

    def __init__(self, *, status: int = 200, body: bytes | None = None) -> None:
        self.requests: list[httpx.Request] = []
        payload = jpeg() if body is None else body

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if status != 200:
                return httpx.Response(status, content=b"nope")
            return httpx.Response(
                200, content=payload, headers={"content-type": "image/jpeg"}
            )

        super().__init__(handler)


def build_app(
    settings: Settings,
    transport: httpx.MockTransport,
    *,
    resolver=public_resolver,
):
    settings.data_path.mkdir(parents=True, exist_ok=True)
    return create_app(
        settings,
        thumbnail_transport=transport,
        thumbnail_resolver=resolver,
    )


class TestThumbnailEndpoint:
    def test_a_pending_cover_is_fetched_and_served_as_webp(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        transport = RecordingTransport()
        with TestClient(build_app(settings, transport)) as client:
            digest = admit_cover(settings)
            log_in(client, settings)
            response = client.get(f"/api/v1/thumbnails/{digest}")

        assert response.status_code == 200
        # Whatever upstream sent, what leaves this server is bytes we encoded.
        assert response.headers["content-type"] == "image/webp"
        assert response.headers["x-thumbnail-state"] == "ready"
        assert response.headers["etag"] == f'"{digest}"'
        assert "immutable" in response.headers["cache-control"]
        with Image.open(io.BytesIO(response.content)) as decoded:
            assert decoded.format == "WEBP"

    def test_the_second_request_is_served_from_disk(
        self, tmp_path: Path
    ) -> None:
        """`immutable` would be a lie if the cache did not actually hold."""
        settings = make_settings(tmp_path)
        transport = RecordingTransport()
        with TestClient(build_app(settings, transport)) as client:
            digest = admit_cover(settings)
            log_in(client, settings)
            client.get(f"/api/v1/thumbnails/{digest}")
            second = client.get(f"/api/v1/thumbnails/{digest}")

        assert second.status_code == 200
        assert len(transport.requests) == 1
        assert disk_path(settings.data_path / "thumbnails", digest).is_file()

    def test_a_matching_etag_gets_a_304_with_no_body(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        transport = RecordingTransport()
        with TestClient(build_app(settings, transport)) as client:
            digest = admit_cover(settings)
            log_in(client, settings)
            client.get(f"/api/v1/thumbnails/{digest}")
            response = client.get(
                f"/api/v1/thumbnails/{digest}",
                headers={"If-None-Match": f'"{digest}"'},
            )

        assert response.status_code == 304
        assert response.content == b""
        assert response.headers["etag"] == f'"{digest}"'

    def test_a_stale_etag_still_gets_the_bytes(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        transport = RecordingTransport()
        with TestClient(build_app(settings, transport)) as client:
            digest = admit_cover(settings)
            log_in(client, settings)
            client.get(f"/api/v1/thumbnails/{digest}")
            response = client.get(
                f"/api/v1/thumbnails/{digest}",
                headers={"If-None-Match": '"0" * 64'},
            )

        assert response.status_code == 200
        assert response.content

    def test_an_unknown_digest_gets_a_placeholder_not_a_404(
        self, tmp_path: Path
    ) -> None:
        """A 404 on an `<img>` src renders as a broken-image icon.

        There is no way to style around that, so an unknown cover is answered
        with a real image and the truth in a header instead.
        """
        settings = make_settings(tmp_path)
        transport = RecordingTransport()
        with TestClient(build_app(settings, transport)) as client:
            log_in(client, settings)
            response = client.get(f"/api/v1/thumbnails/{'a' * 64}")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("image/svg+xml")
        assert response.headers["x-thumbnail-state"] == "failed"
        assert response.headers["x-thumbnail-error"] == "NO_SOURCE"
        # Never a year: a transient failure must not pin a broken cover.
        assert "immutable" not in response.headers["cache-control"]
        # And nothing was fetched, because no caller can name a URL.
        assert transport.requests == []

    def test_a_malformed_hash_never_reaches_the_disk_layout(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        transport = RecordingTransport()
        with TestClient(build_app(settings, transport)) as client:
            log_in(client, settings)
            response = client.get("/api/v1/thumbnails/..%2f..%2fehbot.db")

        assert response.status_code == 404
        assert transport.requests == []

    def test_an_upstream_error_is_a_placeholder_with_the_reason(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        transport = RecordingTransport(status=500)
        with TestClient(build_app(settings, transport)) as client:
            digest = admit_cover(settings)
            log_in(client, settings)
            response = client.get(f"/api/v1/thumbnails/{digest}")

        assert response.status_code == 200
        assert response.headers["x-thumbnail-state"] == "failed"
        assert response.headers["x-thumbnail-error"] == "UPSTREAM_ERROR"

    def test_a_403_is_retried_once_with_a_referer(self, tmp_path: Path) -> None:
        """Image hosts refuse hotlinks; ExHentai's does it with a 403."""
        settings = make_settings(tmp_path)
        seen: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("referer"))
            if request.headers.get("referer") is None:
                return httpx.Response(403, content=b"forbidden")
            return httpx.Response(
                200, content=jpeg(), headers={"content-type": "image/jpeg"}
            )

        with TestClient(
            build_app(settings, httpx.MockTransport(handler))
        ) as client:
            digest = admit_cover(settings)
            log_in(client, settings)
            response = client.get(f"/api/v1/thumbnails/{digest}")

        assert response.status_code == 200
        assert response.headers["x-thumbnail-state"] == "ready"
        assert seen == [None, "https://exhentai.org/"]

    def test_html_served_with_a_200_is_not_stored_as_a_cover(
        self, tmp_path: Path
    ) -> None:
        """An overloaded image host answers with a styled error page and a 200."""
        settings = make_settings(tmp_path)
        transport = RecordingTransport(body=b"<!DOCTYPE html><html>503</html>")
        with TestClient(build_app(settings, transport)) as client:
            digest = admit_cover(settings)
            log_in(client, settings)
            response = client.get(f"/api/v1/thumbnails/{digest}")

        assert response.headers["x-thumbnail-error"] == "IMAGE_NOT_RECOGNISED"
        assert not disk_path(settings.data_path / "thumbnails", digest).exists()

    def test_a_private_address_is_refused_before_the_fetch(
        self, tmp_path: Path
    ) -> None:
        """The URL in the row came from a scrape, so it is not above suspicion."""
        settings = make_settings(tmp_path)
        transport = RecordingTransport()
        app = build_app(settings, transport, resolver=private_resolver)
        with TestClient(app) as client:
            digest = admit_cover(settings)
            log_in(client, settings)
            response = client.get(f"/api/v1/thumbnails/{digest}")

        assert response.status_code == 200
        assert response.headers["x-thumbnail-state"] == "failed"
        assert transport.requests == []

    def test_the_endpoint_needs_a_session(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        transport = RecordingTransport()
        with TestClient(build_app(settings, transport)) as client:
            digest = admit_cover(settings)
            response = client.get(
                f"/api/v1/thumbnails/{digest}", follow_redirects=False
            )

        assert response.status_code == 401
        assert transport.requests == []


class TestConcurrentFirstPaint:
    @pytest.mark.asyncio
    async def test_a_grid_of_identical_covers_fetches_once(
        self, tmp_path: Path
    ) -> None:
        """A first paint asks for every cover at once.

        Without per-hash dedup, one shared cover would mean N simultaneous
        outbound requests for the same bytes.
        """
        from app.thumbnails.service import ThumbnailService

        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        database = Database(settings.data_path / "ehbot.db")
        await database.initialize()
        digest = admit_cover_into(database)

        transport = RecordingTransport()
        async with httpx.AsyncClient(transport=transport) as http_client:
            service = ThumbnailService(
                database,
                settings.data_path / "thumbnails",
                http_client,
            )
            results = await asyncio.gather(
                *(service.get_or_create(digest) for _ in range(8))
            )

        assert len(transport.requests) == 1
        assert {result.state for result in results} == {"READY"}


class TestCoverInTheCandidatePayload:
    def test_a_candidate_with_a_cover_exposes_only_the_proxy_url(
        self, tmp_path: Path
    ) -> None:
        """The upstream URL must not reach the browser.

        Pointing an `<img>` at ExHentai's CDN would tell that host every cover
        this deployment renders, and would break on the first hotlink refusal.
        """
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        transport = RecordingTransport()
        with TestClient(build_app(settings, transport)) as client:
            seed(settings, [(1, "PENDING_REVIEW", "Book 1")])
            with sqlite3.connect(settings.data_path / "ehbot.db") as connection:
                connection.execute(
                    "UPDATE candidates SET thumb_url = ? WHERE id = 1",
                    (COVER_URL,),
                )
            log_in(client, settings)
            item = client.get("/api/v1/candidates").json()["items"][0]

        assert item["cover"] == {
            "url": f"/api/v1/thumbnails/{COVER_DIGEST}",
            "hash": COVER_DIGEST,
        }
        assert "ehgt.org" not in str(item)

    def test_a_candidate_without_a_cover_reports_none(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        transport = RecordingTransport()
        with TestClient(build_app(settings, transport)) as client:
            seed(settings, [(1, "PENDING_REVIEW", "Book 1")])
            log_in(client, settings)
            item = client.get("/api/v1/candidates").json()["items"][0]

        assert item["cover"] is None
