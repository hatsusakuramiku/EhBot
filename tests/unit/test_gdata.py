import httpx
import pytest

from app.exhentai.gdata import (
    GalleryTorrent,
    extract_gallery_ref,
    gallery_data_to_metadata,
    parse_gdata_entry,
    parse_tag_list,
    select_torrent,
)
from app.exhentai.gdata_client import (
    GDATA_ENDPOINT,
    GdataClient,
    GdataError,
    MAX_GALLERIES_PER_REQUEST,
)


SAMPLE_ENTRY = {
    "gid": 4116328,
    "token": "c722b9009c",
    "title": "[Kuroneko Akaribon (Kamisiro Ryu)] Hojo Reina no kyoji [Chinese]",
    "title_jpn": "[\u9ed2\u306d\u3053\u8d64\u30ea\u30dc\u30f3] \u8c4a\u9952\u73b2\u5948\u306e\u77dc\u6301",
    "category": "Doujinshi",
    "uploader": "Amerins",
    "posted": "1786592798",
    "filecount": "51",
    "filesize": 288018922,
    "expunged": False,
    "rating": "4.47",
    "thumb": "https://ehgt.org/w/02/571/03492-ir1958xo.webp",
    "tags": [
        "language:chinese",
        "language:translated",
        "parody:original",
        "group:kuroneko akaribon",
        "artist:kamisiro ryu",
        "character:reina hojo",
        "female:anal",
        "female:big breasts",
        "mixed:group",
    ],
}


def test_parse_tag_list_groups_by_namespace() -> None:
    tags = parse_tag_list(SAMPLE_ENTRY["tags"])
    assert tags.get("artist") == ("kamisiro ryu",)
    assert tags.get("group") == ("kuroneko akaribon",)
    assert tags.get("language") == ("chinese", "translated")
    assert tags.get("female") == ("anal", "big breasts")
    assert tags.first("parody") == "original"


def test_parse_tag_list_handles_unnamespaced_and_duplicates() -> None:
    tags = parse_tag_list(["solo", "artist:x", "artist:x", "  ", "other:y"])
    assert tags.get("misc") == ("solo",)
    assert tags.get("artist") == ("x",)
    assert tags.get("other") == ("y",)


def test_parse_gdata_entry_extracts_structured_fields() -> None:
    gallery = parse_gdata_entry(SAMPLE_ENTRY)
    assert gallery is not None
    assert gallery.gid == 4116328
    assert gallery.artists == ("kamisiro ryu",)
    assert gallery.groups == ("kuroneko akaribon",)
    assert gallery.characters == ("reina hojo",)
    assert gallery.rating == 4.47
    assert gallery.file_count == 51
    # "translated" is a marker, not the content language.
    assert gallery.primary_language == "chinese"


def test_parse_gdata_entry_rejects_error_entries() -> None:
    assert parse_gdata_entry({"error": "Key missing"}) is None
    assert parse_gdata_entry({"gid": 1}) is None


def test_gallery_data_to_metadata_populates_expected_fields() -> None:
    gallery = parse_gdata_entry(SAMPLE_ENTRY)
    assert gallery is not None
    metadata = gallery_data_to_metadata(gallery)
    assert metadata["Artist"] == "kamisiro ryu"
    assert metadata["Group"] == "kuroneko akaribon"
    assert metadata["Parody"] == "original"
    assert metadata["Character"] == "reina hojo"
    assert metadata["Language"] == "chinese"
    assert metadata["Category"] == "Doujinshi"
    assert metadata["Rating"] == "4.47"
    assert metadata["Pages"] == "51"
    assert metadata["Uploader"] == "Amerins"
    assert "female:anal" in metadata["Tags"]
    assert metadata["Web"].endswith("/g/4116328/c722b9009c/")


def test_extract_gallery_ref_matches_both_domains() -> None:
    assert extract_gallery_ref(
        "see https://exhentai.org/g/4116328/c722b9009c/ now"
    ) == (4116328, "c722b9009c")
    assert extract_gallery_ref("https://e-hentai.org/g/123/abc/") == (
        123,
        "abc",
    )
    assert extract_gallery_ref("no gallery here") is None


@pytest.mark.asyncio
async def test_gdata_client_fetches_one_gallery() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == GDATA_ENDPOINT
        return httpx.Response(200, json={"gmetadata": [SAMPLE_ENTRY]})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        gallery = await GdataClient(client).fetch_one(4116328, "c722b9009c")

    assert gallery.title_jpn is not None
    assert gallery.artists == ("kamisiro ryu",)


@pytest.mark.asyncio
async def test_gdata_client_batches_requests_at_the_api_limit() -> None:
    seen_batch_sizes: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        import json

        payload = json.loads(request.content.decode("utf-8"))
        seen_batch_sizes.append(len(payload["gidlist"]))
        entries = [
            {**SAMPLE_ENTRY, "gid": gid, "token": token}
            for gid, token in payload["gidlist"]
        ]
        return httpx.Response(200, json={"gmetadata": entries})

    refs = [(index, f"token{index}") for index in range(1, 31)]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        results = await GdataClient(client).fetch_many(refs)

    assert seen_batch_sizes == [MAX_GALLERIES_PER_REQUEST, 5]
    assert len(results) == 30


@pytest.mark.asyncio
async def test_gdata_client_reports_rate_limiting() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(GdataError) as captured:
            await GdataClient(client).fetch_one(1, "t")

    assert captured.value.code == "EXHENTAI_GDATA_RATE_LIMITED"


@pytest.mark.asyncio
async def test_gdata_client_reports_missing_gallery() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"gmetadata": []})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(GdataError) as captured:
            await GdataClient(client).fetch_one(1, "t")

    assert captured.value.code == "EXHENTAI_GDATA_NOT_FOUND"


# Measured 2026-08-21 against gid 4108964: the torrent holds the uploader's
# own archive, so its fsize does not match the gallery filesize.
TORRENT_ENTRY = {
    "gid": 4108964,
    "token": "a1b2c3d4e5",
    "title": "Sample Gallery",
    "filecount": "78",
    "filesize": 139262241,
    "torrentcount": "1",
    "torrents": [
        {
            "hash": "4acbd66e5d0518977ece30c343eb75c4ca92b031",
            "added": "1786287412",
            "name": "[Sample] Book.zip",
            "tsize": "10119",
            "fsize": "126838245",
        }
    ],
    "tags": ["artist:sample"],
}


def test_parse_gdata_entry_reads_the_torrent_list() -> None:
    gallery = parse_gdata_entry(TORRENT_ENTRY)
    assert gallery is not None
    assert gallery.torrent_count == 1
    assert len(gallery.torrents) == 1
    torrent = gallery.torrents[0]
    assert torrent.hash == "4acbd66e5d0518977ece30c343eb75c4ca92b031"
    assert torrent.added == 1786287412
    assert torrent.fsize == 126838245
    assert gallery.best_torrent is torrent


def test_a_gallery_without_torrents_reports_zero() -> None:
    # gid 1655718 really answers torrentcount 0, so this is not a corner case.
    gallery = parse_gdata_entry(SAMPLE_ENTRY)
    assert gallery is not None
    assert gallery.torrent_count == 0
    assert gallery.torrents == ()
    assert gallery.best_torrent is None


def test_malformed_torrent_hashes_are_dropped() -> None:
    gallery = parse_gdata_entry(
        {
            **TORRENT_ENTRY,
            "torrents": [
                {"hash": "short", "name": "a.zip"},
                {"hash": "z" * 40, "name": "b.zip"},
                {"name": "c.zip"},
                "not-a-dict",
                {"hash": "A" * 40, "name": "d.zip", "fsize": "1"},
            ],
        }
    )
    assert gallery is not None
    assert [torrent.hash for torrent in gallery.torrents] == ["a" * 40]


def test_resampled_torrents_lose_to_a_full_one() -> None:
    resample = GalleryTorrent(
        hash="a" * 40,
        name="[Sample] Book (resample).zip",
        added=2_000_000_000,
        tsize=100,
        fsize=139_262_241,
    )
    full = GalleryTorrent(
        hash="b" * 40,
        name="[Sample] Book.zip",
        added=1_000_000_000,
        tsize=100,
        fsize=126_838_245,
    )

    # The resample is both newer and closer in size, and still loses.
    assert select_torrent((resample, full), 139_262_241) is full


def test_the_closest_file_size_wins_then_the_newest() -> None:
    far = GalleryTorrent(
        hash="a" * 40, name="a.zip", added=2_000_000_000, tsize=1, fsize=1_000
    )
    near = GalleryTorrent(
        hash="b" * 40, name="b.zip", added=1_000_000_000, tsize=1, fsize=99_000
    )
    assert select_torrent((far, near), 100_000) is near

    older = GalleryTorrent(
        hash="c" * 40, name="c.zip", added=1_000, tsize=1, fsize=50_000
    )
    newer = GalleryTorrent(
        hash="d" * 40, name="d.zip", added=2_000, tsize=1, fsize=50_000
    )
    assert select_torrent((older, newer), 50_000) is newer


def test_only_resampled_torrents_still_yield_a_choice() -> None:
    resample = GalleryTorrent(
        hash="a" * 40, name="x (resample).zip", added=1, tsize=1, fsize=10
    )
    assert select_torrent((resample,), 100) is resample
