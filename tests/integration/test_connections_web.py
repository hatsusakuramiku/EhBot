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
            "username": "admin",
            "password": bootstrap_password,
            "csrf_token": login_page.context["csrf_token"],
        },
    )
    change_page = client.get("/settings/passwords")
    client.post(
        "/change-password",
        data={
            "expected_revision": client.get("/api/v1/auth/profile").json()["revision"],
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
        client.headers["X-CSRF-Token"] = csrf_token
        accounts = []
        for kind, fields in (
            ("TG_BOT", {"bot_token": "123:must-not-render"}),
            ("EH", {"ipb_member_id": "10001", "ipb_pass_hash": "pass-must-not-render", "igneous": "igneous-must-not-render"}),
        ):
            created = client.post("/api/v1/accounts/commands", json={
                "action": "create", "kind": kind, "label": kind, "fields": fields,
                "idempotency_key": f"create-{kind}",
            })
            assert created.status_code == 200
            account = created.json()["result"]
            for action in ("test", "enable"):
                result = client.post("/api/v1/accounts/commands", json={
                    "action": action, "account_id": account["account_id"],
                    "expected_revision": account["revision"], "idempotency_key": f"{action}-{kind}",
                })
                assert result.status_code == 200
                account = result.json()["result"]
            accounts.append((account, fields))
        connected_page = client.get("/settings/connections")
        status = client.get("/api/connections/status")
        # Old writes lack account identity/version and must never bypass CAS.
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
        for account, fields in accounts:
            unchanged = client.get(f"/api/v1/accounts/{account['account_id']}").json()
            assert unchanged == account
            disabled = client.post("/api/v1/accounts/commands", json={
                "action": "disable", "account_id": account["account_id"],
                "expected_revision": account["revision"], "idempotency_key": f"disable-{account['kind']}",
            })
            assert disabled.status_code == 202
            client.portal.call(app.state.connection_manager.process_operations)
            operation = client.get(f"/api/v1/accounts/operations/{disabled.json()['operation_id']}").json()
            assert operation["status"] == "COMPLETED"
            saved = client.get(f"/api/v1/accounts/{account['account_id']}").json()
            assert not saved["enabled"]
            revealed = client.post(f"/api/v1/accounts/{account['account_id']}/reveal", json={"expected_revision": saved["revision"]})
            assert revealed.json()["fields"] == fields

    assert telegram.status_code == 400
    assert exhentai.status_code == 400
    assert telegram_disconnect.status_code == 409
    assert exhentai_disconnect.status_code == 409
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
