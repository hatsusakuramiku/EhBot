import asyncio
from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient

from app.candidates.ingestor import CandidateIngestor
from app.config import Settings
from app.db.database import Database
from app.main import create_app


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


async def seed_candidate(database: Database) -> None:
    await database.initialize()
    await database.save_telegram_updates(
        [
            {
                "update_id": 300,
                "channel_post": {
                    "message_id": 60,
                    "date": 1_700_000_100,
                    "chat": {"id": -100123, "title": "Fixture Channel"},
                    "caption": "Queue Fixture Comic",
                    "photo": [
                        {
                            "file_id": "queue-photo",
                            "file_unique_id": "queue-photo-unique",
                            "width": 800,
                            "height": 1200,
                        }
                    ],
                },
            }
        ]
    )
    await CandidateIngestor(database).process_pending_updates()


def test_authenticated_user_can_view_pending_candidate_queue(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    asyncio.run(seed_candidate(database))

    with TestClient(create_app(settings)) as client:
        authenticate(client, settings)
        response = client.get("/candidates")

    assert response.status_code == 200
    assert "待审核队列" in response.text
    assert "Queue Fixture Comic" in response.text
    assert 'href="http://testserver/candidates/1"' in response.text


def test_candidate_detail_shows_source_message_and_attachment(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    asyncio.run(seed_candidate(database))

    with TestClient(create_app(settings)) as client:
        authenticate(client, settings)
        response = client.get("/candidates/1")

    assert response.status_code == 200
    assert "Queue Fixture Comic" in response.text
    assert "Fixture Channel" in response.text
    assert "消息 #60" in response.text
    assert "图片预览" in response.text


def test_dashboard_uses_persisted_candidate_counts(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    asyncio.run(seed_candidate(database))

    with TestClient(create_app(settings)) as client:
        authenticate(client, settings)
        response = client.get("/")

    assert response.status_code == 200
    assert "<strong>1</strong><span>待审核</span>" in response.text
    assert "<strong>0</strong><span>待补充</span>" in response.text


def test_pending_queue_excludes_candidates_in_other_states(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    asyncio.run(seed_candidate(database))
    with sqlite3.connect(database.path) as connection:
        connection.execute("UPDATE candidates SET status = 'NEEDS_INFO'")

    with TestClient(create_app(settings)) as client:
        authenticate(client, settings)
        response = client.get("/candidates")

    assert response.status_code == 200
    assert "Queue Fixture Comic" not in response.text
    assert "暂无待审核候选" in response.text
