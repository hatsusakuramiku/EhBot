from pathlib import Path

import pytest

from app.candidates.ingestor import CandidateIngestor
from app.db.database import Database


async def allow_sources(database: Database, *chat_ids: int) -> None:
    for chat_id in chat_ids:
        await database.configure_telegram_source(
            source_type="CHANNEL" if chat_id < 0 else "PRIVATE_CHAT",
            chat_id=chat_id,
            display_name=f"Fixture {chat_id}",
            enabled=True,
            allowed_archive_formats=("zip", "rar", "7z", "cbz"),
            max_attachment_size_mb=0,
        )


@pytest.mark.asyncio
async def test_photo_preview_update_becomes_a_pending_review_candidate(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    await allow_sources(database, -100123)
    await database.save_telegram_updates(
        [
            {
                "update_id": 200,
                "channel_post": {
                    "message_id": 10,
                    "date": 1_700_000_000,
                    "chat": {"id": -100123, "title": "Fixture Channel"},
                    "caption": "Fixture Comic\nArtist: Example",
                    "photo": [
                        {
                            "file_id": "photo-small",
                            "file_unique_id": "photo-unique-small",
                            "width": 320,
                            "height": 480,
                            "file_size": 12_000,
                        },
                        {
                            "file_id": "photo-large",
                            "file_unique_id": "photo-unique-large",
                            "width": 1280,
                            "height": 1920,
                            "file_size": 240_000,
                        },
                    ],
                },
            }
        ]
    )

    result = await CandidateIngestor(database).process_pending_updates()
    candidates = await database.list_candidates()

    assert result.processed_updates == 1
    assert result.created_candidates == 1
    assert len(candidates) == 1
    assert candidates[0].status == "PENDING_REVIEW"
    assert candidates[0].filter_result == "ACCEPT"
    assert candidates[0].title == "Fixture Comic"
    assert candidates[0].message_count == 1


@pytest.mark.asyncio
async def test_exhentai_link_update_becomes_a_review_candidate(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    await allow_sources(database, 501)
    await database.save_telegram_updates(
        [
            {
                "update_id": 201,
                "message": {
                    "message_id": 11,
                    "date": 1_700_000_001,
                    "chat": {"id": 501, "username": "fixture_sender"},
                    "from": {"id": 501},
                    "text": "https://exhentai.org/g/12345/abcDEF123/",
                },
            }
        ]
    )

    result = await CandidateIngestor(database).process_pending_updates()
    candidate = (await database.list_candidates())[0]

    assert result.created_candidates == 1
    assert candidate.status == "PENDING_REVIEW"
    assert candidate.title == "ExHentai #12345"
    assert candidate.ex_gid == 12345
    assert candidate.ex_gallery_token == "abcDEF123"


@pytest.mark.asyncio
async def test_archive_only_update_uses_filename_as_candidate_title(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    await allow_sources(database, -100123)
    await database.save_telegram_updates(
        [
            {
                "update_id": 202,
                "channel_post": {
                    "message_id": 12,
                    "date": 1_700_000_002,
                    "chat": {"id": -100123, "title": "Fixture Channel"},
                    "document": {
                        "file_id": "archive-file",
                        "file_unique_id": "archive-unique",
                        "file_name": "Fixture Archive.zip",
                        "mime_type": "application/zip",
                        "file_size": 15_000_000,
                    },
                },
            }
        ]
    )

    result = await CandidateIngestor(database).process_pending_updates()
    candidate = (await database.list_candidates())[0]

    assert result.created_candidates == 1
    assert candidate.status == "PENDING_REVIEW"
    assert candidate.title == "Fixture Archive"


@pytest.mark.asyncio
async def test_messages_in_same_media_group_share_one_candidate(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    await allow_sources(database, -100123)
    await database.save_telegram_updates(
        [
            {
                "update_id": 203,
                "channel_post": {
                    "message_id": 20,
                    "date": 1_700_000_010,
                    "chat": {"id": -100123, "title": "Fixture Channel"},
                    "media_group_id": "group-42",
                    "caption": "Grouped Comic",
                    "photo": [
                        {
                            "file_id": "photo",
                            "file_unique_id": "photo-unique",
                            "width": 800,
                            "height": 1200,
                        }
                    ],
                },
            },
            {
                "update_id": 204,
                "channel_post": {
                    "message_id": 21,
                    "date": 1_700_000_011,
                    "chat": {"id": -100123, "title": "Fixture Channel"},
                    "media_group_id": "group-42",
                    "caption": "https://exhentai.org/g/24680/groupToken/",
                    "document": {
                        "file_id": "archive",
                        "file_unique_id": "archive-unique",
                        "file_name": "Grouped Comic.zip",
                    },
                },
            },
        ]
    )

    result = await CandidateIngestor(database).process_pending_updates()
    candidates = await database.list_candidates()

    assert result.processed_updates == 2
    assert result.created_candidates == 1
    assert len(candidates) == 1
    assert candidates[0].message_count == 2
    assert candidates[0].title == "Grouped Comic"
    assert candidates[0].ex_gid == 24680
    assert candidates[0].ex_gallery_token == "groupToken"


@pytest.mark.asyncio
async def test_unrelated_text_update_is_ignored_once(tmp_path: Path) -> None:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    await database.save_telegram_updates(
        [
            {
                "update_id": 209,
                "message": {
                    "message_id": 50,
                    "date": 1_700_000_040,
                    "chat": {"id": 700, "username": "fixture_sender"},
                    "from": {"id": 700},
                    "text": "This message is unrelated to comics.",
                },
            }
        ]
    )

    first = await CandidateIngestor(database).process_pending_updates()
    second = await CandidateIngestor(database).process_pending_updates()

    assert first.processed_updates == 1
    assert first.ignored_updates == 1
    assert second.processed_updates == 0
    assert await database.list_candidates() == []


@pytest.mark.asyncio
async def test_same_exhentai_gallery_reference_merges_across_chats(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    gallery_url = "https://exhentai.org/g/67890/tokenXYZ/"
    await allow_sources(database, 600, -100999)
    await database.save_telegram_updates(
        [
            {
                "update_id": 205,
                "message": {
                    "message_id": 30,
                    "date": 1_700_000_020,
                    "chat": {"id": 600, "username": "first_sender"},
                    "from": {"id": 600},
                    "text": gallery_url,
                },
            },
            {
                "update_id": 206,
                "channel_post": {
                    "message_id": 31,
                    "date": 1_700_000_021,
                    "chat": {"id": -100999, "title": "Other Channel"},
                    "caption": f"Gallery Title\n{gallery_url}",
                    "photo": [
                        {
                            "file_id": "gallery-photo",
                            "file_unique_id": "gallery-photo-unique",
                            "width": 800,
                            "height": 1200,
                        }
                    ],
                },
            },
        ]
    )

    result = await CandidateIngestor(database).process_pending_updates()
    candidates = await database.list_candidates()

    assert result.created_candidates == 1
    assert len(candidates) == 1
    assert candidates[0].message_count == 2
    assert candidates[0].ex_gid == 67890
    assert candidates[0].title == "Gallery Title"


@pytest.mark.asyncio
async def test_reply_message_joins_the_referenced_candidate(tmp_path: Path) -> None:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    await allow_sources(database, -100123)
    await database.save_telegram_updates(
        [
            {
                "update_id": 207,
                "channel_post": {
                    "message_id": 40,
                    "date": 1_700_000_030,
                    "chat": {"id": -100123, "title": "Fixture Channel"},
                    "caption": "Reply Comic",
                    "photo": [
                        {
                            "file_id": "reply-photo",
                            "file_unique_id": "reply-photo-unique",
                            "width": 800,
                            "height": 1200,
                        }
                    ],
                },
            },
            {
                "update_id": 208,
                "channel_post": {
                    "message_id": 41,
                    "date": 1_700_000_031,
                    "chat": {"id": -100123, "title": "Fixture Channel"},
                    "reply_to_message": {"message_id": 40},
                    "document": {
                        "file_id": "reply-archive",
                        "file_unique_id": "reply-archive-unique",
                        "file_name": "Reply Comic.7z",
                    },
                },
            },
        ]
    )

    result = await CandidateIngestor(database).process_pending_updates()
    candidates = await database.list_candidates()

    assert result.created_candidates == 1
    assert len(candidates) == 1
    assert candidates[0].message_count == 2


@pytest.mark.asyncio
async def test_adjacent_preview_and_archive_with_same_title_are_merged(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    await allow_sources(database, -100321)
    await database.save_telegram_updates(
        [
            {
                "update_id": 210,
                "channel_post": {
                    "message_id": 100,
                    "date": 1_700_001_000,
                    "chat": {"id": -100321, "title": "Adjacent Channel"},
                    "caption": "Adjacent Comic",
                    "photo": [
                        {
                            "file_id": "adjacent-photo",
                            "file_unique_id": "adjacent-photo-unique",
                            "width": 800,
                            "height": 1200,
                        }
                    ],
                },
            },
            {
                "update_id": 211,
                "channel_post": {
                    "message_id": 101,
                    "date": 1_700_001_060,
                    "chat": {"id": -100321, "title": "Adjacent Channel"},
                    "document": {
                        "file_id": "adjacent-archive",
                        "file_unique_id": "adjacent-archive-unique",
                        "file_name": "Adjacent Comic.zip",
                    },
                },
            },
        ]
    )

    result = await CandidateIngestor(database).process_pending_updates()
    candidates = await database.list_candidates()

    assert result.created_candidates == 1
    assert len(candidates) == 1
    assert candidates[0].message_count == 2


@pytest.mark.asyncio
async def test_malformed_update_isolated_without_blocking_later_updates(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    await allow_sources(database, 801)
    await database.save_telegram_updates(
        [
            {
                "update_id": 212,
                "message": {
                    "message_id": 110,
                    "date": 1_700_002_000,
                    "chat": {"id": 800, "username": "broken_sender"},
                    "photo": [
                        {
                            "file_unique_id": "broken-photo",
                            "width": 800,
                            "height": 1200,
                        }
                    ],
                },
            },
            {
                "update_id": 213,
                "message": {
                    "message_id": 111,
                    "date": 1_700_002_001,
                    "chat": {"id": 801, "username": "valid_sender"},
                    "from": {"id": 801},
                    "text": "https://exhentai.org/g/11223/validToken/",
                },
            },
        ]
    )

    first = await CandidateIngestor(database).process_pending_updates()
    second = await CandidateIngestor(database).process_pending_updates()

    assert first.processed_updates == 2
    assert first.failed_updates == 1
    assert second.processed_updates == 0
    assert (await database.list_candidates())[0].ex_gid == 11223


@pytest.mark.asyncio
async def test_edited_message_updates_existing_candidate_content(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    await allow_sources(database, -100456)
    original_message = {
        "message_id": 120,
        "date": 1_700_003_000,
        "chat": {"id": -100456, "title": "Edit Channel"},
        "caption": "Original Title",
        "photo": [
            {
                "file_id": "edit-photo",
                "file_unique_id": "edit-photo-unique",
                "width": 800,
                "height": 1200,
            }
        ],
    }
    await database.save_telegram_updates(
        [{"update_id": 214, "channel_post": original_message}]
    )
    await CandidateIngestor(database).process_pending_updates()
    edited_message = {
        **original_message,
        "edit_date": 1_700_003_030,
        "caption": "Edited Title",
    }
    await database.save_telegram_updates(
        [{"update_id": 215, "edited_channel_post": edited_message}]
    )

    result = await CandidateIngestor(database).process_pending_updates()
    candidate = await database.get_candidate(1)

    assert result.processed_updates == 1
    assert candidate is not None
    assert candidate.title == "Edited Title"
    assert len(candidate.messages) == 1
    assert candidate.messages[0].message_text == "Edited Title"


@pytest.mark.asyncio
async def test_non_adjacent_same_title_messages_remain_separate(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    await allow_sources(database, -100567)
    await database.save_telegram_updates(
        [
            {
                "update_id": 216,
                "channel_post": {
                    "message_id": 200,
                    "date": 1_700_004_000,
                    "chat": {"id": -100567, "title": "Busy Channel"},
                    "caption": "Repeated Title",
                    "photo": [
                        {
                            "file_id": "gap-photo",
                            "file_unique_id": "gap-photo-unique",
                            "width": 800,
                            "height": 1200,
                        }
                    ],
                },
            },
            {
                "update_id": 217,
                "channel_post": {
                    "message_id": 202,
                    "date": 1_700_004_060,
                    "chat": {"id": -100567, "title": "Busy Channel"},
                    "document": {
                        "file_id": "gap-archive",
                        "file_unique_id": "gap-archive-unique",
                        "file_name": "Repeated Title.zip",
                    },
                },
            },
        ]
    )

    result = await CandidateIngestor(database).process_pending_updates()
    candidates = await database.list_candidates()

    assert result.created_candidates == 2
    assert len(candidates) == 2


@pytest.mark.asyncio
async def test_edit_keeps_original_candidate_when_media_group_changes(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    await allow_sources(database, -100678)
    first_message = {
        "message_id": 210,
        "date": 1_700_005_000,
        "chat": {"id": -100678, "title": "Edit Group Channel"},
        "media_group_id": "group-a",
        "caption": "First Candidate",
        "photo": [
            {
                "file_id": "group-edit-photo",
                "file_unique_id": "group-edit-photo-unique",
                "width": 800,
                "height": 1200,
            }
        ],
    }
    await database.save_telegram_updates(
        [
            {"update_id": 218, "channel_post": first_message},
            {
                "update_id": 219,
                "channel_post": {
                    "message_id": 220,
                    "date": 1_700_005_010,
                    "chat": {"id": -100678, "title": "Edit Group Channel"},
                    "media_group_id": "group-b",
                    "document": {
                        "file_id": "group-b-archive",
                        "file_unique_id": "group-b-archive-unique",
                        "file_name": "Second Candidate.zip",
                    },
                },
            },
        ]
    )
    await CandidateIngestor(database).process_pending_updates()
    await database.save_telegram_updates(
        [
            {
                "update_id": 220,
                "edited_channel_post": {
                    **first_message,
                    "media_group_id": "group-b",
                    "caption": "Edited First Candidate",
                },
            }
        ]
    )

    await CandidateIngestor(database).process_pending_updates()
    await database.save_telegram_updates(
        [
            {
                "update_id": 225,
                "channel_post": {
                    "message_id": 221,
                    "date": 1_700_005_020,
                    "chat": {"id": -100678, "title": "Edit Group Channel"},
                    "media_group_id": "group-b",
                    "document": {
                        "file_id": "group-b-second-archive",
                        "file_unique_id": "group-b-second-archive-unique",
                        "file_name": "Second Candidate.cbz",
                    },
                },
            }
        ]
    )
    await CandidateIngestor(database).process_pending_updates()
    candidates = await database.list_candidates()

    assert len(candidates) == 2
    assert sorted(candidate.message_count for candidate in candidates) == [1, 2]
    assert {candidate.title for candidate in candidates} == {
        "Edited First Candidate",
        "Second Candidate",
    }


@pytest.mark.asyncio
async def test_edit_replaces_gallery_identity_and_stale_explicit_title(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    await allow_sources(database, -100789)
    original_message = {
        "message_id": 230,
        "date": 1_700_006_000,
        "chat": {"id": -100789, "title": "Edit Gallery Channel"},
        "caption": "Old Explicit Title\nhttps://exhentai.org/g/11111/oldToken/",
        "photo": [
            {
                "file_id": "gallery-edit-photo",
                "file_unique_id": "gallery-edit-photo-unique",
                "width": 800,
                "height": 1200,
            }
        ],
    }
    await database.save_telegram_updates(
        [{"update_id": 221, "channel_post": original_message}]
    )
    await CandidateIngestor(database).process_pending_updates()
    await database.save_telegram_updates(
        [
            {
                "update_id": 222,
                "edited_channel_post": {
                    **original_message,
                    "caption": "https://exhentai.org/g/22222/newToken/",
                },
            }
        ]
    )

    await CandidateIngestor(database).process_pending_updates()
    candidate = await database.get_candidate(1)

    assert candidate is not None
    assert candidate.title == "ExHentai #22222"
    assert candidate.ex_gid == 22222
    assert candidate.ex_gallery_token == "newToken"


@pytest.mark.asyncio
async def test_edit_without_candidate_content_removes_stale_candidate(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    await allow_sources(database, -100890)
    original_message = {
        "message_id": 240,
        "date": 1_700_007_000,
        "chat": {"id": -100890, "title": "Removal Channel"},
        "caption": "Candidate To Remove",
        "photo": [
            {
                "file_id": "removal-photo",
                "file_unique_id": "removal-photo-unique",
                "width": 800,
                "height": 1200,
            }
        ],
    }
    await database.save_telegram_updates(
        [{"update_id": 223, "channel_post": original_message}]
    )
    await CandidateIngestor(database).process_pending_updates()
    await database.save_telegram_updates(
        [
            {
                "update_id": 224,
                "edited_channel_post": {
                    **original_message,
                    "caption": "No longer a candidate",
                    "photo": [],
                },
            }
        ]
    )

    result = await CandidateIngestor(database).process_pending_updates()

    assert result.ignored_updates == 1
    assert await database.list_candidates() == []


@pytest.mark.asyncio
async def test_edit_removal_rebuilds_metadata_from_remaining_message(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    await allow_sources(database, -100901)
    first_message = {
        "message_id": 250,
        "date": 1_700_008_000,
        "chat": {"id": -100901, "title": "Rebuild Channel"},
        "media_group_id": "rebuild-group",
        "caption": "Old Preferred Title",
        "photo": [
            {
                "file_id": "rebuild-photo",
                "file_unique_id": "rebuild-photo-unique",
                "width": 800,
                "height": 1200,
            }
        ],
    }
    await database.save_telegram_updates(
        [
            {"update_id": 226, "channel_post": first_message},
            {
                "update_id": 227,
                "channel_post": {
                    "message_id": 251,
                    "date": 1_700_008_010,
                    "chat": {"id": -100901, "title": "Rebuild Channel"},
                    "media_group_id": "rebuild-group",
                    "caption": "https://exhentai.org/g/33333/survivorToken/",
                    "document": {
                        "file_id": "rebuild-archive",
                        "file_unique_id": "rebuild-archive-unique",
                        "file_name": "Remaining Archive.zip",
                    },
                },
            },
        ]
    )
    await CandidateIngestor(database).process_pending_updates()
    await database.save_telegram_updates(
        [
            {
                "update_id": 228,
                "edited_channel_post": {
                    **first_message,
                    "caption": "No longer a candidate",
                    "photo": [],
                },
            }
        ]
    )

    await CandidateIngestor(database).process_pending_updates()
    candidate = await database.get_candidate(1)

    assert candidate is not None
    assert candidate.title == "Remaining Archive"
    assert candidate.ex_gid == 33333
    assert candidate.ex_gallery_token == "survivorToken"
    assert len(candidate.messages) == 1


@pytest.mark.asyncio
async def test_a_hyperlinked_preview_only_message_becomes_a_candidate(
    tmp_path: Path,
) -> None:
    # The channel hyperlinks the word 「预览」, so the URL exists only in a
    # caption entity. A text-only regex sees nothing here.
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    await allow_sources(database, -100555)
    await database.save_telegram_updates(
        [
            {
                "update_id": 300,
                "channel_post": {
                    "message_id": 500,
                    "date": 1_700_010_000,
                    "chat": {"id": -100555, "title": "Preview Channel"},
                    "caption": "Preview Only Book\n预览 | 原始地址",
                    "caption_entities": [
                        {
                            "type": "text_link",
                            "offset": 18,
                            "length": 2,
                            "url": "https://telegra.ph/Preview-Only-Book-08-21",
                        },
                        {
                            "type": "text_link",
                            "offset": 23,
                            "length": 4,
                            "url": "https://exhentai.org/g/4108964/previewtoken/",
                        },
                    ],
                },
            }
        ]
    )

    result = await CandidateIngestor(database).process_pending_updates()
    candidate = await database.get_candidate(1)

    assert result.created_candidates == 1
    assert candidate is not None
    assert candidate.title == "Preview Only Book"
    assert candidate.preview_url == "https://telegra.ph/Preview-Only-Book-08-21"
    # The gallery link was hyperlinked too, and must still be picked up.
    assert candidate.ex_gid == 4108964
    assert candidate.ex_gallery_token == "previewtoken"


@pytest.mark.asyncio
async def test_a_preview_link_with_no_other_content_is_still_accepted(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    await allow_sources(database, -100556)
    await database.save_telegram_updates(
        [
            {
                "update_id": 310,
                "channel_post": {
                    "message_id": 510,
                    "date": 1_700_010_100,
                    "chat": {"id": -100556, "title": "Bare Preview"},
                    "text": "Bare Preview Book\nhttps://graph.org/Bare-Preview-08-21",
                },
            },
            {
                "update_id": 311,
                "channel_post": {
                    "message_id": 511,
                    "date": 1_700_010_110,
                    "chat": {"id": -100556, "title": "Bare Preview"},
                    "text": "https://graph.org/Untitled-08-21",
                },
            },
        ]
    )

    result = await CandidateIngestor(database).process_pending_updates()
    titled = await database.get_candidate(1)
    untitled = await database.get_candidate(2)

    assert result.created_candidates == 2
    assert result.ignored_updates == 0
    assert titled is not None
    assert titled.filter_reason == "包含预览页链接"
    assert titled.preview_url == "https://graph.org/Bare-Preview-08-21"
    # A link with no title still needs an operator, but the link is kept so
    # the fallback stays available once the title is filled in.
    assert untitled is not None
    assert untitled.status == "NEEDS_INFO"
    assert untitled.filter_reason == "缺少可识别标题"
    assert untitled.preview_url == "https://graph.org/Untitled-08-21"


@pytest.mark.asyncio
async def test_a_message_without_any_candidate_content_is_still_ignored(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    await allow_sources(database, -100557)
    await database.save_telegram_updates(
        [
            {
                "update_id": 320,
                "channel_post": {
                    "message_id": 520,
                    "date": 1_700_010_200,
                    "chat": {"id": -100557, "title": "Chatter"},
                    "text": "明天更新 https://example.com/blog",
                },
            }
        ]
    )

    result = await CandidateIngestor(database).process_pending_updates()

    assert result.created_candidates == 0
    assert result.ignored_updates == 1
    assert await database.list_candidates() == []


@pytest.mark.asyncio
async def test_the_first_preview_link_in_a_group_is_kept(tmp_path: Path) -> None:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    await allow_sources(database, -100558)
    await database.save_telegram_updates(
        [
            {
                "update_id": 330,
                "channel_post": {
                    "message_id": 530,
                    "date": 1_700_010_300,
                    "chat": {"id": -100558, "title": "Group Channel"},
                    "media_group_id": "preview-group",
                    "caption": "Grouped Book",
                    "caption_entities": [
                        {
                            "type": "text_link",
                            "offset": 0,
                            "length": 4,
                            "url": "https://telegra.ph/First-Page-08-21",
                        }
                    ],
                },
            },
            {
                "update_id": 331,
                "channel_post": {
                    "message_id": 531,
                    "date": 1_700_010_310,
                    "chat": {"id": -100558, "title": "Group Channel"},
                    "media_group_id": "preview-group",
                    "caption": "Grouped Book",
                    "caption_entities": [
                        {
                            "type": "text_link",
                            "offset": 0,
                            "length": 4,
                            "url": "https://telegra.ph/Second-Page-08-21",
                        }
                    ],
                },
            },
        ]
    )

    await CandidateIngestor(database).process_pending_updates()
    candidate = await database.get_candidate(1)

    assert candidate is not None
    assert len(candidate.messages) == 2
    assert candidate.preview_url == "https://telegra.ph/First-Page-08-21"


@pytest.mark.asyncio
async def test_an_edit_that_drops_the_preview_link_clears_it(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    await allow_sources(database, -100559)
    original = {
        "message_id": 540,
        "date": 1_700_010_400,
        "chat": {"id": -100559, "title": "Edited Channel"},
        "caption": "Edited Book",
        "caption_entities": [
            {
                "type": "text_link",
                "offset": 0,
                "length": 5,
                "url": "https://telegra.ph/Edited-Book-08-21",
            }
        ],
        "document": {
            "file_id": "edited-archive",
            "file_unique_id": "edited-archive-unique",
            "file_name": "Edited Book.zip",
        },
    }
    await database.save_telegram_updates(
        [{"update_id": 340, "channel_post": original}]
    )
    await CandidateIngestor(database).process_pending_updates()
    before = await database.get_candidate(1)
    await database.save_telegram_updates(
        [
            {
                "update_id": 341,
                "edited_channel_post": {
                    **original,
                    "caption": "Edited Book",
                    "caption_entities": [],
                },
            }
        ]
    )

    await CandidateIngestor(database).process_pending_updates()
    after = await database.get_candidate(1)

    assert before is not None
    assert before.preview_url == "https://telegra.ph/Edited-Book-08-21"
    assert after is not None
    assert after.preview_url is None
