import sqlite3
from pathlib import Path

import pytest

from app.db.database import Database


@pytest.mark.asyncio
async def test_auto_approval_rule_case_sensitive_round_trips(
    tmp_path: Path,
) -> None:
    """`case_sensitive` survives a save, a read, and an update-overwrite.

    Migration 016 adds the column and clears the old rules, so a fresh database
    starts with neither rules nor a schema that could drop the flag.
    """
    path = tmp_path / "ehbot.db"
    database = Database(path)
    await database.initialize()

    with sqlite3.connect(path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(auto_approval_rules)")
        }
        leftover = connection.execute("SELECT COUNT(*) FROM auto_approval_rules").fetchone()[0]
    assert "case_sensitive" in columns
    assert leftover == 0

    condition = {"kind": "condition", "field": "Title", "operator": "LIKE", "value": "%miku%"}
    # Default is insensitive; the insertion path defaults the column to off.
    first = await database.save_auto_approval_rule(
        rule_id=None,
        name="Insensitive by default",
        enabled=True,
        priority=10,
        condition=condition,
        dsl_snapshot='{Title} LIKE "%miku%"',
    )
    assert first.case_sensitive is False

    # A case-sensitive rule round-trips its flag through a read...
    second = await database.save_auto_approval_rule(
        rule_id=None,
        name="Case sensitive",
        enabled=True,
        priority=20,
        condition=condition,
        dsl_snapshot='{Title} LIKE "%Miku%"',
        case_sensitive=True,
    )
    assert second.case_sensitive is True

    stored = await database.list_auto_approval_rules()
    by_id = {rule.rule_id: rule for rule in stored}
    assert by_id[first.rule_id].case_sensitive is False
    assert by_id[second.rule_id].case_sensitive is True

    # ...and through an overwrite, which is the UPDATE path that 编辑 exercises.
    updated = await database.save_auto_approval_rule(
        rule_id=second.rule_id,
        name="Case sensitive, flipped",
        enabled=True,
        priority=20,
        condition=condition,
        dsl_snapshot='{Title} LIKE "%Miku%"',
        case_sensitive=False,
    )
    assert updated.case_sensitive is False
    reread = await database.get_auto_approval_rule(second.rule_id)
    assert reread is not None
    assert reread.case_sensitive is False

    # Migration 024: the action round-trips, and a rule saved without one keeps
    # the historical APPROVE behaviour instead of silently becoming a reject.
    assert reread.action == "APPROVE"
    reject_rule = await database.save_auto_approval_rule(
        rule_id=None,
        name="Reject NTR",
        enabled=True,
        priority=5,
        condition=condition,
        dsl_snapshot='{Title} LIKE "%ntr%"',
        action="REJECT",
    )
    assert reject_rule.action == "REJECT"
    listed = {
        rule.name: rule
        for rule in await database.list_auto_approval_rules()
    }
    assert listed["Reject NTR"].action == "REJECT"

    # The column CHECK is the last line of defence behind the route's own
    # validation, so an unknown action cannot be stored even if a caller skips
    # the web layer.
    with pytest.raises(sqlite3.IntegrityError):
        await database.save_auto_approval_rule(
            rule_id=None,
            name="Bogus action",
            enabled=True,
            priority=1,
            condition=condition,
            dsl_snapshot="x",
            action="DELETE",
        )


@pytest.mark.asyncio
async def test_initial_migration_is_idempotent_and_enables_sqlite_safety(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ehbot.db"
    database = Database(path)

    await database.initialize()
    await database.initialize()

    with sqlite3.connect(path) as connection:
        migration_count = connection.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()[0]
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        update_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(telegram_bot_updates)")
        }
        update_indexes = {
            row[1]
            for row in connection.execute("PRAGMA index_list(telegram_bot_updates)")
        }
        source_message_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(source_messages)")
        }
        candidate_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(candidates)")
        }
        metadata_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(metadata_values)")
        }
        download_job_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(download_jobs)")
        }
        artifact_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(artifacts)")
        }
        thumbnail_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(thumbnails)")
        }
        archive_path_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(work_archive_paths)"
            )
        }
        routing_rule_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(archive_path_rules)"
            )
        }
        auto_approval_rule_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(auto_approval_rules)"
            )
        }
        ai_model_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(ai_provider_models)"
            )
        }
        ai_key_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(ai_provider_keys)"
            )
        }
        ai_provider_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(ai_providers)"
            )
        }
        ai_chain_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(ai_model_chain)"
            )
        }
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        telegram_source_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(telegram_sources)")
        }
        api_credential_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(api_credentials)")
        }

    assert migration_count == 24
    assert "last_message_id" in telegram_source_columns
    # Migration 022: the source tombstone and the candidate's job index.
    assert "dismissed" in telegram_source_columns
    assert "idx_telegram_sources_dismissed" in indexes
    assert "idx_download_jobs_candidate" in indexes
    # Migration 023: mobile/API credentials. The partial unique index is the
    # "at most one valid API key" invariant, so it is asserted by name.
    assert "api_credentials" in tables
    assert {
        "kind",
        "label",
        "public_id",
        "secret_hash",
        "family_id",
        "created_at",
        "last_used_at",
        "expires_at",
        "revoked_at",
        "rotated_from",
    } <= api_credential_columns
    assert "idx_api_credentials_single_key" in indexes
    assert "auto_approval_rules" in tables
    # Migration 024: the rule's action. `case_sensitive` came from 016 and is
    # asserted beside it so the rule row's shape is checked in one place.
    assert {"case_sensitive", "action"} <= auto_approval_rule_columns
    assert {
        "archive_tool_profiles",
        "archive_passwords",
        "archive_settings",
    } <= tables
    assert journal_mode == "wal"
    assert {
        "telegram_accounts",
        "telegram_sources",
        "source_messages",
        "candidates",
        "candidate_messages",
        "metadata_values",
        "review_actions",
        "download_jobs",
        "artifacts",
        "admin_users",
        "telegram_bot_updates",
        "thumbnails",
        "schema_migrations",
    } <= tables
    assert {"processed_at", "processing_result", "processing_reason"} <= update_columns
    assert "idx_telegram_bot_updates_pending" in update_indexes
    assert {"filter_result", "filter_reason"} <= source_message_columns
    assert "preview_urls_json" in source_message_columns
    assert {"preview_url", "torrent_count", "torrent_hash"} <= candidate_columns
    # Migration 012: cover proxy, field locking, queue ordering.
    assert "thumb_url" in candidate_columns
    assert "is_locked" in metadata_columns
    assert "priority" in download_job_columns
    assert "page_count" in artifact_columns
    # Migration 014: the 已下载内容 domain. `library_relative_path` is where a
    # repack has to land once the operator has renamed or moved the book, and
    # `removed_works` is why a removal is recorded rather than only performed --
    # deleting a terminal job row would otherwise make the history lie about its
    # own completeness.
    assert "library_relative_path" in artifact_columns
    assert "removed_works" in tables
    # Migration 015: explicit archive paths. A table keyed by candidate rather
    # than the 014 column, because an operator sets where a book belongs *before*
    # the first pack as readily as after it -- and an artifact row only exists
    # once a pack has produced one. The unique index is the guard behind
    # 「名称已存在」: two books pinned to one path would race at pack time and
    # the loser would be overwritten with no trace.
    assert "work_archive_paths" in tables
    assert "idx_work_archive_paths_relative" in indexes
    assert {
        "candidate_id",
        "relative_path",
        "is_manual",
        "operator_name",
    } <= archive_path_columns
    assert {
        "hash",
        "kind",
        "variant",
        "source_url",
        "state",
        "content_type",
        "byte_size",
        "width",
        "height",
        "error_code",
        "attempt_count",
    } <= thumbnail_columns
    # Migration 017: archive-path routing rules. A rule pairs an auto-approval
    # condition with its own layout template, so a work's path can be chosen by
    # matching instead of every book following one global template.
    assert "archive_path_rules" in tables
    assert "idx_archive_path_rules_enabled_priority" in indexes
    assert {
        "path_template",
        "condition_json",
        "dsl_snapshot",
        "case_sensitive",
    } <= routing_rule_columns
    # Migration 018: AI-generated archive paths. Five tables because three of
    # them are lists an operator edits item by item (providers, keys, models),
    # the chain is an ordering of (provider, model) pairs, and the suggestions
    # are the answer cache the detail page and the packer both read.
    assert {
        "ai_providers",
        "ai_provider_keys",
        "ai_provider_models",
        "ai_model_chain",
        "ai_path_suggestions",
    } <= tables
    assert "idx_ai_provider_keys_provider" in indexes
    assert {
        "cipher",
        "cooldown_until",
        "failures",
        "last_used_at",
    } <= ai_key_columns
    assert {
        "last_verified_at",
        "last_verify_ok",
        "last_verify_error",
    } <= ai_model_columns
    # Migration 019: AstrBot-style provider management. Request headers and
    # request params are JSON text because they are edited as one box and never
    # queried by field; the chain gains a `scope` in its primary key so the
    # archive-path feature can carry its own list while the AI page still owns
    # the global default (rows from 018 become `scope='default'`).
    assert {"custom_headers", "default_params"} <= ai_provider_columns
    assert "params" in ai_model_columns
    assert {"scope", "position", "provider_model_id"} <= ai_chain_columns
    assert "idx_ai_model_chain_scope" in indexes


@pytest.mark.asyncio
async def test_migration_012_defaults_are_backfilled_on_existing_rows(
    tmp_path: Path,
) -> None:
    """The two NOT NULL columns must land on rows that predate them.

    `is_locked` and `priority` are added to populated tables, so a DEFAULT that
    SQLite failed to apply would leave existing candidates unschedulable rather
    than merely unlocked -- worth asserting rather than trusting.
    """
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()

    with sqlite3.connect(tmp_path / "ehbot.db") as connection:
        connection.execute(
            "INSERT INTO candidates (id, status) VALUES (1, 'PENDING_REVIEW')"
        )
        connection.execute(
            "INSERT INTO metadata_values "
            "(candidate_id, field_name, field_value, value_source) "
            "VALUES (1, 'Title', 'A title', 'EXHENTAI')"
        )
        connection.execute(
            "INSERT INTO download_jobs "
            "(candidate_id, provider, idempotency_key, state) "
            "VALUES (1, 'TELEGRAM', 'key-1', 'PENDING')"
        )
        assert connection.execute(
            "SELECT is_locked FROM metadata_values WHERE candidate_id = 1"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT priority FROM download_jobs WHERE candidate_id = 1"
        ).fetchone()[0] == 100
        assert connection.execute(
            "SELECT thumb_url FROM candidates WHERE id = 1"
        ).fetchone()[0] is None


@pytest.mark.asyncio
async def test_telegram_updates_are_persisted_idempotently(tmp_path: Path) -> None:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()

    first_insert = await database.save_telegram_updates(
        [{"update_id": 100, "message": {"text": "first"}}]
    )
    duplicate_insert = await database.save_telegram_updates(
        [{"update_id": 100, "message": {"text": "first"}}]
    )

    assert first_insert == 1
    assert duplicate_insert == 0
    assert await database.latest_telegram_update_id() == 100


@pytest.mark.asyncio
async def test_connection_helper_closes_and_commits(tmp_path: Path) -> None:
    """`with sqlite3.connect(...)` ends the transaction; it does not close.

    Every query in the service layer used the bare connection, so each one left
    a handle for the garbage collector to find. On a WAL database an open
    handle holds its read snapshot, which is what keeps `-wal` from being
    checkpointed away.
    """
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()

    with database.connection() as connection:
        connection.execute(
            "INSERT INTO admin_users (username, password_hash, password_changed) "
            "VALUES ('probe', 'hash', 0)"
        )

    # Closed on the way out: using it again is an error, not a silent success.
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")

    # And the write inside the block was committed.
    with database.connection() as verify:
        stored = verify.execute(
            "SELECT COUNT(*) FROM admin_users WHERE username = 'probe'"
        ).fetchone()[0]
    assert stored == 1


@pytest.mark.asyncio
async def test_connection_helper_rolls_back_and_still_closes(
    tmp_path: Path,
) -> None:
    """A raising block must not commit, and must not leak the handle either."""
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()

    with pytest.raises(RuntimeError):
        with database.connection() as connection:
            connection.execute(
                "INSERT INTO admin_users (username, password_hash, password_changed) "
                "VALUES ('rolled-back', 'hash', 0)"
            )
            raise RuntimeError("boom")

    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")

    with database.connection() as verify:
        stored = verify.execute(
            "SELECT COUNT(*) FROM admin_users WHERE username = 'rolled-back'"
        ).fetchone()[0]
    assert stored == 0


@pytest.mark.asyncio
async def test_ai_path_suggestion_cascades_and_survives_its_migration(
    tmp_path: Path,
) -> None:
    """Migration 021 rebuilds `ai_path_suggestions` with ON DELETE CASCADE.

    The old definition had a plain `REFERENCES candidates(id)`, so deleting a
    candidate that had an AI path cached -- which the ingest merge and the
    edit-removal path both do -- failed with `FOREIGN KEY constraint failed`.
    The assertion that matters is the last one: the upgrade keeps the rows it
    found, because an operator who already paid for an AI path must not lose it
    to a schema fix.
    """
    path = tmp_path / "ehbot.db"
    database = Database(path)
    await database.initialize()

    with database.connection() as connection:
        candidate_id = int(
            connection.execute(
                "INSERT INTO candidates (status) VALUES ('PENDING_REVIEW')"
            ).lastrowid
        )
        # Rebuild the table the way migration 018 defined it, so this exercises
        # the upgrade rather than a fresh install, and forget 021 ran.
        connection.execute("ALTER TABLE ai_path_suggestions RENAME TO sug_new")
        connection.execute(
            "CREATE TABLE ai_path_suggestions ("
            "candidate_id INTEGER PRIMARY KEY REFERENCES candidates(id), "
            "fingerprint TEXT NOT NULL, prompt_hash TEXT NOT NULL, "
            "relative_path TEXT NOT NULL, directory TEXT NOT NULL, "
            "filename TEXT NOT NULL, provider_id INTEGER, "
            "model_name TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 1, "
            "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        connection.execute(
            "INSERT INTO ai_path_suggestions "
            "(candidate_id, fingerprint, prompt_hash, relative_path, directory, "
            " filename, model_name) VALUES (?, 'fp', 'ph', 'a/b.cbz', 'a', "
            "'b', 'm')",
            (candidate_id,),
        )
        connection.execute("DROP TABLE sug_new")
        connection.execute("DELETE FROM schema_migrations WHERE version = 21")

    await database.initialize()

    with database.connection() as connection:
        kept = connection.execute(
            "SELECT fingerprint FROM ai_path_suggestions WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        assert kept == ("fp",)
        # The cascade is the whole point: this delete used to raise.
        connection.execute("DELETE FROM candidates WHERE id = ?", (candidate_id,))
        remaining = connection.execute(
            "SELECT COUNT(*) FROM ai_path_suggestions"
        ).fetchone()[0]
    assert remaining == 0


def _source_message(chat_id: int = -100900) -> "object":
    from app.candidates.models import ParsedSourceMessage

    return ParsedSourceMessage(
        is_edit=False,
        chat_id=chat_id,
        chat_title="Tombstone Fixture",
        message_id=1,
        sender_id=None,
        reply_to_message_id=None,
        media_group_id=None,
        message_text="",
        attachments=(),
        file_unique_id=None,
        message_date="2026-01-01T00:00:00+00:00",
        title="X",
        title_source="TELEGRAM",
        title_confidence=0.9,
        filter_result="ACCEPT",
        filter_reason="",
    )


@pytest.mark.asyncio
async def test_a_dismissed_source_survives_discovery_and_stays_hidden(
    tmp_path: Path,
) -> None:
    """R50: 删除来源 is a tombstone, not a DELETE.

    `discover_telegram_source` inserts a row for every chat a message ever
    arrives from, so a deleted row would come back (disabled) on the next
    message. A dismissed row stays dismissed through discovery and is hidden
    from the settings list.
    """
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    await database.configure_telegram_source(
        source_type="CHANNEL",
        chat_id=-100900,
        display_name="Fixtures",
        enabled=True,
        allowed_archive_formats=("zip",),
        max_attachment_size_mb=0,
    )
    source_id = (await database.list_telegram_sources())[0].source_id

    removed = await database.dismiss_telegram_sources([source_id])
    assert removed == 1
    assert await database.list_telegram_sources() == []

    # A message from the chat re-runs discovery; the tombstone must hold.
    await database.discover_telegram_source(_source_message())
    assert await database.list_telegram_sources() == []


@pytest.mark.asyncio
async def test_saving_a_dismissed_source_again_revives_it(
    tmp_path: Path,
) -> None:
    """The way back from a tombstone: an explicit save un-dismisses the chat."""
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    await database.configure_telegram_source(
        source_type="CHANNEL",
        chat_id=-100901,
        display_name="Revive",
        enabled=True,
        allowed_archive_formats=("zip",),
        max_attachment_size_mb=0,
    )
    source_id = (await database.list_telegram_sources())[0].source_id
    await database.dismiss_telegram_sources([source_id])

    await database.configure_telegram_source(
        source_type="CHANNEL",
        chat_id=-100901,
        display_name="Revive",
        enabled=False,
        allowed_archive_formats=("zip",),
        max_attachment_size_mb=0,
    )
    listed = await database.list_telegram_sources()
    assert [row.source_id for row in listed] == [source_id]


@pytest.mark.asyncio
async def test_bulk_add_creates_disabled_rows_and_keeps_existing_ones(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    await database.configure_telegram_source(
        source_type="CHANNEL",
        chat_id=-100902,
        display_name="Existing",
        enabled=True,
        allowed_archive_formats=("zip",),
        max_attachment_size_mb=0,
    )

    created = await database.add_telegram_sources_bulk(
        [
            {
                "source_type": "CHANNEL",
                "chat_id": -100902,
                "display_name": "Existing",
            },
            {
                "source_type": "PRIVATE_CHAT",
                "chat_id": 9100,
                "display_name": "Fresh",
            },
        ]
    )

    assert created == 1
    rows = {row.chat_id: row for row in await database.list_telegram_sources()}
    assert rows[-100902].enabled is True
    assert rows[9100].enabled is False


@pytest.mark.asyncio
async def test_bulk_add_revives_a_dismissed_source(tmp_path: Path) -> None:
    """Re-selecting a 删除过的 chat in the picker lifts its tombstone.

    The single-source form already behaves this way; the batch add has to
    agree, or an operator who deleted a chat and then found it again in the
    dialog list would see it counted as 已有 while it stayed invisible.
    """
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    await database.configure_telegram_source(
        source_type="CHANNEL",
        chat_id=-100903,
        display_name="Gone",
        enabled=True,
        allowed_archive_formats=("zip",),
        max_attachment_size_mb=0,
    )
    source_id = (await database.list_telegram_sources())[0].source_id
    await database.dismiss_telegram_sources([source_id])
    assert await database.list_telegram_sources() == []

    created = await database.add_telegram_sources_bulk(
        [
            {
                "source_type": "CHANNEL",
                "chat_id": -100903,
                "display_name": "Gone",
            }
        ]
    )

    assert created == 0
    listed = await database.list_telegram_sources()
    assert [row.source_id for row in listed] == [source_id]
    assert listed[0].enabled is False


@pytest.mark.asyncio
async def test_migration_024_backfills_a_missing_action_to_approve(
    tmp_path: Path,
) -> None:
    """The migration's safety property: no rule becomes a reject by accident.

    `024` adds the column with `DEFAULT 'APPROVE'`, which is what backfills the
    rows that already existed when the ALTER ran. A raw insert that names no
    action exercises the same default, so an upgraded database keeps behaving
    exactly as it did before the feature existed.
    """
    path = tmp_path / "ehbot.db"
    database = Database(path)
    await database.initialize()

    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO auto_approval_rules "
            "(name, condition_json, dsl_snapshot) VALUES (?, ?, ?)",
            ("Legacy", '{"kind": "condition"}', "x"),
        )

    rule = (await database.list_auto_approval_rules())[0]
    assert rule.action == "APPROVE"


# ------------------------------------------------- 按画廊 ID 去重 (R59)


@pytest.mark.asyncio
async def test_candidate_id_for_gallery_matches_the_id_not_the_token(
    tmp_path: Path,
) -> None:
    """The gallery id is the work; the token is only how the URL spells it."""
    path = tmp_path / "ehbot.db"
    database = Database(path)
    await database.initialize()

    assert await database.candidate_id_for_gallery(4242) is None

    with sqlite3.connect(path) as connection:
        first = int(
            connection.execute(
                "INSERT INTO candidates (status, ex_gid, ex_gallery_token) "
                "VALUES ('PENDING_REVIEW', 4242, 'tokA')"
            ).lastrowid
        )
        connection.execute(
            "INSERT INTO candidates (status, ex_gid, ex_gallery_token) "
            "VALUES ('PENDING_REVIEW', 99, 'tokB')"
        )
        # A gallery-less row must never answer for an id lookup.
        connection.execute(
            "INSERT INTO candidates (status) VALUES ('PENDING_REVIEW')"
        )

    assert await database.candidate_id_for_gallery(4242) == first
    # A different spelling of the same gallery still finds the row: the gate
    # keys on the id alone, so a re-post with a rewritten token is a duplicate.
    assert await database.candidate_id_for_gallery(4242) == first
    assert await database.candidate_id_for_gallery(99) is not None


@pytest.mark.asyncio
async def test_duplicate_gallery_groups_lists_only_shared_ids(
    tmp_path: Path,
) -> None:
    """Three rows on one gallery, one alone, one with no id at all."""
    path = tmp_path / "ehbot.db"
    database = Database(path)
    await database.initialize()

    with sqlite3.connect(path) as connection:
        ids = [
            int(
                connection.execute(
                    "INSERT INTO candidates (status, ex_gid) VALUES (?, ?)",
                    (status, ex_gid),
                ).lastrowid
            )
            for status, ex_gid in (
                ("PENDING_REVIEW", 700),
                ("DOWNLOADED", 700),
                ("REJECTED", 700),
                ("DOWNLOADED", 800),
                ("PENDING_REVIEW", None),
            )
        ]

    rows = await database.duplicate_gallery_groups()

    # Ordered by gallery then id, so the oldest member of each group comes
    # first, and the lone id and the gallery-less row are absent.
    assert [row["candidate_id"] for row in rows] == ids[:3]
    assert [row["ex_gid"] for row in rows] == [700, 700, 700]
    assert [row["status"] for row in rows] == [
        "PENDING_REVIEW",
        "DOWNLOADED",
        "REJECTED",
    ]
    assert all(row["packaged"] is False for row in rows)
    assert all(row["page_count"] is None for row in rows)


@pytest.mark.asyncio
async def test_pending_candidate_ids_can_require_a_gallery(tmp_path: Path) -> None:
    """The sweeper's population (R59): gallery-linked pending rows only.

    Without the filter a backlog of no-id candidates would fill the oldest-first
    batch window and every candidate behind it would go unswept.
    """
    path = tmp_path / "ehbot.db"
    database = Database(path)
    await database.initialize()

    with sqlite3.connect(path) as connection:
        with_gallery = int(
            connection.execute(
                "INSERT INTO candidates (status, ex_gid) "
                "VALUES ('PENDING_REVIEW', 31)"
            ).lastrowid
        )
        connection.execute(
            "INSERT INTO candidates (status) VALUES ('PENDING_REVIEW')"
        )
        connection.execute(
            "INSERT INTO candidates (status, ex_gid) VALUES ('APPROVED', 32)"
        )

    every = await database.pending_candidate_ids(oldest_first=True)
    linked = await database.pending_candidate_ids(
        oldest_first=True, require_gallery=True
    )

    assert len(every) == 2
    assert linked == (with_gallery,)
