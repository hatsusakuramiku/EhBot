"""Real SQLite races and public adapters for multi-client source edits."""
import asyncio
import json
from pathlib import Path
import sqlite3
from threading import Barrier
from uuid import uuid4

import pytest
import pytest_asyncio

from app.db.database import Database
from app.settings.source_commands import SourceCommands, SourceCommandError
from tests.integration.test_multi_account_source_management import managed_sources
from tests.integration.test_realtime import call, hello


@pytest_asyncio.fixture
async def sources(tmp_path):
    database = Database(tmp_path / "sources.db")
    await database.initialize()
    return SourceCommands(database)


def fields(chat=-100600, revision=0, name="Feed", key=None, **extra):
    return {"source_type": "CHANNEL", "chat_id": chat, "display_name": name,
            "expected_revision": revision, "allowed_archive_formats": ["zip"],
            "idempotency_key": key or str(uuid4()), **extra}


@pytest.mark.asyncio
async def test_competing_editors_one_winner_and_different_sources_do_not_conflict(sources, monkeypatch):
    first = (await sources.execute("save", fields()))["items"][0]
    second = (await sources.execute("save", fields(chat=-100601)))["items"][0]
    barrier = Barrier(2)
    original = sources._execute

    def racing(*args):
        barrier.wait(timeout=5)
        return original(*args)

    monkeypatch.setattr(sources, "_execute", racing)
    results = await asyncio.gather(
        sources.execute("save", fields(revision=first["revision"], name="Client A")),
        sources.execute("save", fields(revision=first["revision"], name="Client B")), return_exceptions=True,
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    assert [result.code for result in results if isinstance(result, SourceCommandError)] == ["REVISION_CONFLICT"]
    latest = (await sources.database.list_telegram_sources())
    current = next(row for row in latest if row.source_id == first["source_id"])
    results = await asyncio.gather(
        sources.execute("save", fields(revision=current.revision, name="First independent")),
        sources.execute("save", fields(chat=-100601, revision=second["revision"], name="Second independent")),
    )
    assert [result["status"] for result in results] == ["COMPLETED", "COMPLETED"]


@pytest.mark.asyncio
async def test_concurrent_same_key_replays_before_revision_check_and_different_payload_conflicts(sources, monkeypatch):
    payload = fields(key="lost-response")
    barrier = Barrier(2)
    original = sources._execute

    def racing(*args):
        barrier.wait(timeout=5)
        return original(*args)

    monkeypatch.setattr(sources, "_execute", racing)
    first, replay = await asyncio.gather(sources.execute("save", payload), sources.execute("save", payload))
    assert first == replay
    monkeypatch.setattr(sources, "_execute", original)
    revision = first["items"][0]["revision"]
    await sources.execute("save", fields(revision=revision, name="Newer edit"))
    assert await sources.execute("save", payload) == first
    assert await sources.receipt("lost-response") == first
    with pytest.raises(SourceCommandError, match="幂等键") as exc:
        await sources.execute("save", {**payload, "display_name": "Different request"})
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"
    assert (await sources.database.list_telegram_sources())[0].display_name == "Newer edit"


@pytest.mark.asyncio
async def test_partial_batch_receipt_replay_does_not_retry_failed_or_successful_items(sources):
    first = (await sources.execute("save", fields()))["items"][0]
    second = (await sources.execute("save", fields(chat=-100601)))["items"][0]
    await sources.execute("save", fields(revision=first["revision"], name="Newer edit"))
    payload = {"source_ids": [first["source_id"], second["source_id"], 9999],
               "expected_revisions": {str(first["source_id"]): first["revision"],
                                      str(second["source_id"]): second["revision"], "9999": 1},
               "idempotency_key": "partial-batch"}
    result = await sources.execute("enable", payload)
    assert result["status"] == "PARTIAL"
    assert [row["status"] for row in result["items"]] == ["FAILED", "COMPLETED", "FAILED"]
    assert [row["error"]["code"] for row in result["items"] if row["status"] == "FAILED"] == ["REVISION_CONFLICT", "SOURCE_NOT_FOUND"]
    before = await sources.database.list_telegram_sources()
    assert await sources.execute("enable", payload) == result
    assert await sources.database.list_telegram_sources() == before
    assert next(row for row in before if row.source_id == first["source_id"]).enabled is False
    assert next(row for row in before if row.source_id == second["source_id"]).enabled is True


@pytest.mark.asyncio
async def test_receipt_failure_rolls_back_all_source_mutations(sources):
    first = (await sources.execute("save", fields()))["items"][0]
    before = await sources.database.list_telegram_sources()
    with sources.database.connection() as c:
        c.execute("CREATE TRIGGER reject_source_receipt BEFORE INSERT ON source_command_receipts "
                  "BEGIN SELECT RAISE(ABORT, 'injected receipt failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected receipt failure"):
        await sources.execute("enable", {"source_ids": [first["source_id"]],
                                        "expected_revisions": {str(first["source_id"]): first["revision"]},
                                        "idempotency_key": "rolled-back"})
    assert await sources.database.list_telegram_sources() == before
    with pytest.raises(SourceCommandError) as exc:
        await sources.receipt("rolled-back")
    assert exc.value.code == "COMMAND_NOT_FOUND"


@pytest.mark.asyncio
async def test_runtime_cursor_does_not_advance_revision_but_legacy_configuration_writer_does(sources):
    first = (await sources.execute("save", fields()))["items"][0]
    with sources.database.connection() as c:
        c.execute("UPDATE telegram_sources SET last_message_id = 20 WHERE id = ?", (first["source_id"],))
    assert (await sources.database.list_telegram_sources())[0].revision == first["revision"]
    await sources.execute("save", fields(revision=first["revision"], name="After cursor"))
    current = (await sources.database.list_telegram_sources())[0]
    await sources.database.update_telegram_sources_bulk([first["source_id"]], enabled=True)
    assert (await sources.database.list_telegram_sources())[0].revision == current.revision + 1
    with pytest.raises(SourceCommandError) as exc:
        await sources.execute("save", fields(revision=current.revision, name="Stale after internal edit"))
    assert exc.value.code == "REVISION_CONFLICT"


@pytest.mark.asyncio
async def test_tombstone_requires_observed_version_and_batch_add_never_overwrites_existing_rules(sources):
    first = (await sources.execute("save", fields(enabled=True, required_tags="original")))["items"][0]
    entry = {"source_type": "CHANNEL", "chat_id": -100600, "display_name": "Picker alias", "expected_revision": first["revision"]}
    skipped = await sources.execute("add", {"entries": [entry], "idempotency_key": "skip"})
    assert skipped["items"][0]["status"] == "SKIPPED"
    assert (await sources.database.list_telegram_sources())[0].required_tags == ("original",)
    deleted = await sources.execute("delete", {"source_ids": [first["source_id"]],
                                              "expected_revisions": {str(first["source_id"]): first["revision"]},
                                              "idempotency_key": "delete"})
    assert await sources.database.list_telegram_sources() == []
    stale = await sources.execute("add", {"entries": [{**entry, "expected_revision": 0}], "idempotency_key": "stale-add"})
    assert stale["items"][0]["error"]["code"] == "REVISION_CONFLICT"
    assert await sources.database.list_telegram_sources() == []
    restored = await sources.execute("add", {"entries": [{**entry, "expected_revision": deleted["items"][0]["revision"]}],
                                            "idempotency_key": "explicit-add"})
    assert restored["status"] == "COMPLETED"
    row = (await sources.database.list_telegram_sources())[0]
    assert row.enabled is False and row.required_tags == () and row.display_name == "Feed"


@pytest.mark.asyncio
@pytest.mark.parametrize("revision", [None, -1, True, "1.0", "-1", ""])
async def test_missing_or_invalid_revision_is_refused(sources, revision):
    with pytest.raises(SourceCommandError) as exc:
        await sources.execute("save", fields(revision=revision))
    assert exc.value.code == "PRECONDITION_REQUIRED"
    assert await sources.database.list_telegram_sources() == []


@pytest.mark.asyncio
async def test_corrupt_historical_rules_can_be_repaired_with_the_observed_version(sources):
    first = (await sources.execute("save", fields()))["items"][0]
    with sources.database.connection() as c:
        c.execute("UPDATE telegram_sources SET rules_json = 'invalid old JSON' WHERE id = ?", (first["source_id"],))
    current = (await sources.database.list_telegram_sources())[0]
    with pytest.raises(SourceCommandError) as exc:
        await sources.execute("save", fields(revision=first["revision"]))
    assert exc.value.code == "REVISION_CONFLICT"
    assert exc.value.details["current"]["rules_json"] == "invalid old JSON"
    repaired = await sources.execute("save", fields(revision=current.revision, required_tags="repaired"))
    assert repaired["status"] == "COMPLETED"
    assert (await sources.database.list_telegram_sources())[0].required_tags == ("repaired",)


def test_032_upgrade_preserves_configuration_tombstones_and_cursor(tmp_path):
    db = Database(tmp_path / "old031.db")
    with db.connection() as c:
        c.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        for path in sorted(Path("app/db/migrations").glob("*.sql")):
            version = int(path.name.split("_", 1)[0])
            if version > 31:
                break
            c.executescript(path.read_text(encoding="utf-8"))
            c.execute("INSERT INTO schema_migrations VALUES(?, CURRENT_TIMESTAMP)", (version,))
            c.commit()
        account = db._ensure_bot_account(c)
        c.execute("INSERT INTO telegram_sources(account_id, source_type, chat_id, display_name, enabled, dismissed, last_message_id) "
                  "VALUES(?, 'CHANNEL', -100600, 'Old deleted', 0, 1, 123)", (account,))
    asyncio.run(db.initialize())
    asyncio.run(db.initialize())
    with db.connection() as c:
        assert c.execute("SELECT revision, dismissed, last_message_id, display_name FROM telegram_sources").fetchone() == (1, 1, 123, "Old deleted")
        assert c.execute("SELECT count(*) FROM schema_migrations WHERE version = 32").fetchone()[0] == 1


def test_http_web_ws_share_preconditions_receipts_and_explicit_draft_resolution(managed_sources):
    client, app, accounts, csrf = managed_sources
    headers = {"X-CSRF-Token": csrf}
    payload = fields(account_id=accounts[0]["account_id"], key="cross-transports")
    first = client.post("/api/v1/settings/sources", json=payload, headers=headers)
    assert first.status_code == 200
    row = first.json()["sources"][0]
    newer = fields(revision=row["revision"], account_id=accounts[0]["account_id"], name="Other device")
    assert client.post("/api/v1/settings/sources", json=newer, headers=headers).status_code == 200
    stale = fields(revision=row["revision"], account_id=accounts[0]["account_id"], name="My draft", required_tags="draft-tag", key="stale-ws")
    refused = client.post("/sources", data={**stale, "csrf_token": csrf})
    assert refused.status_code == 409
    assert refused.context["prefill"]["display_name"] == "My draft"
    assert str(refused.context["prefill"]["expected_revision"]) == str(row["revision"])
    assert 'name="confirmed_revision"' in refused.text
    assert 'value="draft-tag"' in refused.text
    assert client.get("/api/v1/settings/sources").json()["sources"][0]["display_name"] == "Other device"
    confirmed = client.post("/sources", data={**stale, "csrf_token": csrf,
                                              "confirmed_revision": refused.context["source_conflict"]["revision"]}, follow_redirects=False)
    assert confirmed.status_code == 303
    with client.websocket_connect("/ws/ui", headers={"Origin": "http://testserver"}) as ws:
        ready = hello(client, ws)
        assert "api.post_source" in ready["commands"]
        replay, _ = call(ws, "api.post_source", {"body": payload})
        assert replay["type"] == "command.result"
        assert replay["data"]["command"] == first.json()["command"]
        error, _ = call(ws, "api.post_source", {"body": stale}, request_id="stale-source")
        assert error["type"] == "command.error" and error["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        stale_again = {**stale, "idempotency_key": "stale-ws-2"}
        error, _ = call(ws, "api.post_source", {"body": stale_again}, request_id="stale-source-2")
        assert error["type"] == "command.error" and error["error"]["code"] == "REVISION_CONFLICT"
    receipt = client.get("/api/v1/settings/sources/commands/cross-transports")
    assert receipt.status_code == 200 and receipt.json() == first.json()["command"]
    missing = client.post("/sources", data={"csrf_token": csrf, "source_type": "CHANNEL", "chat_id": -100999,
                                           "display_name": "No protection"})
    assert missing.status_code == 428
    assert len(client.get("/api/v1/settings/sources").json()["sources"]) == 1


def test_source_commands_bearer_and_csrf_rules_cover_batch_add_and_receipt(managed_sources):
    client, app, accounts, csrf = managed_sources
    payload = {"account_id": accounts[0]["account_id"], "idempotency_key": "native-add", "entries": [
        {"source_type": "CHANNEL", "chat_id": -100777, "display_name": "Native", "expected_revision": 0},
    ]}
    assert client.post("/api/v1/settings/sources/batch-add", json=payload).status_code == 403
    token = client.post("/api/v1/auth/login", json={"username": "admin", "password": "new-password-with-12-characters"}).json()["access_token"]
    client.cookies.clear()
    headers = {"Authorization": "Bearer " + token}
    added = client.post("/api/v1/settings/sources/batch-add", json=payload, headers=headers)
    assert added.status_code == 200 and added.json()["command"]["items"][0]["created"] is True
    replay = client.post("/api/v1/settings/sources/batch-add", json=payload, headers=headers)
    assert replay.json()["command"] == added.json()["command"]
    assert client.get("/api/v1/settings/sources/commands/native-add").status_code == 401
    assert client.get("/api/v1/settings/sources/commands/native-add", headers=headers).json() == added.json()["command"]


def test_batch_forms_and_ws_preserve_old_versions_and_report_partial_results(managed_sources):
    client, app, accounts, csrf = managed_sources
    headers = {"X-CSRF-Token": csrf}
    for chat in (-100601, -100602):
        assert client.post("/api/v1/settings/sources", json=fields(chat=chat, account_id=accounts[0]["account_id"]), headers=headers).status_code == 200
    rows = client.get("/api/v1/settings/sources").json()["sources"]
    first, second = rows
    assert client.post("/api/v1/settings/sources", json=fields(chat=first["chat_id"], revision=first["revision"],
                                                            account_id=accounts[0]["account_id"], name="Newer source"), headers=headers).status_code == 200
    versions = json.dumps({str(row["source_id"]): row["revision"] for row in rows})
    form = {"csrf_token": csrf, "action": "apply", "source_ids": [str(row["source_id"]) for row in rows],
            "expected_revisions": versions, "idempotency_key": "web-partial",
            "required_tags": "draft-rules", "allowed_archive_formats": ["rar"], "min_rating": "4.1"}
    response = client.post("/sources/batch", data=form)
    assert response.status_code == 200
    command = response.context["source_command"]
    assert command["status"] == "PARTIAL"
    assert response.context["source_draft"]["expected_revisions"] == versions
    assert 'value="draft-rules"' in response.text and 'value="4.1"' in response.text
    after = {row["source_id"]: row for row in client.get("/api/v1/settings/sources").json()["sources"]}
    assert after[first["source_id"]]["required_tags"] == []
    assert after[second["source_id"]]["required_tags"] == ["draft-rules"]
    with client.websocket_connect("/ws/ui", headers={"Origin": "http://testserver"}) as ws:
        hello(client, ws)
        replay, _ = call(ws, "api.put_sources_batch", {"body": {**form, "expected_revisions": json.loads(versions)}})
        assert replay["type"] == "command.result" and replay["data"]["command"] == command
        unprotected, _ = call(ws, "web.sources_batch_action", {"body": [
            ["action", "enable"], ["source_ids", str(first["source_id"])], ["idempotency_key", "missing-versions"],
        ]}, request_id="missing-versions")
        assert unprotected["type"] == "command.error" and unprotected["status"] == 428
        assert "请提交每条来源已读取的版本" in unprotected["data"]
    assert {row["source_id"]: row for row in client.get("/api/v1/settings/sources").json()["sources"]} == after


def test_dialog_picker_embeds_observed_versions_and_web_batch_add_replays(managed_sources):
    client, app, accounts, csrf = managed_sources
    account_id = accounts[1]["account_id"]
    created = client.post("/api/v1/settings/sources", json=fields(chat=-100502, account_id=account_id), headers={"X-CSRF-Token": csrf})
    row = created.json()["sources"][0]
    page = client.post("/sources/dialogs", data={"csrf_token": csrf, "account_id": account_id})
    assert '"expected_revision": 1' in page.text
    picked = client.post("/sources/dialogs/select", data={"csrf_token": csrf, "account_id": account_id,
                                                         "source_type": "CHANNEL", "chat_id": -100502, "display_name": "Picked"})
    assert picked.context["prefill"]["expected_revision"] == row["revision"]
    form = {"csrf_token": csrf, "account_id": account_id, "idempotency_key": "web-add-replay", "dialogs": [
        json.dumps({"source_type": "CHANNEL", "chat_id": -100503, "display_name": "New feed", "expected_revision": 0}),
    ]}
    first = client.post("/sources/batch-add", data=form)
    assert first.status_code == 200
    replay = client.post("/sources/batch-add", data=form)
    assert replay.context["source_command"] == first.context["source_command"]
    assert len(replay.context["sources"]) == 2
