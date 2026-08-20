import gzip
import io
import json
from pathlib import Path

import httpx
import pytest

from app.exhentai.enrich import enrich_metadata
from app.exhentai.gdata import parse_gdata_entry
from app.exhentai.tagdb import TagTranslator
from app.exhentai.tagdb_sync import (
    ASSET_NAME,
    TagDatabaseError,
    TagDatabaseSync,
)


@pytest.fixture
def always_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable the daily freshness window so each call hits the network."""
    monkeypatch.setattr(
        "app.exhentai.tagdb_sync.MIN_REFRESH_INTERVAL_SECONDS", 0
    )


SAMPLE_DB = {
    "version": 7,
    "data": [
        {
            "namespace": "rows",
            "data": {
                "female": {"name": "女性", "intro": "", "links": ""},
                "male": {"name": "男性", "intro": "", "links": ""},
            },
        },
        {
            "namespace": "reclass",
            "data": {
                "doujinshi": {"name": "同人志", "intro": "", "links": ""},
            },
        },
        {
            "namespace": "language",
            "data": {
                "chinese": {"name": "汉语", "intro": "", "links": ""},
                "translated": {"name": "翻译", "intro": "", "links": ""},
            },
        },
        {
            "namespace": "artist",
            "data": {
                "kamisiro ryu": {"name": "神代龙", "intro": "", "links": ""},
            },
        },
        {
            "namespace": "group",
            "data": {
                "kuroneko akaribon": {
                    "name": "黒ねこ赤リボン",
                    "intro": "",
                    "links": "",
                },
            },
        },
        {
            "namespace": "parody",
            "data": {
                "original": {"name": "原创", "intro": "", "links": ""},
            },
        },
        {
            "namespace": "female",
            "data": {
                "big breasts": {
                    "name": "巨乳",
                    "intro": "尺寸较大的乳房。",
                    "links": "",
                },
                "bunny girl": {"name": "兔女郎", "intro": "", "links": ""},
            },
        },
        {
            "namespace": "male",
            "data": {
                "big breasts": {"name": "巨乳", "intro": "", "links": ""},
                "sole male": {"name": "单男主", "intro": "", "links": ""},
            },
        },
    ],
}


def _translator() -> TagTranslator:
    translator = TagTranslator()
    translator.load(SAMPLE_DB)
    return translator


def test_translator_indexes_tags_and_skips_pseudo_namespaces() -> None:
    translator = _translator()
    # rows/reclass are metadata about namespaces, not tags themselves.
    assert translator.lookup("rows", "female") is None
    assert translator.lookup("reclass", "doujinshi") is None
    assert translator.namespace_name("female") == "女性"
    assert translator.category_name("Doujinshi") == "同人志"


def test_translator_resolves_namespaced_tags() -> None:
    translator = _translator()
    found = translator.lookup("female", "big breasts")
    assert found is not None
    assert found.name == "巨乳"
    assert found.qualified_raw == "female:big breasts"
    assert found.intro.startswith("尺寸")


def test_translator_normalizes_case_and_whitespace() -> None:
    translator = _translator()
    found = translator.lookup("Female", "  Big   Breasts ")
    assert found is not None
    assert found.name == "巨乳"


def test_translator_probes_namespaces_for_implicit_tags() -> None:
    translator = _translator()
    found = translator.lookup(None, "big breasts")
    assert found is not None
    # female is ranked ahead of male, so it wins the implicit probe.
    assert found.qualified_raw == "female:big breasts"


def test_translator_reverse_lookup_prefers_conventional_namespace() -> None:
    translator = _translator()
    assert translator.raw_for_name("巨乳") == "female:big breasts"
    assert translator.raw_for_name("神代龙") == "artist:kamisiro ryu"
    assert translator.raw_for_name("不存在的标签") is None


def test_translate_tags_keeps_unknown_tags_untouched() -> None:
    translator = _translator()
    result = translator.translate_tags(
        ("female:big breasts", "female:unmapped tag", "male:sole male")
    )
    assert result == ("巨乳", "female:unmapped tag", "单男主")


def test_translate_tag_handles_bare_and_empty_input() -> None:
    translator = _translator()
    assert translator.translate_tag("bunny girl").name == "兔女郎"
    assert translator.translate_tag("") is None
    assert translator.translate_tag("nope:nothing") is None
SAMPLE_ENTRY = {
    "gid": 4116328,
    "token": "abcdef1234",
    "title": "[Kuroneko Akaribon (Kamisiro Ryu)] Sample",
    "title_jpn": "サンプル",
    "category": "Doujinshi",
    "uploader": "someone",
    "filecount": "24",
    "tags": [
        "language:chinese",
        "language:translated",
        "parody:original",
        "group:kuroneko akaribon",
        "artist:kamisiro ryu",
        "female:big breasts",
        "female:unmapped tag",
    ],
}


def _gzip_body(payload: dict) -> bytes:
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb") as handle:
        handle.write(json.dumps(payload).encode("utf-8"))
    return buffer.getvalue()


def _release_payload(url: str = "https://example.invalid/db.text.json.gz") -> dict:
    return {"assets": [{"name": ASSET_NAME, "browser_download_url": url}]}


def _sync(tmp_path: Path, handler) -> tuple[TagDatabaseSync, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TagDatabaseSync(tmp_path, client), client


@pytest.mark.asyncio
async def test_sync_downloads_and_caches_database(tmp_path: Path) -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.host == "api.github.com":
            return httpx.Response(200, json=_release_payload())
        return httpx.Response(
            200,
            content=_gzip_body(SAMPLE_DB),
            headers={"ETag": '"abc123"'},
        )

    sync, client = _sync(tmp_path, handler)
    async with client:
        result = await sync.synchronize()

    assert result.updated is True
    assert result.from_cache is False
    assert result.reason == "downloaded"
    assert result.entry_count == 12
    assert sync.cache_path.is_file()
    assert len(calls) == 2
    cached = sync.load_cached()
    assert cached is not None and cached["version"] == 7


@pytest.mark.asyncio
async def test_sync_sends_conditional_request_and_reuses_cache(
    tmp_path: Path, always_check: None
) -> None:
    seen_headers: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com":
            return httpx.Response(200, json=_release_payload())
        seen_headers.append(request.headers.get("If-None-Match"))
        if len(seen_headers) == 1:
            return httpx.Response(
                200,
                content=_gzip_body(SAMPLE_DB),
                headers={"ETag": '"abc123"'},
            )
        return httpx.Response(304)

    sync, client = _sync(tmp_path, handler)
    async with client:
        first = await sync.synchronize()
        second = await sync.synchronize()

    assert first.updated is True
    assert second.updated is False
    assert second.from_cache is True
    assert second.reason == "not_modified"
    assert seen_headers == [None, '"abc123"']


@pytest.mark.asyncio
async def test_sync_forces_full_download_when_requested(tmp_path: Path) -> None:
    seen_headers: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com":
            return httpx.Response(200, json=_release_payload())
        seen_headers.append(request.headers.get("If-None-Match"))
        return httpx.Response(
            200,
            content=_gzip_body(SAMPLE_DB),
            headers={"ETag": '"abc123"'},
        )

    sync, client = _sync(tmp_path, handler)
    async with client:
        await sync.synchronize()
        result = await sync.synchronize(force=True)

    assert result.updated is True
    assert seen_headers == [None, None]


@pytest.mark.asyncio
async def test_sync_degrades_to_cache_when_network_fails(
    tmp_path: Path, always_check: None
) -> None:
    fail = False

    async def handler(request: httpx.Request) -> httpx.Response:
        if fail:
            raise httpx.ConnectError("offline", request=request)
        if request.url.host == "api.github.com":
            return httpx.Response(200, json=_release_payload())
        return httpx.Response(200, content=_gzip_body(SAMPLE_DB))

    sync, client = _sync(tmp_path, handler)
    async with client:
        await sync.synchronize()
        fail = True
        result = await sync.synchronize()

    assert result.updated is False
    assert result.from_cache is True
    assert result.reason == "network_failed_using_cache"
    assert result.entry_count == 12


@pytest.mark.asyncio
async def test_sync_raises_when_offline_without_cache(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    sync, client = _sync(tmp_path, handler)
    async with client:
        with pytest.raises(TagDatabaseError) as excinfo:
            await sync.synchronize()

    assert excinfo.value.code == "EHTAG_UNAVAILABLE"


@pytest.mark.asyncio
async def test_sync_reports_missing_release_asset(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"assets": [{"name": "other.zip"}]})

    sync, client = _sync(tmp_path, handler)
    async with client:
        with pytest.raises(TagDatabaseError) as excinfo:
            await sync.synchronize()

    # No cache exists, so the asset lookup failure surfaces as unavailable.
    assert excinfo.value.code == "EHTAG_UNAVAILABLE"


@pytest.mark.asyncio
async def test_sync_keeps_cache_when_payload_is_corrupt(
    tmp_path: Path, always_check: None
) -> None:
    corrupt = False

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com":
            return httpx.Response(200, json=_release_payload())
        if corrupt:
            return httpx.Response(200, content=b"not-gzip-at-all")
        return httpx.Response(200, content=_gzip_body(SAMPLE_DB))

    sync, client = _sync(tmp_path, handler)
    async with client:
        await sync.synchronize()
        corrupt = True
        result = await sync.synchronize()

    assert result.from_cache is True
    assert result.reason == "payload_invalid_using_cache"
    assert sync.load_cached() is not None


@pytest.mark.asyncio
async def test_sync_rejects_corrupt_payload_without_cache(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com":
            return httpx.Response(200, json=_release_payload())
        return httpx.Response(200, content=b"not-gzip-at-all")

    sync, client = _sync(tmp_path, handler)
    async with client:
        with pytest.raises(TagDatabaseError) as excinfo:
            await sync.synchronize()

    assert excinfo.value.code == "EHTAG_PAYLOAD_INVALID"


def test_enrich_metadata_translates_fields_and_keeps_raw() -> None:
    gallery = parse_gdata_entry(SAMPLE_ENTRY)
    assert gallery is not None
    metadata = enrich_metadata(gallery, _translator())

    assert metadata["Category"] == "同人志"
    assert metadata["CategoryRaw"] == "Doujinshi"
    assert metadata["Artist"] == "神代龙"
    assert metadata["ArtistRaw"] == "kamisiro ryu"
    assert metadata["Group"] == "黒ねこ赤リボン"
    assert metadata["Parody"] == "原创"
    assert metadata["Language"] == "汉语"
    assert metadata["LanguageRaw"] == "chinese"
    assert metadata["Pages"] == "24"


def test_enrich_metadata_separates_raw_and_matched_chinese_tags() -> None:
    gallery = parse_gdata_entry(SAMPLE_ENTRY)
    assert gallery is not None
    metadata = enrich_metadata(gallery, _translator())

    assert metadata["TagsRaw"].split(", ") == SAMPLE_ENTRY["tags"]
    assert metadata["Tags"].split(", ") == [
        "汉语",
        "翻译",
        "原创",
        "黒ねこ赤リボン",
        "神代龙",
        "巨乳",
    ]


def test_enrich_metadata_without_translator_returns_raw_metadata() -> None:
    gallery = parse_gdata_entry(SAMPLE_ENTRY)
    assert gallery is not None
    metadata = enrich_metadata(gallery, None)

    assert metadata["Category"] == "Doujinshi"
    assert metadata["Artist"] == "kamisiro ryu"
    assert "CategoryRaw" not in metadata
    assert "TagsRaw" not in metadata


def test_enrich_metadata_ignores_unloaded_translator() -> None:
    gallery = parse_gdata_entry(SAMPLE_ENTRY)
    assert gallery is not None
    metadata = enrich_metadata(gallery, TagTranslator())

    assert metadata["Category"] == "Doujinshi"
    assert "CategoryRaw" not in metadata


@pytest.mark.asyncio
async def test_sync_skips_network_while_cache_is_fresh(tmp_path: Path) -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url.host))
        if request.url.host == "api.github.com":
            return httpx.Response(200, json=_release_payload())
        return httpx.Response(200, content=_gzip_body(SAMPLE_DB))

    sync, client = _sync(tmp_path, handler)
    async with client:
        await sync.synchronize()
        result = await sync.synchronize()

    assert result.reason == "cache_fresh"
    assert result.from_cache is True
    assert calls == ["api.github.com", "example.invalid"]

