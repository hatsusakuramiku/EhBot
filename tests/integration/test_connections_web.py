from pathlib import Path

import httpx
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
    change_page = client.get("/settings/passwords")
    client.post(
        "/change-password",
        data={
            "current_password": bootstrap_password,
            "new_password": "new-password-with-12-characters",
            "confirmation": "new-password-with-12-characters",
            "csrf_token": change_page.context["csrf_token"],
        },
    )


def connection_transport(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/getMe"):
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "id": 42,
                    "is_bot": True,
                    "first_name": "EhBot Intake",
                    "username": "ehbot_intake_bot",
                },
            },
        )
    if request.url.path.endswith("/getUpdates"):
        return httpx.Response(200, json={"ok": True, "result": []})
    if request.url == "https://exhentai.org/":
        return httpx.Response(200, text="<title>ExHentai.org</title>")
    raise AssertionError(f"Unexpected request: {request.url}")


def test_connections_page_requires_authentication(tmp_path: Path) -> None:
    with TestClient(create_app(make_settings(tmp_path))) as client:
        response = client.get("/settings/connections", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_admin_can_connect_and_disconnect_providers_without_secret_echo(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    transport = httpx.MockTransport(connection_transport)
    app = create_app(
        settings,
        telegram_transport=transport,
        exhentai_transport=transport,
    )
    with TestClient(app) as client:
        authenticate(client, settings)
        page = client.get("/settings/connections")
        csrf_token = page.context["csrf_token"]

        telegram = client.post(
            "/connections/telegram",
            data={"bot_token": "123:must-not-render", "csrf_token": csrf_token},
            follow_redirects=False,
        )
        exhentai = client.post(
            "/connections/exhentai",
            data={
                "ipb_member_id": "10001",
                "ipb_pass_hash": "pass-must-not-render",
                "igneous": "igneous-must-not-render",
                "csrf_token": csrf_token,
            },
            follow_redirects=False,
        )
        connected_page = client.get("/settings/connections")
        status = client.get("/api/connections/status")

        telegram_disconnect = client.post(
            "/connections/telegram/disconnect",
            data={"csrf_token": csrf_token},
            follow_redirects=False,
        )
        exhentai_disconnect = client.post(
            "/connections/exhentai/disconnect",
            data={"csrf_token": csrf_token},
            follow_redirects=False,
        )

    assert telegram.status_code == 303
    assert exhentai.status_code == 303
    assert telegram_disconnect.status_code == 303
    assert exhentai_disconnect.status_code == 303
    assert "@ehbot_intake_bot" in connected_page.text
    assert "Member 10001" in connected_page.text
    assert "must-not-render" not in connected_page.text
    assert status.json()["telegram"]["state"] == "connected"
    assert status.json()["exhentai"]["state"] == "connected"
    assert not (settings.data_path / "private" / "telegram_bot_token").exists()
    assert not (settings.data_path / "private" / "exhentai_cookies").exists()


def test_connection_mutations_require_valid_csrf(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        authenticate(client, settings)
        response = client.post(
            "/connections/telegram",
            data={"bot_token": "123:secret", "csrf_token": "invalid"},
        )

    assert response.status_code == 403
    assert not (settings.data_path / "private" / "telegram_bot_token").exists()
