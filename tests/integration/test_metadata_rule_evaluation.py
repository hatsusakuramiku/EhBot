"""Integration tests for source-configured metadata rule re-evaluation."""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.candidates.ingestor import CandidateIngestor
from app.db.database import Database
from app.review.service import ReviewService


def _seed_candidate(database: Database, chat_id: int) -> int:
    async def run() -> int:
        await database.save_telegram_updates(
            [
                {
                    "update_id": 900,
                    "channel_post": {
                        "message_id": 1,
                        "date": 1_700_100_000,
                        "chat": {"id": chat_id, "title": "Fixture"},
                        "caption": "Fixture Title",
                        "photo": [
                            {
                                "file_id": "p",
                                "file_unique_id": "pu",
                                "width": 100,
                                "height": 100,
                            }
                        ],
                    },
                }
            ]
        )
        await CandidateIngestor(database).process_pending_updates()
        candidates = await database.list_candidates()
        assert candidates, "candidate should have been created"
        return candidates[0].candidate_id

    return asyncio.run(run())


def _configure_source(
    database: Database,
    *,
    chat_id: int,
    **overrides,
) -> None:
    base = dict(
        source_type="CHANNEL",
        chat_id=chat_id,
        display_name="Fixture",
        enabled=True,
        allowed_archive_formats=("zip", "rar", "7z", "cbz"),
        max_attachment_size_mb=0,
    )
    base.update(overrides)
    asyncio.run(database.configure_telegram_source(**base))


def _re_evaluate(database: Database, candidate_id: int) -> None:
    asyncio.run(database.re_evaluate_candidate_metadata_rules(candidate_id))


def _set_metadata(
    database: Database, candidate_id: int, field: str, value: str
) -> None:
    asyncio.run(
        ReviewService(database).set_manual_metadata(
            candidate_id, "admin", field, value
        )
    )


def _status(database: Database, candidate_id: int) -> tuple[str, str]:
    async def run() -> tuple[str, str]:
        detail = await database.get_candidate(candidate_id)
        assert detail is not None
        return detail.status, detail.filter_reason

    return asyncio.run(run())


def _rule_actions(database: Database, candidate_id: int) -> list[dict]:
    async def run() -> list[dict]:
        actions = await database.list_review_actions(candidate_id)
        return [a.details for a in actions if a.action == "METADATA_RULE"]

    return asyncio.run(run())


def test_required_tag_missing_rejects(tmp_path: Path) -> None:
    db = Database(tmp_path / "rules.db")
    asyncio.run(db.initialize())
    _configure_source(
        db,
        chat_id=-100501,
        required_tags=("language:chinese",),
    )
    cid = _seed_candidate(db, chat_id=-100501)

    _re_evaluate(db, cid)
    status, reason = _status(db, cid)
    assert status == "REJECTED", status
    assert "language:chinese" in reason

    actions = _rule_actions(db, cid)
    assert len(actions) == 1
    assert actions[0]["result"] == "IGNORE"
    assert actions[0]["source_id"] >= 1


def test_metadata_edit_unblocks_rejected_candidate(tmp_path: Path) -> None:
    db = Database(tmp_path / "rules.db")
    asyncio.run(db.initialize())
    _configure_source(
        db,
        chat_id=-100502,
        forbidden_tags=("male:only",),
    )
    cid = _seed_candidate(db, chat_id=-100502)

    _set_metadata(db, cid, "Tags", "male:only")
    status, _ = _status(db, cid)
    assert status == "REJECTED", status

    _set_metadata(db, cid, "Tags", "female:big_breasts")
    status, _ = _status(db, cid)
    assert status == "PENDING_REVIEW", status


def test_rating_below_threshold_rejects(tmp_path: Path) -> None:
    db = Database(tmp_path / "rules.db")
    asyncio.run(db.initialize())
    _configure_source(db, chat_id=-100503, min_rating=4.0)
    cid = _seed_candidate(db, chat_id=-100503)

    _set_metadata(db, cid, "Rating", "3.5")
    status, reason = _status(db, cid)
    assert status == "REJECTED", status
    assert "3.5" in reason


def test_approved_candidate_is_terminal(tmp_path: Path) -> None:
    db = Database(tmp_path / "rules.db")
    asyncio.run(db.initialize())
    _configure_source(db, chat_id=-100504, min_rating=4.5)
    cid = _seed_candidate(db, chat_id=-100504)

    _set_metadata(db, cid, "Rating", "5.0")
    asyncio.run(ReviewService(db).approve_candidate(cid, "admin"))
    # Editing metadata after approval should NOT transition out of APPROVED.
    _set_metadata(db, cid, "Rating", "1.0")
    status, _ = _status(db, cid)
    assert status == "APPROVED", status


def test_needs_info_returns_to_pending_when_metadata_filled(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "rules.db")
    asyncio.run(db.initialize())
    _configure_source(
        db,
        chat_id=-100505,
        allowed_languages=("chinese",),
        min_rating=4.0,
    )
    cid = _seed_candidate(db, chat_id=-100505)

    # No language/rating -> re-evaluation flips to NEEDS_INFO.
    _re_evaluate(db, cid)
    status, _ = _status(db, cid)
    assert status == "NEEDS_INFO", status

    # Operator adds both fields -> back to PENDING_REVIEW.
    _set_metadata(db, cid, "Language", "chinese")
    _set_metadata(db, cid, "Rating", "4.5")
    status, _ = _status(db, cid)
    assert status == "PENDING_REVIEW", status
