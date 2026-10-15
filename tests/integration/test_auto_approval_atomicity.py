"""Real automatic decisions survive failed commits, stale reads and lost acknowledgements."""
import asyncio

import pytest

from app.auto_approval.service import AutomaticApprovalService
from app.candidates.followups import due_followups
from app.db.database import Database
from app.downloads.archived import ArchivedWorkService
from app.downloads.service import DownloadService
from app.review.orchestration import ReviewOrchestrator
from app.review.service import ReviewService
from tests.integration.test_ingest_followups import seed, sweeper, write_metadata


def orchestrator(database, tmp_path):
    download = DownloadService(database, tmp_path / 'work')
    return ReviewOrchestrator(database, lambda: download,
        torrent_available=lambda: False, telegraph_available=lambda: False)


def assert_no_decision(database, candidate_id, *, status='PENDING_REVIEW'):
    with database.connection() as c:
        assert c.execute('SELECT status FROM candidates WHERE id = ?', (candidate_id,)).fetchone() == (status,)
        for table in ('candidate_approvals', 'download_jobs', 'queue_tasks', 'queue_attempts'):
            assert c.execute(f'SELECT count(*) FROM {table}').fetchone() == (0,)
        assert c.execute("SELECT count(*) FROM review_actions WHERE action IN ('AUTO_APPROVE', 'AUTO_REJECT')").fetchone() == (0,)


@pytest.mark.asyncio
@pytest.mark.parametrize('action,table,condition', [
    ('APPROVE', 'download_jobs', '1'),
    ('APPROVE', 'review_actions', "NEW.action = 'APPROVE'"),
    ('APPROVE', 'review_actions', "NEW.action = 'AUTO_APPROVE'"),
    ('REJECT', 'review_actions', "NEW.action = 'REJECT'"),
    ('REJECT', 'review_actions', "NEW.action = 'AUTO_REJECT'"),
])
async def test_failed_commit_rolls_back_review_task_and_evidence_then_recovers(tmp_path, action, table, condition):
    database, cid, _ = await seed(tmp_path, action=action)
    with database.connection() as c:
        c.execute(f"CREATE TRIGGER fail_decision BEFORE INSERT ON {table} WHEN {condition} "
                  "BEGIN SELECT RAISE(ABORT, 'DECISION_INJECTED'); END")
    assert await sweeper(database, tmp_path).process_ingest_followups() == 0
    assert_no_decision(database, cid)
    with database.connection() as c:
        assert c.execute('SELECT count(*) FROM review_actions').fetchone() == (0,)
        assert c.execute('SELECT followup_attempts, followup_revision > followup_done_revision FROM ingest_entries').fetchone() == (1, 1)
        c.execute('DROP TRIGGER fail_decision')
        c.execute('UPDATE ingest_entries SET followup_retry_at = 0')
    fresh = Database(database.path)
    await fresh.initialize()
    assert await sweeper(fresh, tmp_path).process_ingest_followups() == 1
    assert due_followups(fresh, 100) == []
    with fresh.connection() as c:
        assert c.execute('SELECT status FROM candidates WHERE id = ?', (cid,)).fetchone() == (
            'APPROVED' if action == 'APPROVE' else 'REJECTED',)
        assert c.execute('SELECT count(*) FROM download_jobs').fetchone() == (int(action == 'APPROVE'),)
        assert c.execute('SELECT count(*) FROM candidate_approvals').fetchone() == (int(action == 'APPROVE'),)
        assert c.execute('SELECT count(*) FROM review_actions').fetchone() == (2,)


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['APPROVE', 'REJECT'])
async def test_restart_after_decision_before_ack_does_not_repeat_it(tmp_path, monkeypatch, action):
    import app.candidates.followups as followups
    database, cid, _ = await seed(tmp_path, action=action)
    original = followups.finish_followup
    def interrupt(*args):
        raise asyncio.CancelledError
    monkeypatch.setattr(followups, 'finish_followup', interrupt)
    with pytest.raises(asyncio.CancelledError):
        await sweeper(database, tmp_path).process_ingest_followups()
    assert len(due_followups(database, 100)) == 1
    monkeypatch.setattr(followups, 'finish_followup', original)
    fresh = Database(database.path)
    await fresh.initialize()
    assert await sweeper(fresh, tmp_path).process_ingest_followups() == 0
    assert due_followups(fresh, 100) == []
    with fresh.connection() as c:
        assert c.execute('SELECT count(*) FROM download_jobs').fetchone() == (int(action == 'APPROVE'),)
        assert c.execute('SELECT count(*) FROM review_actions WHERE candidate_id = ?', (cid,)).fetchone() == (2,)


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['APPROVE', 'REJECT'])
@pytest.mark.parametrize('change', ['metadata', 'gallery', 'rule_version', 'rule_priority', 'rule_disabled', 'new_first_rule', 'manual_reject'])
async def test_final_transaction_declines_stale_decision(tmp_path, monkeypatch, action, change):
    database, cid, _ = await seed(tmp_path, action=action)
    read, release = asyncio.Event(), asyncio.Event()
    original = AutomaticApprovalService.matching_rule
    async def hold_match(self, candidate_id):
        match = await original(self, candidate_id)
        assert match is not None
        read.set()
        await release.wait()
        return match
    monkeypatch.setattr(AutomaticApprovalService, 'matching_rule', hold_match)
    pending = asyncio.create_task(orchestrator(database, tmp_path).apply_automatic_decision(cid))
    try:
        await asyncio.wait_for(read.wait(), 3)
        if change == 'manual_reject':
            await ReviewService(database).reject_candidate(cid, 'admin', 'manual decision')
        elif change == 'gallery':
            await database.set_candidate_eh_ref(cid, 456, 'new-fixture-token')
        else:
            with database.connection() as c:
                if change == 'metadata':
                    c.execute("UPDATE metadata_values SET field_value = 'changed' WHERE candidate_id = ?", (cid,))
                elif change == 'rule_version':
                    c.execute('UPDATE auto_approval_rules SET version = version + 1')
                elif change == 'rule_priority':
                    c.execute('UPDATE auto_approval_rules SET priority = priority + 1')
                elif change == 'rule_disabled':
                    c.execute('UPDATE auto_approval_rules SET enabled = 0')
                elif change == 'new_first_rule':
                    c.execute("INSERT INTO auto_approval_rules(name, enabled, priority, version, condition_json, dsl_snapshot, action) "
                              "SELECT 'higher priority', 1, 0, 1, condition_json, dsl_snapshot, action FROM auto_approval_rules")
        release.set()
        assert await asyncio.wait_for(pending, 3) is None
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
    assert_no_decision(database, cid, status='REJECTED' if change == 'manual_reject' else 'PENDING_REVIEW')


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['APPROVE', 'REJECT'])
async def test_two_automatic_callers_commit_one_decision(tmp_path, monkeypatch, action):
    database, cid, _ = await seed(tmp_path, action=action)
    ready, release = asyncio.Event(), asyncio.Event()
    original = AutomaticApprovalService.matching_rule
    count = 0
    async def hold_match(self, candidate_id):
        nonlocal count
        match = await original(self, candidate_id)
        count += 1
        if count == 2:
            ready.set()
        await release.wait()
        return match
    monkeypatch.setattr(AutomaticApprovalService, 'matching_rule', hold_match)
    service = orchestrator(database, tmp_path)
    pending = [asyncio.create_task(service.apply_automatic_decision(cid)) for _ in range(2)]
    try:
        await asyncio.wait_for(ready.wait(), 3)
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*pending), 3)
    finally:
        release.set()
        await asyncio.gather(*pending, return_exceptions=True)
    assert results.count(action) == 1 and results.count(None) == 1
    with database.connection() as c:
        assert c.execute('SELECT count(*) FROM download_jobs').fetchone() == (int(action == 'APPROVE'),)
        assert c.execute('SELECT count(*) FROM review_actions').fetchone() == (2,)


@pytest.mark.asyncio
async def test_old_match_cannot_decide_a_readded_generation_with_the_same_integer_id(tmp_path, monkeypatch):
    database, cid, _ = await seed(tmp_path)
    read, release = asyncio.Event(), asyncio.Event()
    original = AutomaticApprovalService.matching_rule
    async def hold_match(self, candidate_id):
        match = await original(self, candidate_id)
        assert match is not None
        read.set()
        await release.wait()
        return match
    monkeypatch.setattr(AutomaticApprovalService, 'matching_rule', hold_match)
    pending = asyncio.create_task(orchestrator(database, tmp_path).apply_automatic_decision(cid))
    try:
        await asyncio.wait_for(read.wait(), 3)
        await ArchivedWorkService(database, tmp_path / 'library', tmp_path / 'work').purge_work(cid)
        new_cid = await database.create_manual_candidate(filter_reason='explicit re-add', ex_gid=123,
            ex_gallery_token='fixture-token')
        assert new_cid == cid
        with database.connection() as c:
            write_metadata(c, new_cid)
            assert c.execute("SELECT generation FROM ingest_entries WHERE unique_key = 'eh:gallery:123'").fetchone() == (2,)
            tables = ('candidate_approvals', 'download_jobs', 'queue_tasks', 'queue_attempts', 'review_actions')
            before = [c.execute(f'SELECT count(*) FROM {table}').fetchone() for table in tables]
        release.set()
        assert await asyncio.wait_for(pending, 3) is None
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
    with database.connection() as c:
        assert c.execute('SELECT status FROM candidates WHERE id = ?', (cid,)).fetchone() == ('PENDING_REVIEW',)
        assert [c.execute(f'SELECT count(*) FROM {table}').fetchone() for table in tables] == before
