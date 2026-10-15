"""Publication and follow-up survive notification loss, retry and restart."""
import asyncio
from dataclasses import replace

import pytest

from app.auto_approval.sweeper import AutoApprovalSweeper
from app.candidates.followups import due_followups, finish_followup
from app.candidates.ingestor import CandidateIngestor
from app.db.database import Database
from app.downloads.service import DownloadService
from app.review.orchestration import ReviewOrchestrator
from tests.integration.test_ingest_entries import message
from tests.integration.test_ingest_pipeline import setup


class DisabledScans:
    async def auto_approval_interval_minutes(self):
        return 0


async def seed(tmp_path, *, metadata=True, action='REJECT'):
    database, accounts = await setup(tmp_path)
    await CandidateIngestor(database).process_message(message(accounts[0]))
    with database.connection() as c:
        cid = c.execute('SELECT id FROM candidates').fetchone()[0]
        if metadata:
            write_metadata(c, cid)
    await database.save_auto_approval_rule(rule_id=None, name='followup rule', enabled=True, priority=1,
        condition={'kind': 'condition', 'field': 'Title', 'operator': '=', 'value': 'ready'},
        dsl_snapshot='{Title} = "ready"', action=action)
    return database, cid, accounts


def write_metadata(connection, candidate_id):
    connection.execute("INSERT INTO metadata_values(candidate_id, field_name, field_value, value_source) "
                       "VALUES(?, 'Title', 'ready', 'EXHENTAI')", (candidate_id,))
    connection.execute("UPDATE candidates SET status = 'PENDING_REVIEW' WHERE id = ?", (candidate_id,))


def sweeper(database, tmp_path, **kwargs):
    download = DownloadService(database, tmp_path / 'work')
    orchestrator = ReviewOrchestrator(database, lambda: download,
        torrent_available=lambda: False, telegraph_available=lambda: False)
    return AutoApprovalSweeper(database, orchestrator, DisabledScans(), **kwargs)


@pytest.mark.asyncio
async def test_restart_recovers_lost_callback_even_with_periodic_scans_disabled(tmp_path):
    database, cid, _ = await seed(tmp_path)
    # No ingestion callback is called. A fresh DB and the real existing worker
    # must recover solely from the committed publication intention.
    fresh = Database(database.path)
    await fresh.initialize()
    worker = sweeper(fresh, tmp_path)
    finished = asyncio.Event()
    worker._notify = lambda **_: finished.set()
    await worker.start()
    try:
        await asyncio.wait_for(finished.wait(), 3)
    finally:
        await worker.stop()
    assert (await fresh.get_candidate(cid)).status == 'REJECTED'
    assert due_followups(fresh, 100) == []
    with fresh.connection() as c:
        assert c.execute("SELECT count(*) FROM review_actions WHERE action = 'AUTO_REJECT'").fetchone() == (1,)
    assert await sweeper(fresh, tmp_path).process_ingest_followups() == 0


@pytest.mark.asyncio
async def test_duplicate_observation_does_not_schedule_a_second_decision(tmp_path):
    database, cid, accounts = await seed(tmp_path)
    worker = sweeper(database, tmp_path)
    assert await worker.process_ingest_followups() == 1
    with database.connection() as c:
        initial = c.execute('SELECT followup_revision, followup_done_revision FROM ingest_entries').fetchone()
    await CandidateIngestor(database).process_message(message(accounts[1]))
    assert await worker.process_ingest_followups() == 0
    with database.connection() as c:
        assert c.execute('SELECT followup_revision, followup_done_revision FROM ingest_entries').fetchone() == initial
        assert c.execute('SELECT count(*) FROM candidate_messages WHERE candidate_id = ?', (cid,)).fetchone() == (2,)


@pytest.mark.asyncio
async def test_missing_metadata_retains_intent_with_persisted_backoff(tmp_path):
    database, cid, _ = await seed(tmp_path, metadata=False)
    worker = sweeper(database, tmp_path)
    assert await worker.process_ingest_followups() == 0
    with database.connection() as c:
        assert c.execute('SELECT followup_revision > followup_done_revision, followup_attempts, followup_retry_at > unixepoch() FROM ingest_entries').fetchone() == (1, 1, 1)
        write_metadata(c, cid)
        c.execute('UPDATE ingest_entries SET followup_retry_at = unixepoch() - 1')
    fresh = Database(database.path)
    await fresh.initialize()
    assert await sweeper(fresh, tmp_path).process_ingest_followups() == 1
    assert due_followups(fresh, 100) == []


@pytest.mark.asyncio
async def test_old_followup_ack_cannot_consume_a_newer_publication(tmp_path):
    database, _, _ = await seed(tmp_path)
    identity, = due_followups(database, 100)
    with database.connection() as c:
        c.execute('UPDATE ingest_entries SET followup_revision = followup_revision + 1')
    finish_followup(database, identity, True)
    assert len(due_followups(database, 100)) == 1
    finish_followup(database, due_followups(database, 100)[0], True)
    assert due_followups(database, 100) == []


@pytest.mark.asyncio
async def test_followup_publication_rolls_back_with_candidate_and_claim(tmp_path):
    database, accounts = await setup(tmp_path)
    with database.connection() as c:
        c.execute("CREATE TRIGGER reject_followup BEFORE UPDATE OF followup_revision ON ingest_entries "
                  "WHEN NEW.followup_revision > OLD.followup_revision BEGIN SELECT RAISE(ABORT, 'FOLLOWUP_INJECTED'); END")
    with pytest.raises(Exception, match='FOLLOWUP_INJECTED'):
        await CandidateIngestor(database).process_message(message(accounts[0]))
    with database.connection() as c:
        assert c.execute('SELECT count(*) FROM candidates').fetchone() == (0,)
        assert c.execute("SELECT count(*) FROM ingest_entries WHERE processing_state = 'LINKED'").fetchone() == (0,)


@pytest.mark.asyncio
async def test_unmatched_first_batch_does_not_hide_later_ingestion_intents(tmp_path):
    database, cid, accounts = await seed(tmp_path)
    with database.connection() as c:
        c.execute("UPDATE metadata_values SET field_value = 'no match' WHERE candidate_id = ?", (cid,))
    for index in range(1, 102):
        await database.save_candidate_message(None, replace(message(accounts[0], gid=123 + index), message_id=42 + index))
    with database.connection() as c:
        for other, in c.execute('SELECT id FROM candidates WHERE id <> ?', (cid,)).fetchall():
            write_metadata(c, other)
            if other != 102:
                c.execute("UPDATE metadata_values SET field_value = 'no match' WHERE candidate_id = ?", (other,))
    worker = sweeper(database, tmp_path)
    assert await worker.process_ingest_followups() == 0
    assert await worker.process_ingest_followups() == 1
    assert due_followups(database, 100) == []


@pytest.mark.asyncio
async def test_ingestion_hook_and_worker_do_not_run_the_same_decision_concurrently(tmp_path):
    database, _, _ = await seed(tmp_path)
    worker = sweeper(database, tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    original = worker._orchestrator.apply_automatic_decision
    calls = 0
    async def blocked(cid):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return await original(cid)
    worker._orchestrator.apply_automatic_decision = blocked
    first = asyncio.create_task(worker.process_ingest_followups())
    await asyncio.wait_for(entered.wait(), 3)
    second = asyncio.create_task(worker.process_ingest_followups())
    release.set()
    assert await asyncio.gather(first, second) == [1, 0]
    assert calls == 1


@pytest.mark.asyncio
async def test_legacy_alias_followups_recover_one_decision_and_ack_every_alias(tmp_path):
    from tests.integration.test_ingest_migration import legacy_database, candidate, source
    database = legacy_database(tmp_path)
    with database.connection() as c:
        candidate(c, 1, 'legacy-token', gid=None)
        source(c, 1, 1)
        # The later legacy observation proves a gallery; the earlier message
        # key remains an alias of that same work during backfill.
        source(c, 2, 1, text='https://exhentai.org/g/123/fixture-token/')
        write_metadata(c, 1)
    await database.save_auto_approval_rule(rule_id=None, name='migrated', enabled=True, priority=1,
        condition={'kind': 'condition', 'field': 'Title', 'operator': '=', 'value': 'ready'},
        dsl_snapshot='{Title} = "ready"', action='REJECT')
    await database.initialize()
    assert len(due_followups(database, 100)) == 2
    assert await sweeper(database, tmp_path).process_ingest_followups() == 1
    assert due_followups(database, 100) == []
    fresh = Database(database.path)
    await fresh.initialize()
    assert await sweeper(fresh, tmp_path).process_ingest_followups() == 0
    with fresh.connection() as c:
        assert c.execute("SELECT count(*) FROM review_actions WHERE action = 'AUTO_REJECT'").fetchone() == (1,)
        assert c.execute('SELECT count(*) FROM ingest_entries WHERE followup_revision = followup_done_revision').fetchone() == (2,)
