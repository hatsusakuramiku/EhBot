from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def make_settings(root: Path) -> Settings:
    return Settings(
        data_path=root / "data",
        library_path=root / "library",
        work_path=root / "work",
        app_secret_key="test-secret-key-with-at-least-32-characters",
        tag_translation_enabled=False,
        archive_toolchain_auto_install=False,
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


def test_source_rules_page_requires_authentication(tmp_path: Path) -> None:
    with TestClient(create_app(make_settings(tmp_path))) as client:
        response = client.get("/sources", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_admin_can_add_and_update_source_rules(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        authenticate(client, settings)
        page = client.get("/sources")
        csrf_token = page.context["csrf_token"]
        created = client.post(
            "/sources",
            data={
                "source_type": "CHANNEL",
                "chat_id": "-100600",
                "display_name": "Configured Channel",
                "enabled": "on",
                "allowed_archive_formats": ["zip", "cbz"],
                "max_attachment_size_mb": "256",
                "csrf_token": csrf_token,
            },
            follow_redirects=False,
        )
        configured_page = client.get("/sources")

    assert created.status_code == 303
    assert created.headers["location"] == "/sources"
    assert "Configured Channel" in configured_page.text
    assert "ZIP、CBZ" in configured_page.text
    assert "256 MB" in configured_page.text


def test_needs_info_queue_is_separate_from_pending_queue(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        authenticate(client, settings)
        response = client.get("/candidates/needs-info")

    assert response.status_code == 200
    assert "待补充队列" in response.text
    assert "暂无待补充候选" in response.text
