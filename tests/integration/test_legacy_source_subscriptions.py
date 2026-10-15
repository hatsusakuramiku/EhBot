"""Source configuration migration never invents historical receiving accounts."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from app.connections.accounts import AccountRegistry
from app.db.database import Database
from app.secrets import SecretStore
from app.settings.source_commands import SourceCommands, SourceCommandError
from tests.integration.test_ingest_migration import legacy_database


def setup_legacy(tmp_path, *, kinds=("TG_BOT", "TG_USER")):
    db = legacy_database(tmp_path)
    db._configure_telegram_source_sync("CHANNEL", -1000000000600, "Shared feed", True, ("zip",), 128, ("keep",))
    db._configure_telegram_source_sync("CHANNEL", -1000000000601, "Deleted feed", False, (), 0)
    db._dismiss_telegram_sources_sync([2])
    db._set_source_cursor_sync(-1000000000600, 37)
    with db.connection() as c:
        account_id = db._ensure_bot_account(c)
        c.execute("INSERT INTO source_messages(account_id, chat_id, message_id, message_text, message_date, filter_result) "
                  "VALUES(?, -1000000000600, 12, 'Historical message', '2026-10-13', 'IGNORE')", (account_id,))
    asyncio.run(db.initialize())
    secrets = SecretStore(tmp_path / "private")
    if "TG_BOT" in kinds:
        secrets.write("telegram_bot_token", "legacy-test-token")
    if "TG_USER" in kinds:
        secrets.write("telegram_user_api", "1234:legacy-test-api-hash")
    return db, AccountRegistry(db, secrets)


def test_legacy_rules_copy_to_both_slots_keep_user_cursor_and_unknown_observations(tmp_path):
    db, registry = setup_legacy(tmp_path)
    with db.connection() as c:
        before = c.execute("SELECT * FROM source_messages").fetchall()
        legacy_account = c.execute("SELECT account_id FROM telegram_sources WHERE id = 1").fetchone()[0]
    imported = asyncio.run(registry.import_legacy())
    rows = asyncio.run(db.list_telegram_sources())
    assert len(rows) == 2
    assert {row.account_id for row in rows} == set(imported.values())
    assert all(row.enabled and row.display_name == "Shared feed" and row.required_tags == ("keep",) for row in rows)
    assert asyncio.run(db.telegram_ingest_targets(imported["TG_USER"]))[0]["cursor"] == 37
    assert asyncio.run(db.telegram_ingest_targets(imported["TG_BOT"]))[0]["cursor"] == 0
    assert asyncio.run(db.telegram_ingest_targets()) == []
    versions = asyncio.run(SourceCommands(db).versions())
    assert len(versions) == 4 and sum(row["dismissed"] for row in versions) == 2
    with db.connection() as c:
        assert c.execute("SELECT * FROM source_messages").fetchall() == before
        assert c.execute("SELECT external_account_id FROM telegram_accounts WHERE id = ?", (legacy_account,)).fetchone() == (None,)
        assert c.execute("SELECT count(*) FROM telegram_sources WHERE legacy_retired = 1").fetchone() == (2,)
        assert c.execute("SELECT count(*) FROM legacy_telegram_source_imports").fetchone() == (2,)


def test_restart_keeps_edits_and_deletions_and_does_not_apply_rules_to_new_accounts(tmp_path):
    db, registry = setup_legacy(tmp_path)
    imported = asyncio.run(registry.import_legacy())
    rows = asyncio.run(db.list_telegram_sources())
    user = next(row for row in rows if row.account_id == imported["TG_USER"])
    bot = next(row for row in rows if row.account_id == imported["TG_BOT"])
    asyncio.run(db.configure_telegram_source(account_id=user.account_id, source_type="CHANNEL", chat_id=user.chat_id,
                                             display_name="Edited user rules", enabled=False, allowed_archive_formats=("cbz",), max_attachment_size_mb=0))
    asyncio.run(db.dismiss_telegram_sources([bot.source_id]))
    unrelated = asyncio.run(registry.create("TG_USER", "New account", {}))
    asyncio.run(db.initialize())
    assert asyncio.run(registry.import_legacy()) == imported
    after = asyncio.run(db.list_telegram_sources())
    assert len(after) == 1 and after[0].display_name == "Edited user rules"
    assert after[0].enabled is False and after[0].allowed_archive_formats == ("cbz",)
    assert asyncio.run(db.telegram_ingest_targets(unrelated["account_id"])) == []
    with db.connection() as c:
        assert c.execute("SELECT dismissed FROM telegram_sources WHERE id = ?", (bot.source_id,)).fetchone() == (1,)


def test_existing_account_specific_configuration_wins_and_deleted_slot_is_not_revived(tmp_path):
    db, registry = setup_legacy(tmp_path)
    user = registry._create_sync("TG_USER", "Already imported", {"api_id": "1"}, "TG_USER")
    asyncio.run(db.configure_telegram_source(account_id=user["account_id"], source_type="CHANNEL", chat_id=-1000000000600,
                                             display_name="Account-specific", enabled=False, allowed_archive_formats=("rar",), max_attachment_size_mb=256))
    with db.connection() as c:
        before = c.execute("SELECT * FROM telegram_sources WHERE account_id IN "
                           "(SELECT id FROM telegram_accounts WHERE external_account_id = ?)", (user["account_id"],)).fetchall()
    imported = asyncio.run(registry.import_legacy())
    with db.connection() as c:
        after = c.execute("SELECT * FROM telegram_sources WHERE account_id IN "
                          "(SELECT id FROM telegram_accounts WHERE external_account_id = ?) AND chat_id = -1000000000600", (user["account_id"],)).fetchall()
        assert after == before
        c.execute("UPDATE external_accounts SET deleted_at = CURRENT_TIMESTAMP WHERE account_id = ?", (user["account_id"],))
    assert "TG_USER" not in asyncio.run(registry.import_legacy())
    assert imported["TG_USER"] == user["account_id"]


def test_later_imported_legacy_slot_uses_retained_snapshot_without_overwriting_first(tmp_path):
    db, registry = setup_legacy(tmp_path, kinds=("TG_USER",))
    imported = asyncio.run(registry.import_legacy())
    user = asyncio.run(db.list_telegram_sources())[0]
    asyncio.run(db.dismiss_telegram_sources([user.source_id]))
    registry._secrets.write("telegram_bot_token", "later-legacy-test-token")
    second = asyncio.run(registry.import_legacy())
    assert second["TG_USER"] == imported["TG_USER"]
    rows = asyncio.run(db.list_telegram_sources())
    assert len(rows) == 1 and rows[0].account_id == second["TG_BOT"] and rows[0].enabled is True


def test_copy_and_completion_marker_roll_back_together_after_failure(tmp_path):
    db, registry = setup_legacy(tmp_path)
    with db.connection() as c:
        c.execute("CREATE TRIGGER fail_source_import BEFORE INSERT ON legacy_telegram_source_imports "
                  "WHEN (SELECT kind FROM external_accounts WHERE account_id = NEW.account_id) = 'TG_USER' "
                  "BEGIN SELECT RAISE(ABORT, 'injected source import failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected source import failure"):
        asyncio.run(registry.import_legacy())
    with db.connection() as c:
        assert c.execute("SELECT count(*) FROM legacy_telegram_source_imports").fetchone() == (0,)
        assert c.execute("SELECT count(*) FROM telegram_sources WHERE legacy_retired = 1").fetchone() == (0,)
        assert c.execute("SELECT count(*) FROM telegram_sources").fetchone() == (2,)
        c.execute("DROP TRIGGER fail_source_import")
    asyncio.run(registry.import_legacy())
    assert len(asyncio.run(db.list_telegram_sources())) == 2


def test_concurrent_source_import_has_one_copy_and_marker_per_legacy_account(tmp_path):
    db, registry = setup_legacy(tmp_path)
    # Register the legacy account slots first so this race isolates the
    # configuration import; account registration has its own concurrency tests.
    registry._create_sync("TG_USER", "Imported user", {"api_id": "1"}, "TG_USER")
    registry._create_sync("TG_BOT", "Imported bot", {"bot_token": "fixture-token"}, "TG_BOT")
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda _: registry._import_legacy_sources_sync(), range(2)))
    with db.connection() as c:
        assert c.execute("SELECT count(*) FROM legacy_telegram_source_imports").fetchone() == (2,)
        assert c.execute("SELECT count(*) FROM telegram_sources").fetchone() == (6,)
    assert len(asyncio.run(db.list_telegram_sources())) == 2


def test_stale_legacy_edit_or_batch_cannot_change_retired_rules(tmp_path):
    db, registry = setup_legacy(tmp_path)
    original = asyncio.run(db.list_telegram_sources())[0]
    asyncio.run(registry.import_legacy())
    commands = SourceCommands(db)
    with pytest.raises(SourceCommandError) as exc:
        asyncio.run(commands.execute("save", {"source_type": "CHANNEL", "chat_id": original.chat_id, "display_name": "Stale",
                                                "expected_revision": original.revision, "idempotency_key": "stale-save"}))
    assert exc.value.code == "ACCOUNT_SELECTION_REQUIRED"
    result = asyncio.run(commands.execute("enable", {"source_ids": [original.source_id],
                                                      "expected_revisions": {str(original.source_id): original.revision},
                                                      "idempotency_key": "stale-batch"}))
    assert result["items"][0]["error"]["code"] == "REVISION_CONFLICT"
    with db.connection() as c:
        version = c.execute("SELECT revision FROM telegram_sources WHERE id = ?", (original.source_id,)).fetchone()[0]
    result = asyncio.run(commands.execute("enable", {"source_ids": [original.source_id],
                                                      "expected_revisions": {str(original.source_id): version},
                                                      "idempotency_key": "retired-batch"}))
    assert result["items"][0]["error"]["code"] == "SOURCE_MIGRATED"
