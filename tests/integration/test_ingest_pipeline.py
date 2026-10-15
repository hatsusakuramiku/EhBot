"""The actual shared ingestion flow, including concurrent AI and source edits."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.candidates.ingestor import CandidateIngestor
from app.candidates.parse_rules import PARSE_RULES_KEY
from app.connections.accounts import AccountRegistry
from app.db.database import Database
from app.secrets import SecretStore
from tests.ingest_admission import permit_all_message_types
from tests.integration.test_candidate_ingestion import allow_sources
from tests.integration.test_ingest_entries import message


async def setup(tmp_path):
    db = Database(tmp_path / "pipeline.db")
    await db.initialize()
    await permit_all_message_types(db)
    await allow_sources(db, -1000000000777, -1000000000888)
    registry = AccountRegistry(db, SecretStore(tmp_path / "private"))
    accounts = [await registry.create("TG_BOT", "A", {"bot_token": "fixture-a"}),
                await registry.create("TG_USER", "B", {"api_id": "1", "api_hash": "fixture-b"})]
    accounts[0] = await registry.record_identity(accounts[0]["account_id"], 1, "1", "Fixture Bot")
    accounts[1] = await registry.save_session(accounts[1]["account_id"], expected_revision=1,
                                            session="fixture-session", remote_identity="2", platform_name="Fixture User")
    for account in accounts:
        await registry.enable(account["account_id"], expected_revision=account["revision"])
    for account in accounts:
        for chat in (-1000000000777, -1000000000888):
            await db.configure_telegram_source(account_id=account["account_id"], source_type="CHANNEL", chat_id=chat,
                display_name="fixture-source", enabled=True, allowed_archive_formats=("zip",), max_attachment_size_mb=0)
    return db, [row["account_id"] for row in accounts]


def accepted():
    return SimpleNamespace(verdict="accept", override=False, reason="fixture accepted")


@pytest.mark.asyncio
async def test_concurrent_receivers_run_one_ai_and_keep_the_owners_original_input(tmp_path):
    db, (a, b) = await setup(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    seen = []
    class Admission:
        async def decide(self, parsed):
            seen.append(parsed)
            entered.set()
            await release.wait()
            return accepted()
    ingestor = CandidateIngestor(db, admission=Admission())
    first_input = replace(message(a), message_text="first persisted caption",
                          attachments=({"type": "archive", "file_name": "a.zip", "file_id": "bot-a-file"},))
    first = asyncio.create_task(ingestor.process_message(first_input))
    await asyncio.wait_for(entered.wait(), 5)
    second = await ingestor.process_message(replace(message(b, chat_id=-1000000000888), message_text="other caption",
        ex_gallery_token="other-link-token", attachments=({"type": "archive", "file_name": "b.zip", "file_id": ""},)))
    assert second[0] is False and len(seen) == 1
    with db.connection() as c:
        assert c.execute("SELECT count(*) FROM candidates").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM source_messages").fetchone()[0] == 2
    release.set()
    assert (await first)[0] is True
    assert seen[0].account_id == a and seen[0].message_text == "first persisted caption"
    with db.connection() as c:
        assert c.execute("SELECT count(*) FROM candidates").fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM candidate_messages").fetchone()[0] == 2
        assert c.execute("SELECT count(*) FROM download_jobs").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_duplicate_existing_gallery_preserves_approval_and_does_not_call_ai(tmp_path):
    db, (a, b) = await setup(tmp_path)
    ingestor = CandidateIngestor(db)
    assert (await ingestor.process_message(message(a)))[0]
    with db.connection() as c:
        c.execute("UPDATE candidates SET status = 'APPROVED' WHERE id = 1")
        c.execute("INSERT INTO metadata_values(candidate_id, field_name, field_value, value_source, is_manual) "
                  "VALUES(1, 'Title', 'operator title', 'MANUAL', 1)")
    class Admission:
        async def decide(self, parsed):
            pytest.fail("Existing identity must supplement without another AI call")
    ingestor.set_admission(Admission())
    assert (await ingestor.process_message(message(b, chat_id=-1000000000888)))[0] is False
    with db.connection() as c:
        assert c.execute("SELECT status FROM candidates WHERE id = 1").fetchone() == ("APPROVED",)
        assert c.execute("SELECT field_value FROM metadata_values WHERE candidate_id = 1").fetchone() == ("operator title",)
        assert c.execute("SELECT count(*) FROM candidate_approvals").fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM candidate_messages").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_explicit_gallery_conflicts_override_media_group_and_reply_links(tmp_path):
    db, (a, _) = await setup(tmp_path)
    ingestor = CandidateIngestor(db)
    await ingestor.process_message(replace(message(a), media_group_id="album"))
    await ingestor.process_message(replace(message(a, gid=456, message_id=43), media_group_id="album", reply_to_message_id=42))
    with db.connection() as c:
        assert c.execute("SELECT ex_gid FROM candidates ORDER BY ex_gid").fetchall() == [(123,), (456,)]
        assert c.execute("SELECT count(*) FROM candidate_messages").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_gallery_edit_moves_one_source_and_preserves_original_review_and_artifact(tmp_path):
    db, (a, b) = await setup(tmp_path)
    ingestor = CandidateIngestor(db)
    await ingestor.process_message(message(a))
    await ingestor.process_message(message(b, chat_id=-1000000000888))
    with db.connection() as c:
        c.execute("UPDATE candidates SET status = 'APPROVED' WHERE id = 1")
        c.execute("INSERT INTO review_actions(candidate_id, action, operator_name) VALUES(1, 'APPROVE', 'fixture')")
        jid = c.execute("INSERT INTO download_jobs(candidate_id, idempotency_key, provider, state) "
                        "VALUES(1, 'fixture-completed', 'TELEGRAM', 'COMPLETED')").lastrowid
        c.execute("INSERT INTO artifacts(job_id, artifact_type, path) VALUES(?, 'ARCHIVE', 'fixture/archive.zip')", (jid,))
    await ingestor.process_message(replace(message(a, gid=456), is_edit=True, message_text="changed gallery"))
    with db.connection() as c:
        assert c.execute("SELECT status, ex_gid FROM candidates WHERE id = 1").fetchone() == ("APPROVED", 123)
        assert c.execute("SELECT count(*) FROM candidate_messages WHERE candidate_id = 1").fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM review_actions WHERE candidate_id = 1").fetchone()[0] == 1
        assert c.execute("SELECT candidate_id FROM download_jobs WHERE id = ?", (jid,)).fetchone() == (1,)
        assert c.execute("SELECT path FROM artifacts WHERE job_id = ?", (jid,)).fetchone() == ("fixture/archive.zip",)
        assert c.execute("SELECT ex_gid FROM candidates WHERE id <> 1").fetchone() == (456,)


@pytest.mark.asyncio
async def test_gallery_completion_merges_stopped_reviewed_history_instead_of_refusing_it(tmp_path):
    db, (a, _) = await setup(tmp_path)
    ingestor = CandidateIngestor(db)
    await ingestor.process_message(message(a, gid=None))
    await ingestor.process_message(message(a, gid=123, message_id=90))
    with db.connection() as c:
        source = c.execute('SELECT id FROM candidates WHERE ex_gid IS NULL').fetchone()[0]
        target = c.execute('SELECT id FROM candidates WHERE ex_gid = 123').fetchone()[0]
        c.execute("UPDATE candidates SET status = 'APPROVED' WHERE id = ?", (source,))
        job = c.execute("INSERT INTO download_jobs(candidate_id, idempotency_key, provider, state) "
                        "VALUES(?, 'stopped-history', 'TELEGRAM', 'COMPLETED')", (source,)).lastrowid
        c.execute("INSERT INTO artifacts(job_id, artifact_type, path) VALUES(?, 'ARCHIVE', 'kept.zip')", (job,))
    await ingestor.process_message(replace(message(a, gid=123), is_edit=True))
    with db.connection() as c:
        assert c.execute('SELECT id, ex_gid, status FROM candidates').fetchall() == [(target, 123, 'APPROVED')]
        assert c.execute('SELECT candidate_id FROM download_jobs WHERE id = ?', (job,)).fetchone() == (target,)
        assert c.execute('SELECT path FROM artifacts WHERE job_id = ?', (job,)).fetchone() == ('kept.zip',)
        assert c.execute('SELECT count(*) FROM candidate_messages WHERE candidate_id = ?', (target,)).fetchone() == (2,)


@pytest.mark.asyncio
async def test_failed_ai_leaves_recoverable_input_and_retry_uses_fixed_original(tmp_path):
    db, _ = await setup(tmp_path)
    seen = []
    class Admission:
        async def decide(self, parsed):
            seen.append((parsed.message_text, parsed.chat_title))
            if len(seen) == 1:
                raise RuntimeError("fixture network failure")
            return accepted()
    ingestor = CandidateIngestor(db, admission=Admission())
    with pytest.raises(ExceptionGroup):
        await ingestor.process_message(replace(message(), message_text="original", chat_title="Original channel"))
    with db.connection() as c:
        assert c.execute("SELECT processing_state FROM ingest_entries").fetchone() == ("PENDING",)
        assert c.execute("SELECT count(*) FROM candidates").fetchone()[0] == 0
        c.execute("UPDATE ingest_entries SET retry_at = 0")
        c.execute("UPDATE telegram_sources SET display_name = 'Renamed rule label'")
    assert await CandidateIngestor(db, admission=ingestor._admission).process_pending_entries() == 1
    assert seen == [("original", "Original channel"), ("original", "Original channel")]


@pytest.mark.asyncio
async def test_cancellation_waits_for_actual_ai_cleanup_before_releasing_processing_right(tmp_path):
    db, _ = await setup(tmp_path)
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    class Admission:
        async def decide(self, parsed):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
    ingestor = CandidateIngestor(db, admission=Admission())
    task = asyncio.create_task(ingestor.process_message(message()))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    await asyncio.wait_for(cleaning.wait(), 5)
    assert not task.done()
    with db.connection() as c:
        assert c.execute("SELECT processing_state FROM ingest_entries").fetchone() == ("PROCESSING",)
    assert (await ingestor.process_message(message(chat_id=-1000000000888)))[0] is False
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    with db.connection() as c:
        assert c.execute("SELECT processing_state FROM ingest_entries").fetchone() == ("PROCESSING",)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    with db.connection() as c:
        assert c.execute("SELECT processing_state, owner FROM ingest_entries").fetchone() == ("PENDING", None)


@pytest.mark.asyncio
async def test_edit_stops_old_ai_before_processing_the_new_revision(tmp_path):
    db, _ = await setup(tmp_path)
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []
    class Admission:
        async def decide(self, parsed):
            calls.append(parsed.message_text)
            if len(calls) == 1:
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaning.set()
                    await release.wait()
            return accepted()
    ingestor = CandidateIngestor(db, admission=Admission())
    first = asyncio.create_task(ingestor.process_message(replace(message(), message_text="before")))
    await asyncio.wait_for(entered.wait(), 5)
    edited = asyncio.create_task(ingestor.process_message(replace(message(), is_edit=True, message_text="after")))
    await asyncio.wait_for(cleaning.wait(), 5)
    assert not edited.done() and calls == ["before"]
    release.set()
    assert (await first)[0] is False
    # Interrupted old input had backoff; its replacement is a new revision.
    with db.connection() as c:
        c.execute("UPDATE ingest_entries SET retry_at = 0")
    await edited
    await ingestor.process_pending_entries()
    assert calls == ["before", "after"]
    with db.connection() as c:
        assert c.execute("SELECT count(*) FROM candidates").fetchone()[0] == 1
        assert c.execute("SELECT message_text FROM source_messages").fetchone() == ("after",)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["message_text", "chat_title"])
async def test_edited_rejected_input_is_rejudged_once_and_replayed_edit_does_not_repeat_ai(tmp_path, field):
    db, _ = await setup(tmp_path)
    calls = []
    class Admission:
        async def decide(self, parsed):
            calls.append(getattr(parsed, field))
            return SimpleNamespace(verdict="reject", override=False, reason="fixture rejected") if len(calls) == 1 else accepted()
    ingestor = CandidateIngestor(db, admission=Admission())
    await ingestor.process_message(replace(message(), **{field: "rejected"}))
    edited = replace(message(), is_edit=True, **{field: "fixed"})
    assert (await ingestor.process_message(edited))[0]
    assert (await ingestor.process_message(edited))[0] is False
    assert calls == ["rejected", "fixed"]


@pytest.mark.asyncio
async def test_forward_without_link_uses_originals_recorded_gallery_not_another_channel_key(tmp_path):
    db, (a, b) = await setup(tmp_path)
    ingestor = CandidateIngestor(db)
    await ingestor.process_message(message(a))
    forwarded = replace(message(b, gid=None, chat_id=-1000000000888, message_id=80),
                        origin_channel_id=777, origin_message_id=42)
    await db.save_system_settings({PARSE_RULES_KEY: '{"require_gallery_link":true}'})
    assert (await ingestor.parse_rules())["require_gallery_link"] is True
    assert (await ingestor.process_message(forwarded))[0] is False
    with db.connection() as c:
        assert c.execute("SELECT unique_key FROM ingest_entries").fetchall() == [("eh:gallery:123",)]
        assert c.execute("SELECT count(*) FROM candidate_messages").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_observing_external_account_retains_rule_reference_for_later_metadata_filtering(tmp_path):
    db, (a, _) = await setup(tmp_path)
    await db.configure_telegram_source(account_id=a, source_type="CHANNEL", chat_id=-1000000000777, display_name="fixture-source",
        enabled=True, allowed_archive_formats=("zip",), max_attachment_size_mb=0, required_tags=("language:chinese",))
    await CandidateIngestor(db).process_message(message(a))
    with db.connection() as c:
        assert c.execute("SELECT ta.external_account_id, ts.display_name FROM source_messages sm "
                         "JOIN telegram_accounts ta ON ta.id = sm.account_id "
                         "JOIN telegram_sources ts ON ts.id = sm.rule_source_id").fetchone() == (a, "fixture-source")
    await db.set_candidate_metadata_value(1, "Title", "gallery title", "EXHENTAI", 0.6)
    await db.re_evaluate_candidate_metadata_rules(1)
    with db.connection() as c:
        assert c.execute("SELECT status FROM candidates WHERE id = 1").fetchone() == ("REJECTED",)


@pytest.mark.asyncio
async def test_bot_events_and_processing_results_belong_to_receiving_account_across_restart(tmp_path):
    from app.connections.runtime import current_account_context
    db, (a, b) = await setup(tmp_path)
    registry = AccountRegistry(db, SecretStore(tmp_path / "private"))
    bot_b = await registry.create("TG_BOT", "other bot", {"bot_token": "fixture-other"})
    ids = (a, bot_b["account_id"])
    for index, account in enumerate(ids):
        token = current_account_context.set(await registry.context(account))
        try:
            await db.save_telegram_updates([{"update_id": 50, "message": {"fixture": index}}])
            assert await db.latest_telegram_update_id() == 50
            assert (await db.pending_telegram_updates(limit=10))[0][1]["message"] == {"fixture": index}
            if index == 0:
                await db.mark_telegram_update_result(50, "IGNORE", "fixture acknowledged")
        finally:
            current_account_context.reset(token)
    await db.initialize()
    assert await db.latest_telegram_update_id() is None
    assert await db.pending_telegram_updates(limit=10) == []
    token = current_account_context.set(await registry.context(ids[1]))
    try:
        assert len(await db.pending_telegram_updates(limit=10)) == 1
        await db.mark_telegram_update_result(50, "ACCEPT", "second processed")
    finally:
        current_account_context.reset(token)
    with db.connection() as c:
        assert c.execute("SELECT external_account_id, processing_result FROM telegram_bot_updates ORDER BY processing_result").fetchall() == [
            (ids[1], "ACCEPT"), (ids[0], "IGNORE")]


@pytest.mark.asyncio
@pytest.mark.parametrize("source_change", ["disabled", "format", "unchanged"])
async def test_pending_input_keeps_eligible_owner_or_yields_to_a_qualified_observation(tmp_path, source_change):
    db, (a, b) = await setup(tmp_path)
    seen = []

    class Admission:
        async def decide(self, parsed):
            seen.append(parsed.account_id)
            if len(seen) == 1:
                raise RuntimeError("fixture first request failed")
            return accepted()

    ingestor = CandidateIngestor(db, admission=Admission())
    with pytest.raises(ExceptionGroup) as failed:
        await ingestor.process_message(message(a))
    assert str(failed.value.exceptions[0]) == "fixture first request failed"
    if source_change != "unchanged":
        await db.configure_telegram_source(
            account_id=a, source_type="CHANNEL", chat_id=-1000000000777, display_name="changed original",
            enabled=source_change != "disabled",
            allowed_archive_formats=("rar",) if source_change == "format" else ("zip",), max_attachment_size_mb=0,
        )
    with db.connection() as connection:
        connection.execute("UPDATE ingest_entries SET retry_at = NULL")
    created, _ = await ingestor.process_message(message(b, chat_id=-1000000000888))
    assert created
    assert seen == [a, a if source_change == "unchanged" else b]
    with db.connection() as connection:
        assert connection.execute("SELECT count(*) FROM candidates").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM candidate_messages").fetchone() == (2,)


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_request", [False, True])
async def test_restart_recovers_previously_saved_qualified_observer_without_a_new_event(tmp_path, failed_request):
    from app.connections.runtime import current_account_context
    db, (a, b) = await setup(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    seen = []

    class Admission:
        async def decide(self, parsed):
            seen.append(parsed.account_id)
            if len(seen) == 1:
                entered.set()
                await release.wait()
                if failed_request:
                    raise RuntimeError("fixture original failed")
            return accepted()

    admission = Admission()
    ingestor = CandidateIngestor(db, admission=admission)
    first = asyncio.create_task(ingestor.process_message(message(a)))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert not (await ingestor.process_message(message(b, chat_id=-1000000000888)))[0]
        if not failed_request:
            await db.configure_telegram_source(
                account_id=a, source_type="CHANNEL", chat_id=-1000000000777, display_name="disabled during AI",
                enabled=False, allowed_archive_formats=("zip",), max_attachment_size_mb=0,
            )
    finally:
        release.set()
    if failed_request:
        with pytest.raises(ExceptionGroup):
            await first
    else:
        assert not (await first)[0]
    await db.configure_telegram_source(
        account_id=a, source_type="CHANNEL", chat_id=-1000000000777, display_name="disabled original",
        enabled=False, allowed_archive_formats=("zip",), max_attachment_size_mb=0,
    )
    with db.connection() as connection:
        connection.execute("UPDATE ingest_entries SET retry_at = NULL")
    registry = AccountRegistry(db, SecretStore(tmp_path / "private"))
    token = current_account_context.set(await registry.context(b))
    try:
        fresh = CandidateIngestor(db, admission=admission)
        assert await fresh.process_pending_entries() == 1
        assert await fresh.process_pending_entries() == 0
    finally:
        current_account_context.reset(token)
    assert seen == [a, b]
    with db.connection() as connection:
        assert connection.execute("SELECT count(*) FROM candidates").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM candidate_messages").fetchone() == (2,)
