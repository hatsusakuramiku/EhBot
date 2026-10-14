"""Shared account HTTP API, auth, masking, receipts and Web management page."""
import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.integration.test_settings_web import _authenticate, _settings


@pytest.fixture
def client(tmp_path):
    settings = _settings(tmp_path)
    async def transport(request):
        if request.url.path.endswith("getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"id": 1234, "username": "test_bot", "first_name": "Bot"}})
        return httpx.Response(200, text="ExHentai")
    app = create_app(settings, telegram_transport=httpx.MockTransport(transport), exhentai_transport=httpx.MockTransport(transport))
    with TestClient(app) as value:
        csrf = _authenticate(value, settings)
        value.headers["X-CSRF-Token"] = csrf
        yield value


def command(client, action, key, account=None, **params):
    payload = {"action": action, "idempotency_key": key, **params}
    if account is not None:
        payload.update(account_id=account["account_id"], expected_revision=account["revision"])
    return client.post("/api/v1/accounts/commands", json=payload)


def create(client, kind="EH"):
    fields = {"ipb_member_id": "1234", "ipb_pass_hash": "test-private-cookie", "igneous": "test-private-igneous"} if kind == "EH" else {"bot_token": "test-private-token"}
    response = command(client, "create", "create-account", kind=kind, label="Account", fields=fields)
    assert response.status_code == 200
    return response.json()["result"]


def test_masked_page_editable_reveal_requires_csrf_and_does_not_cache(client):
    account = create(client)
    response = client.get("/api/v1/accounts")
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["items"] == [account]
    assert "test-private" not in response.text
    page = client.get("/settings/connections")
    assert page.status_code == 200 and 'data-account-manager' in page.text
    assert 'value="********"' in page.text and 'data-account-field="igneous"' in page.text
    assert "test-private" not in page.text and "默认显示掩码" in page.text
    assert "api/v1/accounts/commands" in page.text
    path = f"/api/v1/accounts/{account['account_id']}/reveal"
    headers = dict(client.headers)
    del client.headers["X-CSRF-Token"]
    assert client.post(path, json={"expected_revision":account["revision"]}).status_code == 403
    client.headers.update(headers)
    revealed = client.post(path, json={"expected_revision":account["revision"]})
    assert revealed.status_code == 200 and revealed.headers["cache-control"] == "no-store"
    assert revealed.json()["fields"]["igneous"] == "test-private-igneous"


def test_replay_create_rename_and_stale_client_cannot_override_draft(client):
    account = create(client)
    replay = command(client, "create", "create-account", kind="EH", label="Account", fields={"ipb_member_id": "1234", "ipb_pass_hash": "test-private-cookie", "igneous": "test-private-igneous"})
    assert replay.json()["result"] == account
    assert command(client, "create", "create-account", kind="EH", label="Different", fields={}).status_code == 409
    renamed = command(client, "rename", "rename-account", account, label="New name")
    assert renamed.status_code == 200
    assert renamed.json()["result"]["credential_revision"] == account["credential_revision"]
    stale = command(client, "rename", "stale-account", account, label="Late name")
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "REVISION_CONFLICT"
    assert command(client, "rename", "rename-account", account, label="New name").json() == renamed.json()
    assert client.get(f"/api/v1/accounts/{account['account_id']}").json()["label"] == "New name"
    assert client.get("/api/v1/accounts/commands/rename-account").json() == renamed.json()


def test_version_preconditions_and_unknown_fields_rejected_before_mutation(client):
    account = create(client)
    no_revision = client.post("/api/v1/accounts/commands", json={"action":"disable", "account_id":account["account_id"], "idempotency_key":"no-revision"})
    assert no_revision.status_code == 428
    assert command(client, "disable", "", account).status_code == 428
    assert command(client, "disable", "unknown-field", account, unexpected="x").status_code == 400
    assert client.get(f"/api/v1/accounts/{account['account_id']}").json() == account


def test_verify_enable_disable_partial_edit_and_delete_shared_operations(client):
    account = create(client)
    verified = command(client, "test", "verify-account", account)
    assert verified.status_code == 200
    account = verified.json()["result"]
    account = command(client, "enable", "enable-account", account).json()["result"]
    assert account["enabled"]
    editing = command(client, "edit", "edit-account", account, fields={"igneous":"next-private-igneous"})
    assert editing.status_code == 202
    operation = editing.json()["operation_id"]
    client.portal.call(client.app.state.connection_manager.process_operations)
    assert client.get(f"/api/v1/accounts/operations/{operation}").json()["status"] == "COMPLETED"
    account = client.get(f"/api/v1/accounts/{account['account_id']}").json()
    assert not account["enabled"] and not account["verified"]
    revealed = client.post(f"/api/v1/accounts/{account['account_id']}/reveal", json={"expected_revision":account["revision"]}).json()
    assert revealed["fields"] == {"ipb_member_id":"1234", "ipb_pass_hash":"test-private-cookie", "igneous":"next-private-igneous"}
    deleted = command(client, "delete", "delete-account", account)
    assert deleted.status_code == 202
    client.portal.call(client.app.state.connection_manager.process_operations)
    assert client.get(f"/api/v1/accounts/{account['account_id']}").status_code == 404
    assert command(client, "delete", "delete-account", account).json() == deleted.json()
    assert client.get("/api/v1/accounts").json()["items"] == []


def test_bearer_clients_share_commands_and_session_is_not_revealed(client):
    token = client.post("/api/v1/auth/login", json={"username":"admin", "password":"new-password-with-12-characters"}).json()["access_token"]
    client.cookies.clear()
    del client.headers["X-CSRF-Token"]
    client.headers["Authorization"] = f"Bearer {token}"
    account = command(client, "create", "create-user", kind="TG_USER", label="User", fields={"api_id":"1234", "api_hash":"test-api-hash", "phone":"+123456789"}).json()["result"]
    client.portal.call(lambda: client.app.state.connection_manager.registry.save_session(account["account_id"], expected_revision=account["revision"], session="test-private-session", remote_identity="42", platform_name="User"))
    account = client.get(f"/api/v1/accounts/{account['account_id']}").json()
    revealed = client.post(f"/api/v1/accounts/{account['account_id']}/reveal", json={"expected_revision":account["revision"]})
    assert revealed.status_code == 200 and "session" not in revealed.json()["fields"]
    assert "test-private-session" not in client.get("/api/v1/accounts").text


def test_user_account_page_renders_before_login_with_masked_api_fields(client):
    result = command(client, "create", "create-user-page", kind="TG_USER", label="User", fields={"api_id":"1234", "api_hash":"test-api-hash", "phone":"+123456789"})
    assert result.status_code == 200
    response = client.get("/settings/connections")
    assert response.status_code == 200 and "完成登录" in response.text
    assert "test-api-hash" not in response.text and "+123456789" not in response.text


def test_unauthenticated_account_api_refuses_reads_and_commands(client):
    client.cookies.clear()
    assert client.get("/api/v1/accounts").status_code == 401
    assert command(client, "create", "unauth", kind="EH", label="No", fields={}).status_code == 401


def test_websocket_catalogue_supports_shared_commands_but_excludes_secret_reveal(client):
    from app.api.realtime import command_catalogue
    catalogue = command_catalogue()
    assert catalogue["api.api_account_command"]["path"] == "/api/v1/accounts/commands"
    assert "api.api_account_reveal" not in catalogue


def test_removed_account_keeps_completed_enable_receipt_without_reactivation(client):
    account = create(client)
    account = command(client, "test", "verify-replay", account).json()["result"]
    enabled = command(client, "enable", "enable-replay", account)
    current = enabled.json()["result"]
    command(client, "delete", "delete-replay", current)
    client.portal.call(client.app.state.connection_manager.process_operations)
    assert command(client, "enable", "enable-replay", account).json() == enabled.json()
    assert client.get("/api/v1/accounts").json()["items"] == []


def test_completed_stop_result_keeps_its_revision_after_later_account_edits(client):
    account = create(client)
    stopping = command(client, "disable", "disable-result", account)
    assert stopping.status_code == 202
    client.portal.call(client.app.state.connection_manager.process_operations)
    path = f"/api/v1/accounts/operations/{stopping.json()['operation_id']}"
    completed = client.get(path).json()
    saved = completed["result"]
    assert completed["status"] == "COMPLETED" and not saved["enabled"]
    assert "test-private" not in client.get(path).text
    renamed = command(client, "rename", "rename-after-stop", saved, label="Another client")
    assert renamed.status_code == 200
    assert client.get(path).json() == completed
    assert renamed.json()["result"]["revision"] > saved["revision"]


def test_account_api_reports_runtime_health_separately_from_saved_configuration(client):
    account = create(client)
    runtime = client.get("/api/v1/accounts").json()["runtime"][account["account_id"]]
    assert runtime == {"state": "not_configured", "error": None}
    account = command(client, "test", "runtime-test", account).json()["result"]
    account = command(client, "enable", "runtime-enable", account).json()["result"]
    assert client.get("/api/v1/accounts").json()["runtime"][account["account_id"]]["state"] == "connected"
    assert "连接：正常" in client.get("/settings/connections").text
