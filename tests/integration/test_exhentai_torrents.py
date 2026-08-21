from pathlib import Path

import httpx
import pytest

from app.candidates.ingestor import CandidateIngestor
from app.db.database import Database
from app.exhentai.gdata_client import GDATA_ENDPOINT
from app.exhentai.service import ExHentaiService


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
