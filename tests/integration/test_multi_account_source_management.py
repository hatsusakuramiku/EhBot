"""Account identity survives source snapshots, dialog picking and row edits."""
from functools import partial
import json
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from tests.integration.test_telegram_user_web import (
    StubChannel, StubTelethonClient, authenticate, make_app,
)
from tests.integration.test_realtime import call, hello


@pytest.fixture
def managed_sources(tmp_path):
    app, settings, _ = make_app(tmp_path, {})
    clients = {
        "source-session-1": StubTelethonClient({
            "dialogs": [{"id": -100501, "entity": StubChannel("First feed")}],
        }),
        "source-session-2": StubTelethonClient({
            "dialogs": [{"id": -100502, "entity": StubChannel("Second feed")}],
        }),
    }
    with TestClient(app) as client:
        authenticate(client, settings)
        manager = app.state.connection_manager
        manager._user_client_factory = lambda api_id, api_hash, session: clients[session]

        async def create_accounts():
            accounts = []
            for number in (1, 2):
                account = await manager.registry.create("TG_USER", f"User {number}", {
                    "api_id": str(number), "api_hash": str(number) * 32,
                })
                account = await manager.registry.save_session(
                    account["account_id"], expected_revision=account["revision"],
                    session=f"source-session-{number}", remote_identity=str(number),
                    platform_name=f"User {number}",
                )
                accounts.append(await manager.enable_account(
                    account["account_id"], expected_revision=account["revision"],
                ))
            return accounts

        accounts = client.portal.call(create_accounts)
        csrf = client.get("/settings/sources").context["csrf_token"]
        yield client, app, accounts, csrf


def test_same_chat_sources_are_visible_and_edits_preserve_the_selected_account(managed_sources):
    client, app, accounts, csrf = managed_sources
    for number, account in enumerate(accounts, 1):
        response = client.post("/api/v1/settings/sources", headers={"X-CSRF-Token": csrf}, json={
            "expected_revision": 0, "idempotency_key": str(uuid4()),
            "account_id": account["account_id"], "source_type": "CHANNEL", "chat_id": -100600,
            "display_name": f"Feed {number}", "allowed_archive_formats": ["zip"],
        })
        assert response.status_code == 200
    snapshot = client.get("/api/v1/settings/sources").json()
    rows = {row["account_id"]: row for row in snapshot["sources"]}
    assert set(rows) == {account["account_id"] for account in accounts}
    assert {row["account_label"] for row in rows.values()} == {"User 1", "User 2"}
    assert {row["account_kind"] for row in rows.values()} == {"TG_USER"}
    page = client.get("/settings/sources")
    assert page.context["sources"] == snapshot["sources"]
    for account in accounts:
        assert f'name="account_id" value="{account["account_id"]}"' in page.text
    assert "1" * 32 not in json.dumps(snapshot)
    assert "2" * 32 not in json.dumps(snapshot)
    assert "source-session-" not in page.text
    updated = client.post("/sources", data={
        "expected_revision": rows[accounts[1]["account_id"]]["revision"], "idempotency_key": str(uuid4()),
        "csrf_token": csrf, "account_id": accounts[1]["account_id"], "source_type": "CHANNEL",
        "chat_id": "-100600", "display_name": "Edited second", "allowed_archive_formats": ["cbz"],
    }, follow_redirects=False)
    assert updated.status_code == 303
    after = {row["account_id"]: row for row in client.get("/api/v1/settings/sources").json()["sources"]}
    assert after[accounts[0]["account_id"]] == rows[accounts[0]["account_id"]]
    assert after[accounts[1]["account_id"]]["display_name"] == "Edited second"
    assert after[accounts[1]["account_id"]]["allowed_archive_formats"] == ["cbz"]
    assert len(after) == 2


def test_dialog_api_and_web_read_only_the_named_account_and_keep_separate_caches(managed_sources):
    client, app, accounts, csrf = managed_sources
    for number, account in enumerate(accounts, 1):
        listed = client.post("/api/v1/settings/sources/dialogs", json={"account_id": account["account_id"]},
                             headers={"X-CSRF-Token": csrf})
        assert listed.status_code == 200
        assert listed.json()["total"] == 1
        assert listed.json()["items"][0]["chat_id"] == -100500 - number
        assert listed.json()["items"][0]["account_id"] == account["account_id"]
        page = client.post("/sources/dialogs", data={"csrf_token": csrf, "account_id": account["account_id"]})
        assert page.status_code == 200
        assert page.context["dialogs"] == listed.json()["items"]
        assert f'name="account_id" value="{account["account_id"]}"' in page.text
    manager = app.state.connection_manager
    assert manager._listener(accounts[0]["account_id"])._user_entities.get(-100501) is not None
    assert manager._listener(accounts[0]["account_id"])._user_entities.get(-100502) is None
    assert manager._listener(accounts[1]["account_id"])._user_entities.get(-100502) is not None
    assert manager._listener(accounts[1]["account_id"])._user_entities.get(-100501) is None
    assert client.get("/api/v1/settings/sources").json()["sources"] == []
    with client.websocket_connect("/ws/ui", headers={"Origin": "http://testserver"}) as ws:
        ready = hello(client, ws)
        assert "api.api_source_dialogs" in ready["commands"]
        result, _ = call(ws, "api.api_source_dialogs", {"body": {"account_id": accounts[1]["account_id"]}})
        assert result["type"] == "command.result"
        assert result["data"] == listed.json()
    token = client.post("/api/v1/auth/login", json={
        "username": "admin", "password": "new-password-with-12-characters",
    }).json()["access_token"]
    client.cookies.clear()
    native = client.post("/api/v1/settings/sources/dialogs", json={"account_id": accounts[1]["account_id"]},
                         headers={"Authorization": "Bearer " + token})
    assert native.status_code == 200
    assert native.json() == listed.json()


def test_ambiguous_dialog_read_requires_account_and_never_chooses_the_first(managed_sources):
    client, app, accounts, csrf = managed_sources
    refused = client.post("/api/v1/settings/sources/dialogs", json={}, headers={"X-CSRF-Token": csrf})
    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "ACCOUNT_SELECTION_REQUIRED"
    page = client.post("/sources/dialogs", data={"csrf_token": csrf})
    assert page.status_code == 400
    assert "请选择要读取会话的 TG 用户账号" in page.text


def test_picking_and_batch_adding_dialogs_keep_their_account(managed_sources):
    client, app, accounts, csrf = managed_sources
    account_id = accounts[1]["account_id"]
    picked = client.post("/sources/dialogs/select", data={
        "csrf_token": csrf, "account_id": account_id, "source_type": "CHANNEL",
        "chat_id": "-100502", "display_name": "Second feed",
    })
    assert picked.status_code == 200
    assert picked.context["prefill"]["account_id"] == account_id
    assert f'value="{account_id}" selected' in picked.text
    added = client.post("/sources/batch-add", data={
        "idempotency_key": str(uuid4()),
        "csrf_token": csrf, "account_id": account_id,
        "dialogs": [json.dumps({"source_type": "CHANNEL", "chat_id": -100502, "display_name": "Second feed", "expected_revision": 0})],
    })
    assert added.status_code == 200
    rows = added.context["sources"]
    assert len(rows) == 1 and rows[0]["account_id"] == account_id
    assert rows[0]["enabled"] is False
    assert client.portal.call(partial(app.state.database.telegram_ingest_targets, accounts[0]["account_id"])) == []


@pytest.mark.parametrize("account_kind", ["TG_BOT", "EH", "MISSING", "DISABLED"])
def test_dialog_read_rejects_wrong_missing_or_disabled_account(managed_sources, account_kind):
    client, app, accounts, csrf = managed_sources
    registry = app.state.connection_manager.registry
    if account_kind in {"TG_BOT", "EH"}:
        account = client.portal.call(registry.create, account_kind, "Other account", {})
        account_id = account["account_id"]
        code, status = "ACCOUNT_KIND_INVALID", 400
    elif account_kind == "DISABLED":
        account = client.portal.call(registry.create, "TG_USER", "Disabled", {})
        account_id = account["account_id"]
        code, status = "ACCOUNT_UNAVAILABLE", 400
    else:
        account_id = "missing-account"
        code, status = "ACCOUNT_NOT_FOUND", 404
    response = client.post("/api/v1/settings/sources/dialogs", json={"account_id": account_id},
                           headers={"X-CSRF-Token": csrf})
    assert response.status_code == status
    assert response.json()["error"]["code"] == code


def test_dialog_api_requires_authentication_and_csrf(tmp_path):
    app, settings, _ = make_app(tmp_path, {})
    with TestClient(app) as client:
        assert client.post("/api/v1/settings/sources/dialogs", json={}).status_code == 401
        authenticate(client, settings)
        assert client.post("/api/v1/settings/sources/dialogs", json={}).status_code == 403


@pytest.mark.parametrize("kind", ["EH", "MISSING"])
def test_source_writes_reject_invalid_account_without_partial_sources(managed_sources, kind):
    client, app, accounts, csrf = managed_sources
    account_id = "missing-account"
    if kind == "EH":
        account_id = client.portal.call(app.state.connection_manager.registry.create, "EH", "EH", {})["account_id"]
    fields = {"account_id": account_id, "source_type": "CHANNEL", "chat_id": -100999,
              "expected_revision": 0, "idempotency_key": str(uuid4()),
              "display_name": "Invalid account source", "allowed_archive_formats": ["zip"]}
    api = client.post("/api/v1/settings/sources", json=fields, headers={"X-CSRF-Token": csrf})
    assert api.status_code == 400 and api.json()["error"]["code"] == "ACCOUNT_INVALID"
    page = client.post("/sources", data={**fields, "csrf_token": csrf})
    assert page.status_code == 400
    batch = client.post("/sources/batch-add", data={
        "idempotency_key": str(uuid4()),
        "csrf_token": csrf, "account_id": account_id, "dialogs": [json.dumps(fields)],
    })
    assert batch.status_code == 400
    assert client.get("/api/v1/settings/sources").json()["sources"] == []
