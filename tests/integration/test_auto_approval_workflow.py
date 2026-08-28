import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from app.candidates.ingestor import CandidateIngestor
from app.config import Settings
from app.db.database import Database
from app.downloads.service import DownloadService
from app.main import create_app


def _settings(root: Path) -> Settings:
    return Settings(
        data_path=root / "data",
        library_path=root / "library",
        work_path=root / "work",
        app_secret_key="test-secret-key-with-at-least-32-characters",
        tag_translation_enabled=False,
    )


async def _seed_candidate(database: Database) -> int:
    await database.initialize()
    await database.configure_telegram_source(
        source_type="CHANNEL",
        chat_id=-100987,
        display_name="Automatic Approval",
        enabled=True,
        allowed_archive_formats=("zip",),
        max_attachment_size_mb=0,
    )
    await database.save_telegram_updates(
        [{
            "update_id": 987,
            "channel_post": {
                "message_id": 1,
                "date": 1_700_000_000,
                "chat": {"id": -100987, "title": "Automatic Approval"},
                "caption": "Automatic Title",
                "document": {
                    "file_id": "archive",
                    "file_unique_id": "archive-unique",
                    "file_name": "archive.zip",
                    "file_size": 1024,
                },
            },
        }]
    )
    await CandidateIngestor(database).process_pending_updates()
    return (await database.list_candidates())[0].candidate_id


def _authenticate(client: TestClient, settings: Settings) -> None:
    password = (settings.data_path / "bootstrap_admin_password").read_text(
        encoding="utf-8"
    )
    login = client.get("/login")
    client.post("/login", data={"password": password, "csrf_token": login.context["csrf_token"]})
    change = client.get("/settings/passwords")
    client.post("/change-password", data={"current_password": password, "new_password": "new-password-with-12-characters", "confirmation": "new-password-with-12-characters", "csrf_token": change.context["csrf_token"]})


def test_first_matching_auto_rule_approves_enqueues_and_audits(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = Database(settings.data_path / "ehbot.db")
    candidate_id = asyncio.run(_seed_candidate(database))
    first = asyncio.run(database.save_auto_approval_rule(
        rule_id=None,
        name="First",
        enabled=True,
        priority=10,
        condition={"kind": "condition", "field": "Title", "operator": "=", "value": "Automatic Title"},
        dsl_snapshot='{Title} = "Automatic Title"',
    ))
    asyncio.run(database.save_auto_approval_rule(
        rule_id=None,
        name="Second",
        enabled=True,
        priority=20,
        condition={"kind": "condition", "field": "Title", "operator": "=", "value": "Automatic Title"},
        dsl_snapshot='{Title} = "Automatic Title"',
    ))

    with TestClient(create_app(settings)) as client:
        _authenticate(client, settings)
        response = client.get("/candidates")
        assert response.status_code == 200
        page = client.get("/settings/auto-approval")
        assert "已保存规则" in page.text
        assert "First" in page.text

    jobs = asyncio.run(DownloadService(database, settings.work_path).list_jobs_for_candidate(candidate_id))
    actions = asyncio.run(database.list_review_actions(candidate_id))
    audit = next(action for action in actions if action.action == "AUTO_APPROVE")
    assert len(jobs) == 1
    assert audit.details["rule_id"] == first.rule_id
    assert audit.details["rule_version"] == 1
    assert audit.details["metadata"]["Title"] == "Automatic Title"
    assert audit.details["download_job_ids"] == [jobs[0].job_id]
