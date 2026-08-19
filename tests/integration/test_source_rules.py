from pathlib import Path
import sqlite3

import pytest

from app.candidates.ingestor import CandidateIngestor
from app.db.database import Database


def archive_update(
    *,
    update_id: int,
    chat_id: int,
    file_name: str = "Allowed Comic.zip",
    file_size: int | None = 1_000_000,
) -> dict:
    document = {
        "file_id": f"file-{update_id}",
        "file_unique_id": f"unique-{update_id}",
        "file_name": file_name,
    }
    if file_size is not None:
        document["file_size"] = file_size
    return {
        "update_id": update_id,
        "channel_post": {
            "message_id": update_id,
            "date": 1_700_100_000 + update_id,
            "chat": {"id": chat_id, "title": "Rule Fixture"},
            "document": document,
        },
    }


@pytest.mark.asyncio
async def test_unknown_source_is_discovered_but_not_ingested(tmp_path: Path) -> None:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    await database.save_telegram_updates(
        [archive_update(update_id=500, chat_id=-100500)]
    )

    result = await CandidateIngestor(database).process_pending_updates()
    sources = await database.list_telegram_sources()

    assert result.ignored_updates == 1
    assert await database.list_candidates() == []
    assert len(sources) == 1
    assert sources[0].chat_id == -100500
    assert sources[0].enabled is False


@pytest.mark.asyncio
async def test_enabled_source_accepts_allowed_archive(tmp_path: Path) -> None:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    await database.configure_telegram_source(
        source_type="CHANNEL",
        chat_id=-100501,
        display_name="Allowed Channel",
        enabled=True,
        allowed_archive_formats=("zip", "cbz"),
        max_attachment_size_mb=10,
    )
    await database.save_telegram_updates(
        [archive_update(update_id=501, chat_id=-100501)]
    )

    result = await CandidateIngestor(database).process_pending_updates()
    candidates = await database.list_candidates()

    assert result.created_candidates == 1
    assert len(candidates) == 1
    assert candidates[0].status == "PENDING_REVIEW"


@pytest.mark.asyncio
async def test_disallowed_archive_format_is_ignored(tmp_path: Path) -> None:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    await database.configure_telegram_source(
        source_type="CHANNEL",
        chat_id=-100502,
        display_name="ZIP Only",
        enabled=True,
        allowed_archive_formats=("zip",),
        max_attachment_size_mb=0,
    )
    await database.save_telegram_updates(
        [
            archive_update(
                update_id=502,
                chat_id=-100502,
                file_name="Blocked Comic.rar",
            )
        ]
    )

    result = await CandidateIngestor(database).process_pending_updates()

    assert result.ignored_updates == 1
    assert await database.list_candidates() == []


@pytest.mark.asyncio
async def test_unknown_attachment_size_requires_information(tmp_path: Path) -> None:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    await database.configure_telegram_source(
        source_type="CHANNEL",
        chat_id=-100503,
        display_name="Sized Channel",
        enabled=True,
        allowed_archive_formats=("zip",),
        max_attachment_size_mb=10,
    )
    await database.save_telegram_updates(
        [archive_update(update_id=503, chat_id=-100503, file_size=None)]
    )

    result = await CandidateIngestor(database).process_pending_updates()
    needs_info = await database.list_candidates(status="NEEDS_INFO")

    assert result.created_candidates == 1
    assert len(needs_info) == 1
    assert needs_info[0].filter_result == "NEEDS_INFO"
    with sqlite3.connect(database.path) as connection:
        processing_result = connection.execute(
            "SELECT processing_result FROM telegram_bot_updates WHERE update_id = 503"
        ).fetchone()[0]
    assert processing_result == "NEEDS_INFO"


@pytest.mark.asyncio
async def test_oversized_attachment_is_ignored(tmp_path: Path) -> None:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    await database.configure_telegram_source(
        source_type="CHANNEL",
        chat_id=-100504,
        display_name="Limited Channel",
        enabled=True,
        allowed_archive_formats=("zip",),
        max_attachment_size_mb=1,
    )
    await database.save_telegram_updates(
        [archive_update(update_id=504, chat_id=-100504, file_size=2_000_000)]
    )

    result = await CandidateIngestor(database).process_pending_updates()

    assert result.ignored_updates == 1
    assert await database.list_candidates() == []


@pytest.mark.asyncio
async def test_preview_without_title_requires_information(tmp_path: Path) -> None:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    await database.configure_telegram_source(
        source_type="PRIVATE_CHAT",
        chat_id=505,
        display_name="Allowed Sender",
        enabled=True,
        allowed_archive_formats=(),
        max_attachment_size_mb=0,
    )
    await database.save_telegram_updates(
        [
            {
                "update_id": 505,
                "message": {
                    "message_id": 505,
                    "date": 1_700_100_505,
                    "chat": {"id": 505, "username": "allowed_sender"},
                    "from": {"id": 505},
                    "photo": [
                        {
                            "file_id": "untitled-photo",
                            "file_unique_id": "untitled-photo-unique",
                            "width": 800,
                            "height": 1200,
                            "file_size": 200_000,
                        }
                    ],
                },
            }
        ]
    )

    await CandidateIngestor(database).process_pending_updates()
    needs_info = await database.list_candidates(status="NEEDS_INFO")

    assert len(needs_info) == 1
    assert needs_info[0].title is None


@pytest.mark.asyncio
async def test_archive_title_resolves_grouped_preview_needs_info(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    await database.configure_telegram_source(
        source_type="CHANNEL",
        chat_id=-100506,
        display_name="Grouped Source",
        enabled=True,
        allowed_archive_formats=("zip",),
        max_attachment_size_mb=0,
    )
    await database.save_telegram_updates(
        [
            {
                "update_id": 506,
                "channel_post": {
                    "message_id": 506,
                    "date": 1_700_100_506,
                    "chat": {"id": -100506, "title": "Grouped Source"},
                    "media_group_id": "needs-info-group",
                    "photo": [
                        {
                            "file_id": "grouped-photo",
                            "file_unique_id": "grouped-photo-unique",
                            "width": 800,
                            "height": 1200,
                            "file_size": 200_000,
                        }
                    ],
                },
            },
            {
                "update_id": 507,
                "channel_post": {
                    "message_id": 507,
                    "date": 1_700_100_507,
                    "chat": {"id": -100506, "title": "Grouped Source"},
                    "media_group_id": "needs-info-group",
                    "document": {
                        "file_id": "grouped-archive",
                        "file_unique_id": "grouped-archive-unique",
                        "file_name": "Resolved Title.zip",
                        "file_size": 1_000_000,
                    },
                },
            },
        ]
    )

    await CandidateIngestor(database).process_pending_updates()
    pending = await database.list_candidates()

    assert len(pending) == 1
    assert pending[0].title == "Resolved Title"
    assert await database.list_candidates(status="NEEDS_INFO") == []


@pytest.mark.asyncio
async def test_rejected_edit_removes_stale_candidate(tmp_path: Path) -> None:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    await database.configure_telegram_source(
        source_type="CHANNEL",
        chat_id=-100507,
        display_name="ZIP Source",
        enabled=True,
        allowed_archive_formats=("zip",),
        max_attachment_size_mb=0,
    )
    original = archive_update(update_id=508, chat_id=-100507)
    await database.save_telegram_updates([original])
    await CandidateIngestor(database).process_pending_updates()
    edited_message = dict(original["channel_post"])
    edited_message["document"] = {
        **edited_message["document"],
        "file_name": "Now Blocked.rar",
    }
    await database.save_telegram_updates(
        [{"update_id": 509, "edited_channel_post": edited_message}]
    )

    result = await CandidateIngestor(database).process_pending_updates()

    assert result.ignored_updates == 1
    assert await database.list_candidates() == []


@pytest.mark.asyncio
async def test_discovery_preserves_admin_source_name(tmp_path: Path) -> None:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    await database.configure_telegram_source(
        source_type="CHANNEL",
        chat_id=-100508,
        display_name="My Source Name",
        enabled=True,
        allowed_archive_formats=("zip",),
        max_attachment_size_mb=0,
    )
    await database.save_telegram_updates(
        [archive_update(update_id=510, chat_id=-100508)]
    )

    await CandidateIngestor(database).process_pending_updates()

    assert (await database.list_telegram_sources())[0].display_name == "My Source Name"


@pytest.mark.asyncio
async def test_invalid_saved_rules_fail_closed(tmp_path: Path) -> None:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    await database.configure_telegram_source(
        source_type="CHANNEL",
        chat_id=-100509,
        display_name="Broken Rules",
        enabled=True,
        allowed_archive_formats=("zip",),
        max_attachment_size_mb=0,
    )
    with sqlite3.connect(database.path) as connection:
        connection.execute(
            "UPDATE telegram_sources SET rules_json = 'not-json' "
            "WHERE chat_id = -100509"
        )
    await database.save_telegram_updates(
        [archive_update(update_id=511, chat_id=-100509)]
    )

    result = await CandidateIngestor(database).process_pending_updates()
    sources = await database.list_telegram_sources()

    assert result.ignored_updates == 1
    assert sources[0].enabled is False
    assert await database.list_candidates() == []
