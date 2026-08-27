from pathlib import Path

import httpx
import pytest

from app.candidates.ingestor import CandidateIngestor
from app.db.database import Database
from app.exhentai.gdata_client import GDATA_ENDPOINT
from app.exhentai.service import ExHentaiService
from app.thumbnails import (
    THUMBNAIL_KIND_CANDIDATE_COVER,
    THUMBNAIL_VARIANT_CARD,
)
from app.thumbnails.identity import identity_hash


THUMB_URL = "https://ehgt.org/ab/cd/torrentcover-4108964-250-350-jpg_250.jpg"


GDATA_RESPONSE = {
    "gmetadata": [
        {
            "gid": 4108964,
            "token": "torrenttoken",
            "title": "Torrent Sample",
            "category": "Doujinshi",
            "filecount": "78",
            "filesize": 139262241,
            "torrentcount": "2",
            "torrents": [
                {
                    "hash": "1111111111111111111111111111111111111111",
                    "added": "1786299999",
                    "name": "[Sample] Book (resample).zip",
                    "tsize": "9000",
                    "fsize": "139262241",
                },
                {
                    "hash": "4acbd66e5d0518977ece30c343eb75c4ca92b031",
                    "added": "1786287412",
                    "name": "[Sample] Book.zip",
                    "tsize": "10119",
                    "fsize": "126838245",
                },
            ],
            "tags": ["artist:sample", "language:chinese"],
        }
    ]
}


async def make_candidate(database: Database, gid: int, token: str) -> None:
    await database.configure_telegram_source(
        source_type="CHANNEL",
        chat_id=-100777,
        display_name="Torrent Fixture",
        enabled=True,
        allowed_archive_formats=("zip",),
        max_attachment_size_mb=0,
    )
    await database.save_telegram_updates(
        [
            {
                "update_id": 900,
                "channel_post": {
                    "message_id": 900,
                    "date": 1_700_020_000,
                    "chat": {"id": -100777, "title": "Torrent Fixture"},
                    "text": f"Torrent Sample\nhttps://exhentai.org/g/{gid}/{token}/",
                },
            }
        ]
    )
    await CandidateIngestor(database).process_pending_updates()


def scraped_title(database: Database) -> str | None:
    """The `EXHENTAI`-sourced Title row, not the resolved value.

    `TELEGRAM` outranks `EXHENTAI` in the resolver, so a test that wants to
    know whether a scrape wrote has to look at the row the scrape owns.
    """
    with database._connect() as connection:  # noqa: SLF001
        row = connection.execute(
            "SELECT field_value FROM metadata_values WHERE candidate_id = 1 "
            "AND field_name = 'Title' AND value_source = 'EXHENTAI'"
        ).fetchone()
    return None if row is None else str(row[0])


def build_service(
    database: Database, tmp_path: Path, response: dict
) -> ExHentaiService:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == GDATA_ENDPOINT
        return httpx.Response(200, json=response)

    async def no_credentials():
        return None

    return ExHentaiService(
        database=database,
        work_path=tmp_path / "work",
        library_path=tmp_path / "library",
        credentials_provider=no_credentials,
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )


@pytest.mark.asyncio
async def test_gdata_torrent_availability_lands_on_the_candidate(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    await make_candidate(database, 4108964, "torrenttoken")
    service = build_service(database, tmp_path, GDATA_RESPONSE)

    metadata = await service.fetch_metadata_for_candidate(1)
    candidate = await database.get_candidate(1)

    assert metadata["Pages"] == "78"
    assert candidate is not None
    assert candidate.torrent_count == 2
    # The resample is newer and matches filesize exactly, and still loses.
    assert candidate.torrent_hash == "4acbd66e5d0518977ece30c343eb75c4ca92b031"


@pytest.mark.asyncio
async def test_a_gallery_without_torrents_records_zero_not_null(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    await make_candidate(database, 1655718, "notorrenttoken")
    response = {
        "gmetadata": [
            {
                "gid": 1655718,
                "token": "notorrenttoken",
                "title": "No Torrent Sample",
                "filecount": "15",
                "filesize": 145185851,
                "torrentcount": "0",
                "torrents": [],
                "tags": ["artist:sample"],
            }
        ]
    }
    service = build_service(database, tmp_path, response)
    before = await database.get_candidate(1)

    await service.fetch_metadata_for_candidate(1)
    after = await database.get_candidate(1)

    # NULL before the query and 0 after, so the router can tell 「未查询」 from
    # 「确认无种」 instead of retrying a gallery that will never have a torrent.
    assert before is not None
    assert before.torrent_count is None
    assert after is not None
    assert after.torrent_count == 0
    assert after.torrent_hash is None


@pytest.mark.asyncio
async def test_the_cover_url_and_its_cache_slot_are_written_together(
    tmp_path: Path,
) -> None:
    """A cover only becomes fetchable because the scrape vouched for it.

    The proxy endpoint accepts no URL, so this row is the sole admission
    point. If the two writes could drift apart, a candidate would show a
    cover link the endpoint refuses to serve.
    """
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    await make_candidate(database, 4108964, "torrenttoken")
    response = {
        "gmetadata": [
            {**GDATA_RESPONSE["gmetadata"][0], "thumb": THUMB_URL}
        ]
    }
    service = build_service(database, tmp_path, response)

    await service.fetch_metadata_for_candidate(1)

    digest = identity_hash(THUMB_URL, THUMBNAIL_VARIANT_CARD)
    with database._connect() as connection:  # noqa: SLF001
        stored_url = connection.execute(
            "SELECT thumb_url FROM candidates WHERE id = 1"
        ).fetchone()[0]
        slot = connection.execute(
            "SELECT source_url, kind, variant, state FROM thumbnails "
            "WHERE hash = ?",
            (digest,),
        ).fetchone()

    assert stored_url == THUMB_URL
    assert slot == (
        THUMB_URL,
        THUMBNAIL_KIND_CANDIDATE_COVER,
        THUMBNAIL_VARIANT_CARD,
        "PENDING",
    )


@pytest.mark.asyncio
async def test_a_gallery_without_a_thumb_opens_no_cache_slot(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    await make_candidate(database, 4108964, "torrenttoken")
    service = build_service(database, tmp_path, GDATA_RESPONSE)

    await service.fetch_metadata_for_candidate(1)

    with database._connect() as connection:  # noqa: SLF001
        assert (
            connection.execute("SELECT COUNT(*) FROM thumbnails").fetchone()[0]
            == 0
        )


@pytest.mark.asyncio
async def test_a_pinned_value_survives_a_later_scrape(tmp_path: Path) -> None:
    """`is_locked` exists for values the operator did not retype.

    `is_manual` already protects hand-entered text. A pinned value is one
    ExHentai supplied that the operator judged correct — nothing marks it as
    theirs, so without the second guard the next scrape overwrites it.
    """
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    await make_candidate(database, 4108964, "torrenttoken")
    service = build_service(database, tmp_path, GDATA_RESPONSE)
    await service.fetch_metadata_for_candidate(1)
    assert scraped_title(database) == "Torrent Sample"

    await database.set_metadata_lock(1, "operator", "Title", True)
    renamed = {
        "gmetadata": [
            {**GDATA_RESPONSE["gmetadata"][0], "title": "Renamed Upstream"}
        ]
    }
    await build_service(
        database, tmp_path, renamed
    ).fetch_metadata_for_candidate(1)

    # Asserted against the scraped row rather than the effective value, because
    # `TELEGRAM` outranks `EXHENTAI` — reading the resolved value would pass
    # even if the guard did nothing.
    assert scraped_title(database) == "Torrent Sample"

    # And releasing the pin lets the next scrape through.
    await database.set_metadata_lock(1, "operator", "Title", False)
    await build_service(
        database, tmp_path, renamed
    ).fetch_metadata_for_candidate(1)

    assert scraped_title(database) == "Renamed Upstream"


@pytest.mark.asyncio
async def test_a_lock_covers_every_row_for_the_field(tmp_path: Path) -> None:
    """The operator is pinning a field, not the row that happens to win today.

    Locking only the resolved row would let a later scrape land on an unlocked
    row of another source and change what the field resolves to.
    """
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    await make_candidate(database, 4108964, "torrenttoken")
    await build_service(
        database, tmp_path, GDATA_RESPONSE
    ).fetch_metadata_for_candidate(1)

    await database.set_metadata_lock(1, "operator", "Title", True)

    with database._connect() as connection:  # noqa: SLF001
        sources = connection.execute(
            "SELECT value_source, is_locked FROM metadata_values "
            "WHERE candidate_id = 1 AND field_name = 'Title' "
            "ORDER BY value_source"
        ).fetchall()

    assert len(sources) > 1, "fixture must store Title from two sources"
    assert all(row[1] == 1 for row in sources)


@pytest.mark.asyncio
async def test_locking_is_recorded_as_a_review_action(tmp_path: Path) -> None:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    await make_candidate(database, 4108964, "torrenttoken")
    await build_service(
        database, tmp_path, GDATA_RESPONSE
    ).fetch_metadata_for_candidate(1)

    await database.set_metadata_lock(1, "operator", "Title", True)

    with database._connect() as connection:  # noqa: SLF001
        action = connection.execute(
            "SELECT action, operator_name FROM review_actions "
            "WHERE candidate_id = 1 ORDER BY id DESC LIMIT 1"
        ).fetchone()

    assert action == ("LOCK_METADATA", "operator")


@pytest.mark.asyncio
async def test_locking_a_field_with_no_stored_value_is_refused(
    tmp_path: Path,
) -> None:
    """Nothing to pin is a caller mistake, not a silent no-op."""
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    await make_candidate(database, 4108964, "torrenttoken")

    with pytest.raises(LookupError):
        await database.set_metadata_lock(1, "operator", "Artist", True)
