import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from app.candidates.ingestor import CandidateIngestor
from app.config import Settings
from app.db.database import Database
from app.main import create_app
from app.review.service import ReviewError, ReviewService


def make_settings(root: Path) -> Settings:
    return Settings(
        data_path=root / "data",
        library_path=root / "library",
        work_path=root / "work",
        app_secret_key="test-secret-key-with-at-least-32-characters",
    )


def authenticate(client: TestClient, settings: Settings) -> None:
    bootstrap_password = (
        settings.data_path / "bootstrap_admin_password"
    ).read_text(encoding="utf-8")
    login_page = client.get("/login")
    client.post(
        "/login",
        data={
            "password": bootstrap_password,
            "csrf_token": login_page.context["csrf_token"],
        },
    )
    change_page = client.get("/change-password")
    client.post(
        "/change-password",
        data={
            "current_password": bootstrap_password,
            "new_password": "new-password-with-12-characters",
            "confirmation": "new-password-with-12-characters",
            "csrf_token": change_page.context["csrf_token"],
        },
    )


async def seed_candidate(database: Database) -> int:
    await database.initialize()
    await database.configure_telegram_source(
        source_type="CHANNEL",
        chat_id=-100456,
        display_name="Review Channel",
        enabled=True,
        allowed_archive_formats=("zip", "rar", "7z", "cbz"),
        max_attachment_size_mb=0,
    )
    await database.save_telegram_updates(
        [
            {
                "update_id": 900,
                "channel_post": {
                    "message_id": 1,
                    "date": 1_700_000_300,
                    "chat": {"id": -100456, "title": "Review Channel"},
                    "caption": "Original Title",
                    "photo": [
                        {
                            "file_id": "photo-a",
                            "file_unique_id": "photo-a-uniq",
                            "width": 800,
                            "height": 1200,
                        }
                    ],
                },
            }
        ]
    )
    await CandidateIngestor(database).process_pending_updates()
    candidates = await database.list_candidates()
    assert candidates, "expected candidate to be created"
    return candidates[0].candidate_id


def test_approve_moves_candidate_to_approved_state(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(seed_candidate(database))

    with TestClient(create_app(settings), follow_redirects=False) as client:
        authenticate(client, settings)
        detail = client.get(f"/candidates/{candidate_id}")
        assert detail.status_code == 200
        csrf = detail.context["csrf_token"]
        response = client.post(
            f"/candidates/{candidate_id}/approve",
            data={"csrf_token": csrf, "note": "Looks good"},
        )
        assert response.status_code == 303
        refreshed = client.get(f"/candidates/{candidate_id}")
        assert refreshed.status_code == 200
        assert "APPROVED" in refreshed.text or chr(24050) in refreshed.text


def test_reject_requires_reason(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(seed_candidate(database))

    with TestClient(create_app(settings), follow_redirects=False) as client:
        authenticate(client, settings)
        detail = client.get(f"/candidates/{candidate_id}")
        csrf = detail.context["csrf_token"]
        response = client.post(
            f"/candidates/{candidate_id}/reject",
            data={"csrf_token": csrf, "reason": ""},
        )
        assert response.status_code == 400


def test_metadata_edit_persists_and_creates_action(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(seed_candidate(database))

    with TestClient(create_app(settings), follow_redirects=False) as client:
        authenticate(client, settings)
        detail = client.get(f"/candidates/{candidate_id}")
        csrf = detail.context["csrf_token"]
        response = client.post(
            f"/candidates/{candidate_id}/metadata",
            data={
                "csrf_token": csrf,
                "field_name": "Title",
                "field_value": "Edited Title",
            },
        )
        assert response.status_code == 303

    metadata = asyncio.run(database.list_metadata(candidate_id))
    title_entry = next(m for m in metadata if m.field_name == "Title")
    assert title_entry.field_value == "Edited Title"
    assert title_entry.is_manual

    history = asyncio.run(database.list_review_actions(candidate_id))
    assert any(h.action == "EDIT_METADATA" for h in history)


def test_metadata_edit_rejects_unknown_field(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(seed_candidate(database))

    with TestClient(create_app(settings), follow_redirects=False) as client:
        authenticate(client, settings)
        detail = client.get(f"/candidates/{candidate_id}")
        csrf = detail.context["csrf_token"]
        response = client.post(
            f"/candidates/{candidate_id}/metadata",
            data={
                "csrf_token": csrf,
                "field_name": "UnsupportedField",
                "field_value": "value",
            },
        )
        assert response.status_code == 400


def test_requeue_restores_pending_review(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(seed_candidate(database))

    with TestClient(create_app(settings), follow_redirects=False) as client:
        authenticate(client, settings)
        detail = client.get(f"/candidates/{candidate_id}")
        csrf = detail.context["csrf_token"]
        client.post(
            f"/candidates/{candidate_id}/reject",
            data={"csrf_token": csrf, "reason": "spam"},
        )
        rejected_detail = client.get(f"/candidates/{candidate_id}")
        rejected_csrf = rejected_detail.context["csrf_token"]
        response = client.post(
            f"/candidates/{candidate_id}/requeue",
            data={"csrf_token": rejected_csrf, "note": "double check"},
        )
        assert response.status_code == 303

    detail = asyncio.run(database.get_candidate(candidate_id))
    assert detail is not None
    assert detail.status == "PENDING_REVIEW"


def test_review_service_rejects_invalid_status_transition(tmp_path: Path) -> None:
    database = Database(tmp_path / "ehbot.db")
    asyncio.run(database.initialize())

    raised = False
    try:
        asyncio.run(
            ReviewService(database).approve_candidate(999, "admin")
        )
    except ReviewError:
        raised = True
    assert raised


def test_rating_field_requires_numeric_value(tmp_path: Path) -> None:
    database = Database(tmp_path / "ehbot.db")
    asyncio.run(database.initialize())

    async def run() -> None:
        await database.configure_telegram_source(
            source_type="CHANNEL",
            chat_id=-100999,
            display_name="Rating Channel",
            enabled=True,
            allowed_archive_formats=("zip",),
            max_attachment_size_mb=0,
        )
        await database.save_telegram_updates(
            [
                {
                    "update_id": 901,
                    "channel_post": {
                        "message_id": 1,
                        "date": 1_700_000_400,
                        "chat": {
                            "id": -100999,
                            "title": "Rating Channel",
                        },
                        "caption": "Rating Test",
                        "photo": [
                            {
                                "file_id": "p",
                                "file_unique_id": "p-uniq",
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
        candidate_id = candidates[0].candidate_id
        try:
            await ReviewService(database).set_manual_metadata(
                candidate_id, "admin", "Rating", "not-a-number"
            )
        except ReviewError:
            return
        raise AssertionError("expected ReviewError for non-numeric rating")

    asyncio.run(run())
