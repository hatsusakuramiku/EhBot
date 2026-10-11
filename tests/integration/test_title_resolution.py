"""How a work's name is chosen, and what 「no name yet」 means (R62).

One shared fragment answers both, which is the point of the change: before R62
six queries each ordered `metadata_values` their own way, and they disagreed
about whether a caption's first line was the title. These tests read the
fragment directly and through the two projections that render it, so a future
edit to one call site cannot quietly re-open the question.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.archive.service import SETTING_TITLE_SOURCE
from app.db.database import TITLE_SOURCE_SETTING_KEY, Database, title_subquery


async def _candidate(database: Database, *, status: str = "NEEDS_INFO") -> int:
    with database.connection() as connection:
        cursor = connection.execute(
            "INSERT INTO candidates (status, filter_result, filter_reason) "
            "VALUES (?, 'NEEDS_INFO', '尚未取得画廊标题')",
            (status,),
        )
        return int(cursor.lastrowid)


async def _value(
    database: Database,
    candidate_id: int,
    field: str,
    value: str,
    source: str,
    *,
    is_manual: int = 0,
    is_locked: int = 0,
    confidence: float = 0.5,
) -> None:
    with database.connection() as connection:
        connection.execute(
            "INSERT INTO metadata_values (candidate_id, field_name, field_value, "
            "value_source, confidence, is_manual, is_locked) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (candidate_id, field, value, source, confidence, is_manual, is_locked),
        )


async def _resolve(database: Database, candidate_id: int) -> str | None:
    with database.connection() as connection:
        row = connection.execute(
            "SELECT " + title_subquery("?"), (candidate_id,)
        ).fetchone()
    return row[0] if row is not None and row[0] is not None else None


@pytest.mark.asyncio
async def test_the_gallery_outranks_the_caption(tmp_path: Path) -> None:
    """The bug R62 exists to fix: confidence used to decide, and the caption's
    guess shipped at 0.9 while the gallery's own title shipped at 0.6."""
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    candidate_id = await _candidate(database)
    await _value(
        database, candidate_id, "Title", "caption guess", "TELEGRAM", confidence=0.9
    )
    await _value(
        database, candidate_id, "Title", "画廊标题", "EXHENTAI", confidence=0.6
    )

    assert await _resolve(database, candidate_id) == "画廊标题"


@pytest.mark.asyncio
async def test_a_manual_value_wins_outright(tmp_path: Path) -> None:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    candidate_id = await _candidate(database)
    await _value(
        database, candidate_id, "Title", "画廊标题", "EXHENTAI", confidence=1.0
    )
    await _value(
        database,
        candidate_id,
        "Title",
        "手改标题",
        "OPERATOR",
        is_manual=1,
        confidence=0.1,
    )

    assert await _resolve(database, candidate_id) == "手改标题"


@pytest.mark.asyncio
async def test_japanese_is_preferred_by_default(tmp_path: Path) -> None:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    candidate_id = await _candidate(database)
    await _value(
        database, candidate_id, "Title", "English Name", "EXHENTAI", confidence=0.9
    )
    await _value(
        database,
        candidate_id,
        "JapaneseTitle",
        "日本語名",
        "EXHENTAI",
        confidence=0.6,
    )

    assert await _resolve(database, candidate_id) == "日本語名"


@pytest.mark.asyncio
async def test_the_preference_follows_the_archive_setting(tmp_path: Path) -> None:
    """One setting, read inside the query: the web card and the archive file
    name must not be able to disagree about which language wins."""
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    candidate_id = await _candidate(database)
    await _value(
        database, candidate_id, "Title", "English Name", "EXHENTAI", confidence=0.9
    )
    await _value(
        database,
        candidate_id,
        "JapaneseTitle",
        "日本語名",
        "EXHENTAI",
        confidence=0.6,
    )
    await database.save_archive_settings({SETTING_TITLE_SOURCE: "english"})

    assert await _resolve(database, candidate_id) == "English Name"


@pytest.mark.asyncio
async def test_the_setting_key_copy_is_pinned(tmp_path: Path) -> None:
    """`database.py` spells the key rather than importing it (a real import
    cycle), so the copy is pinned here."""
    assert TITLE_SOURCE_SETTING_KEY == SETTING_TITLE_SOURCE


@pytest.mark.asyncio
async def test_no_title_is_what_keeps_a_candidate_in_needs_info(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    candidate_id = await _candidate(database)

    assert await _resolve(database, candidate_id) is None
    with database.connection() as connection:
        assert (
            Database._candidate_title_missing(connection, candidate_id) is True
        )

    # A Japanese name alone counts: a gallery that only has one is named.
    await _value(
        database, candidate_id, "JapaneseTitle", "日本語名", "EXHENTAI"
    )
    with database.connection() as connection:
        assert (
            Database._candidate_title_missing(connection, candidate_id) is False
        )


@pytest.mark.asyncio
async def test_the_list_row_and_the_detail_page_agree(tmp_path: Path) -> None:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    candidate_id = await _candidate(database)
    await _value(
        database, candidate_id, "Title", "caption guess", "TELEGRAM", confidence=0.9
    )
    await _value(
        database, candidate_id, "Title", "画廊标题", "EXHENTAI", confidence=0.6
    )

    rows, _total = await database.list_candidates_page(statuses=("NEEDS_INFO",))
    detail = await database.get_candidate(candidate_id)

    assert rows[0].title == "画廊标题"
    assert detail is not None and detail.title == "画廊标题"
