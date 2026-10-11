"""The one-time R62 repair of titles that were read off a caption.

Before R62 the message parser stored a caption's first non-URL line as a
`Title` with `value_source='TELEGRAM'`. The display query ignores those rows
now -- the gallery outranks them -- but leaving them in the table means the
metadata drawer still offers a caption line as a title, and a candidate whose
gallery answered without a name would look named. `maintain_titles` deletes
exactly those three sources, once, at startup.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.db.database import MISSING_TITLE_REASON, TITLE_MAINTENANCE_KEY, Database


async def _candidate(database: Database, *, status: str = "PENDING_REVIEW") -> int:
    with database.connection() as connection:
        cursor = connection.execute(
            "INSERT INTO candidates (status, filter_result, filter_reason) "
            "VALUES (?, 'ACCEPT', '')",
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
) -> None:
    with database.connection() as connection:
        connection.execute(
            "INSERT INTO metadata_values (candidate_id, field_name, field_value, "
            "value_source, confidence, is_manual, is_locked) "
            "VALUES (?, ?, ?, ?, 0.9, ?, ?)",
            (candidate_id, field, value, source, is_manual, is_locked),
        )


async def _rows(database: Database, candidate_id: int) -> list[tuple[str, str, int, int]]:
    with database.connection() as connection:
        return [
            (str(row[0]), str(row[1]), int(row[2]), int(row[3]))
            for row in connection.execute(
                "SELECT field_name, value_source, is_manual, is_locked "
                "FROM metadata_values WHERE candidate_id = ? ORDER BY id",
                (candidate_id,),
            ).fetchall()
        ]


async def _status(database: Database, candidate_id: int) -> str:
    with database.connection() as connection:
        return str(
            connection.execute(
                "SELECT status FROM candidates WHERE id = ?", (candidate_id,)
            ).fetchone()[0]
        )


@pytest.mark.asyncio
async def test_the_repair_deletes_only_the_caption_lines(tmp_path: Path) -> None:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    # A caption-only candidate: it loses its name and goes back to 待补充.
    caption_only = await _candidate(database)
    await _value(database, caption_only, "Title", "caption guess", "TELEGRAM")
    # A gallery-linked one: the caption row goes, the gallery's stays.
    gallery_named = await _candidate(database)
    await _value(database, gallery_named, "Title", "caption guess", "TELEGRAM")
    await _value(database, gallery_named, "Title", "画廊标题", "EXHENTAI")

    result = await database.maintain_titles()

    assert result == {
        "status": "completed",
        "scanned": 2,
        "deleted": 2,
        "needs_info": 1,
    }
    assert await _rows(database, caption_only) == []
    assert await _status(database, caption_only) == "NEEDS_INFO"
    assert await _rows(database, gallery_named) == [
        ("Title", "EXHENTAI", 0, 0)
    ]
    assert await _status(database, gallery_named) == "PENDING_REVIEW"


@pytest.mark.asyncio
async def test_a_manual_or_locked_row_is_left_alone(tmp_path: Path) -> None:
    """`is_manual` / `is_locked` are the operator's mark, not data to repair."""
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    manual = await _candidate(database)
    await _value(
        database, manual, "Title", "手改标题", "TELEGRAM", is_manual=1
    )
    locked = await _candidate(database)
    await _value(
        database, locked, "Title", "锁定标题", "TELEGRAM", is_locked=1
    )

    result = await database.maintain_titles()

    assert result["deleted"] == 0
    assert result["needs_info"] == 0
    assert await _rows(database, manual) == [("Title", "TELEGRAM", 1, 0)]
    assert await _status(database, manual) == "PENDING_REVIEW"
    assert await _rows(database, locked) == [("Title", "TELEGRAM", 0, 1)]
    assert await _status(database, locked) == "PENDING_REVIEW"


@pytest.mark.asyncio
async def test_inferred_and_filename_rows_are_repaired_too(tmp_path: Path) -> None:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    inferred = await _candidate(database)
    await _value(database, inferred, "Title", "ExHentai #1", "INFERRED")
    filename = await _candidate(database)
    await _value(database, filename, "Title", "book", "FILENAME")

    result = await database.maintain_titles()

    assert result["deleted"] == 2
    assert await _status(database, inferred) == "NEEDS_INFO"
    assert await _status(database, filename) == "NEEDS_INFO"


@pytest.mark.asyncio
async def test_an_already_decided_work_keeps_its_status(tmp_path: Path) -> None:
    """The pass repairs what a title was read off; APPROVED and REJECTED are not
    statements about a title, so they are not rewritten."""
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    approved = await _candidate(database, status="APPROVED")
    await _value(database, approved, "Title", "caption guess", "TELEGRAM")

    result = await database.maintain_titles()

    assert result["deleted"] == 1
    assert result["needs_info"] == 0
    assert await _status(database, approved) == "APPROVED"


@pytest.mark.asyncio
async def test_the_marker_makes_a_second_run_a_no_op(tmp_path: Path) -> None:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    candidate_id = await _candidate(database)
    await _value(database, candidate_id, "Title", "caption guess", "TELEGRAM")

    first = await database.maintain_titles()
    # A caption row written after the repair (there is no producer for one any
    # more, but a restored backup could hold one) is not touched: the marker is
    # what makes this 「exactly once」.
    await _value(database, candidate_id, "Title", "late caption", "TELEGRAM")
    second = await database.maintain_titles()

    assert first["status"] == "completed"
    assert second == {
        "status": "already_done",
        "scanned": 0,
        "deleted": 0,
        "needs_info": 0,
    }
    assert ("Title", "TELEGRAM", 0, 0) in await _rows(database, candidate_id)
    with database.connection() as connection:
        marker = connection.execute(
            "SELECT value FROM system_settings WHERE key = ?",
            (TITLE_MAINTENANCE_KEY,),
        ).fetchone()
    assert marker is not None and "completed_at" in marker[0]


@pytest.mark.asyncio
async def test_the_reason_left_behind_is_the_shared_one(tmp_path: Path) -> None:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    candidate_id = await _candidate(database)
    await _value(database, candidate_id, "Title", "caption guess", "TELEGRAM")

    await database.maintain_titles()

    with database.connection() as connection:
        reason = connection.execute(
            "SELECT filter_reason FROM candidates WHERE id = ?", (candidate_id,)
        ).fetchone()[0]
    assert reason == MISSING_TITLE_REASON
