from __future__ import annotations

import gzip
import io
import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.exhentai.tagdb_sync import ASSET_NAME
from app.main import create_app


SAMPLE_DB = {
    "version": 7,
    "data": [
        {
            "namespace": "reclass",
            "data": {"doujinshi": {"name": "TRANSLATED", "intro": "", "links": ""}},
        },
        {
            "namespace": "female",
            "data": {"big breasts": {"name": "TRANSLATED", "intro": "", "links": ""}},
        },
    ],
}


def make_settings(root: Path, *, translation: bool) -> Settings:
    return Settings(
        data_path=root / "data",
        library_path=root / "library",
        work_path=root / "work",
        app_secret_key="test-secret-key-with-at-least-32-characters",
        tag_translation_enabled=translation,
    )


def gzip_body(payload: dict) -> bytes:
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb") as handle:
        handle.write(json.dumps(payload).encode("utf-8"))
    return buffer.getvalue()


def test_startup_loads_tag_translator_from_dedicated_client(
    tmp_path: Path,
) -> None:
    seen_hosts: list[str] = []

    def tagdb_transport(request: httpx.Request) -> httpx.Response:
        seen_hosts.append(request.url.host)
        if request.url.host == "api.github.com":
            return httpx.Response(
                200,
                json={
                    "assets": [
                        {
                            "name": ASSET_NAME,
                            "browser_download_url": (
                                "https://example.invalid/db.text.json.gz"
                            ),
                        }
                    ]
                },
            )
        return httpx.Response(200, content=gzip_body(SAMPLE_DB))

    def exhentai_transport(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"ExHentai client must not be used: {request.url}")

    app = create_app(
        make_settings(tmp_path, translation=True),
        exhentai_transport=httpx.MockTransport(exhentai_transport),
        tagdb_transport=httpx.MockTransport(tagdb_transport),
    )
    with TestClient(app) as client:
        translator = client.app.state.tag_translator

    assert translator is not None
    assert translator.is_loaded
    assert translator.lookup("female", "big breasts").name == "TRANSLATED"
    assert "api.github.com" in seen_hosts


def test_startup_skips_tag_database_when_disabled(tmp_path: Path) -> None:
    def tagdb_transport(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Tag database must not be fetched: {request.url}")

    app = create_app(
        make_settings(tmp_path, translation=False),
        tagdb_transport=httpx.MockTransport(tagdb_transport),
    )
    with TestClient(app) as client:
        assert client.app.state.tag_translator is None


def test_startup_reuses_cached_database_without_network(tmp_path: Path) -> None:
    calls: list[str] = []

    def tagdb_transport(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "api.github.com":
            return httpx.Response(
                200,
                json={
                    "assets": [
                        {
                            "name": ASSET_NAME,
                            "browser_download_url": (
                                "https://example.invalid/db.text.json.gz"
                            ),
                        }
                    ]
                },
            )
        return httpx.Response(200, content=gzip_body(SAMPLE_DB))

    settings = make_settings(tmp_path, translation=True)
    for _ in range(2):
        app = create_app(
            settings, tagdb_transport=httpx.MockTransport(tagdb_transport)
        )
        with TestClient(app) as client:
            assert client.app.state.tag_translator is not None

    # The freshness window keeps the second startup entirely offline.
    assert calls == ["api.github.com", "example.invalid"]
