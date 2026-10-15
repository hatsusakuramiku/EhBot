"""An edited source stops its actual selected transfer before moving identity."""
import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from app.candidates.ingestor import CandidateIngestor
from app.candidates.changes import process_observation_changes
from app.downloads.service import DownloadService
from app.downloads.queue import QueueControl, task_snapshot
from app.db.database import Database
from app.connections.runtime import current_account_context
from tests.integration.test_ingest_entries import message
from tests.integration.test_ingest_pipeline import setup


@pytest.mark.asyncio
@pytest.mark.parametrize('edit_kind', ['same-gallery', 'new-gallery', 'complete-gallery', 'deactivate'])
async def test_edit_defers_until_selected_attempt_finishes_cleanup(tmp_path, monkeypatch, edit_kind):
    db, (account, _) = await setup(tmp_path)
    ingestor = CandidateIngestor(db)
    gid = None if edit_kind == 'complete-gallery' else 123
    await ingestor.process_message(message(account, gid=gid))
    if edit_kind == 'complete-gallery':
        await ingestor.process_message(message(account, gid=456, message_id=90))
    with db.connection() as c:
        cid, = c.execute('SELECT candidate_id FROM candidate_messages ORDER BY source_message_id LIMIT 1').fetchone()
        sid, revision = c.execute('SELECT id, observation_revision FROM source_messages ORDER BY id LIMIT 1').fetchone()
        c.execute("UPDATE candidates SET status = 'APPROVED' WHERE id = ?", (cid,))
    service = DownloadService(db, tmp_path / 'work')
    candidate = await db.get_candidate(cid)
    result = await service.enqueue_telegram_download(cid, candidate.messages[0].attachments[0])
    job_id = result.job_id
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def produce(job):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
    monkeypatch.setattr(service, '_handle_job', produce)
    running = asyncio.create_task(service._process_one())
    stopping = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if edit_kind == 'deactivate':
            await db.deactivate_candidate_message(message(account).chat_id, 42, account_id=account)
        else:
            edited = replace(message(account, gid=gid if edit_kind == 'same-gallery' else 456),
                             is_edit=True, message_text='edited input')
            assert not (await ingestor.process_message(edited))[0]
        with db.connection() as c:
            assert c.execute('SELECT state FROM download_jobs WHERE id = ?', (job_id,)).fetchone() == ('STOPPING',)
            assert c.execute('SELECT status FROM ingest_observation_changes').fetchone() == ('PENDING',)
            assert c.execute('SELECT observation_revision, message_text FROM source_messages WHERE id = ?', (sid,)).fetchone() == (revision + 1, 'fixture caption')
        stopping = asyncio.create_task(service.process_operations())
        await asyncio.wait_for(cleaning.wait(), 5)
        assert not stopping.done()
        assert process_observation_changes(db) == []
    finally:
        release.set()
        if stopping is None:
            running.cancel()
        await asyncio.gather(running, *([stopping] if stopping else []), return_exceptions=True)
    # A moved new gallery input is processed by the listener's same recovery path.
    context_token = current_account_context.set(SimpleNamespace(account_id=account, kind='TG_BOT'))
    try:
        await ingestor.process_pending_entries()
    finally:
        current_account_context.reset(context_token)
    with db.connection() as c:
        assert c.execute('SELECT status, error_code FROM ingest_observation_changes').fetchone() == ('COMPLETED', None)
        assert c.execute('SELECT state FROM download_jobs WHERE id = ?', (job_id,)).fetchone() == ('CANCELLED',)
        if edit_kind == 'deactivate':
            assert c.execute('SELECT message_state FROM source_messages WHERE id = ?', (sid,)).fetchone() == ('DELETED',)
        else:
            assert c.execute('SELECT message_text FROM source_messages WHERE id = ?', (sid,)).fetchone() == ('edited input',)
        if edit_kind in {'new-gallery', 'complete-gallery'}:
            new_cid, = c.execute('SELECT id FROM candidates WHERE ex_gid = 456').fetchone()
            assert c.execute('SELECT candidate_id FROM candidate_messages WHERE source_message_id = ?', (sid,)).fetchone() == (new_cid,)
            old_owner, = c.execute('SELECT candidate_id FROM download_jobs WHERE id = ?', (job_id,)).fetchone()
            assert (old_owner == new_cid) == (edit_kind == 'complete-gallery')
        assert c.execute('PRAGMA foreign_key_check').fetchall() == []


@pytest.mark.asyncio
async def test_editing_other_accounts_observation_does_not_cancel_selected_transfer(tmp_path):
    db, (a, b) = await setup(tmp_path)
    ingestor = CandidateIngestor(db)
    await ingestor.process_message(message(a))
    await ingestor.process_message(message(b))
    with db.connection() as c:
        cid, = c.execute('SELECT id FROM candidates').fetchone()
        selected, = c.execute('SELECT min(id) FROM source_messages').fetchone()
        job = c.execute("INSERT INTO download_jobs(candidate_id, idempotency_key, provider, state, details_json) "
                        "VALUES(?, 'unaffected', 'TELEGRAM', 'DOWNLOADING', ?)",
                        (cid, json.dumps({'observation_id': selected}))).lastrowid
    await ingestor.process_message(replace(message(b, gid=456), is_edit=True))
    with db.connection() as c:
        assert c.execute('SELECT state, candidate_id FROM download_jobs WHERE id = ?', (job,)).fetchone() == ('DOWNLOADING', cid)
        assert c.execute('SELECT count(*) FROM queue_operations').fetchone() == (0,)
        assert c.execute('SELECT count(*) FROM ingest_observation_changes').fetchone() == (0,)


@pytest.mark.asyncio
@pytest.mark.parametrize('interleaving', ['restart', 'newer-edit', 'pending-switch'])
async def test_persisted_edit_recovers_and_supersedes_old_input_and_switch(tmp_path, interleaving):
    db, (account, _) = await setup(tmp_path)
    ingestor = CandidateIngestor(db)
    await ingestor.process_message(message(account))
    with db.connection() as c:
        cid, = c.execute('SELECT id FROM candidates').fetchone()
        sid, = c.execute('SELECT id FROM source_messages').fetchone()
        c.execute("UPDATE candidates SET status = 'APPROVED' WHERE id = ?", (cid,))
        job = c.execute("INSERT INTO download_jobs(candidate_id, idempotency_key, provider, state, details_json) "
                        "VALUES(?, 'before-restart', 'TELEGRAM', 'DOWNLOADING', ?)",
                        (cid, json.dumps({'observation_id': sid}))).lastrowid
        if interleaving == 'pending-switch':
            QueueControl(db)._apply(c, job, 'switch-source', task_snapshot(c, job), {'provider': 'TELEGRAPH'})
    await ingestor.process_message(replace(message(account, gid=456), is_edit=True))
    expected_gid = 456
    if interleaving == 'newer-edit':
        await ingestor.process_message(replace(message(account, gid=789), is_edit=True))
        expected_gid = 789
    fresh = Database(db.path)
    await fresh.initialize()
    await DownloadService(fresh, tmp_path / 'fresh-work').process_operations()
    token = current_account_context.set(SimpleNamespace(account_id=account, kind='TG_BOT'))
    try:
        await CandidateIngestor(fresh).process_pending_entries()
    finally:
        current_account_context.reset(token)
    with fresh.connection() as c:
        assert c.execute('SELECT status, error_code FROM ingest_observation_changes').fetchone() == ('COMPLETED', None)
        assert c.execute('SELECT provider, state FROM download_jobs').fetchall() == [('TELEGRAM', 'CANCELLED')]
        new_cid, = c.execute('SELECT candidate_id FROM candidate_messages WHERE source_message_id = ?', (sid,)).fetchone()
        assert c.execute('SELECT ex_gid FROM candidates WHERE id = ?', (new_cid,)).fetchone() == (expected_gid,)
        if interleaving == 'newer-edit':
            assert c.execute('SELECT id FROM candidates WHERE ex_gid = 456').fetchone() is None


@pytest.mark.asyncio
async def test_decided_message_compatibility_writer_cannot_bypass_source_stop(tmp_path):
    db, (account, _) = await setup(tmp_path)
    await CandidateIngestor(db).process_message(message(account))
    with db.connection() as c:
        cid, = c.execute('SELECT id FROM candidates').fetchone()
        sid, = c.execute('SELECT id FROM source_messages').fetchone()
        c.execute("INSERT INTO download_jobs(candidate_id, idempotency_key, provider, state, details_json) "
                  "VALUES(?, 'compatibility', 'TELEGRAM', 'DOWNLOADING', ?)",
                  (cid, json.dumps({'observation_id': sid})))
    assert await db.save_candidate_message(None, replace(message(account, gid=456), is_edit=True)) is False
    with db.connection() as c:
        assert c.execute('SELECT status FROM ingest_observation_changes').fetchone() == ('PENDING',)
        assert c.execute('SELECT state FROM download_jobs').fetchone() == ('STOPPING',)
        assert c.execute('SELECT ex_gid FROM candidates WHERE id = ?', (cid,)).fetchone() == (123,)


@pytest.mark.asyncio
@pytest.mark.parametrize('running', [False, True])
async def test_reply_completing_unknown_gallery_uses_same_identity_arbiter(tmp_path, running):
    db, (account, _) = await setup(tmp_path)
    ingestor = CandidateIngestor(db)
    await ingestor.process_message(message(account, gid=None))
    with db.connection() as c:
        cid, = c.execute('SELECT id FROM candidates').fetchone()
        if running:
            c.execute("UPDATE candidates SET status = 'APPROVED' WHERE id = ?", (cid,))
            c.execute("INSERT INTO download_jobs(candidate_id, idempotency_key, provider, state) "
                      "VALUES(?, 'reply-running', 'EXHENTAI', 'DOWNLOADING')", (cid,))
    await ingestor.process_message(replace(message(account, gid=456, message_id=43), reply_to_message_id=42))
    with db.connection() as c:
        if running:
            assert c.execute('SELECT ex_gid FROM candidates WHERE id = ?', (cid,)).fetchone() == (None,)
            assert c.execute('SELECT state FROM download_jobs').fetchone() == ('STOPPING',)
            assert c.execute('SELECT status FROM ingest_reference_changes').fetchone() == ('PENDING',)
    await DownloadService(db, tmp_path / 'work').process_operations()
    with db.connection() as c:
        assert c.execute('SELECT id, ex_gid FROM candidates').fetchall() == [(cid, 456)]
        assert c.execute('SELECT count(*) FROM candidate_messages WHERE candidate_id = ?', (cid,)).fetchone() == (2,)
        assert c.execute('SELECT count(DISTINCT ingest_entry_id) FROM source_messages').fetchone() == (1,)
        assert c.execute("SELECT processing_state FROM ingest_entries WHERE unique_key = 'eh:gallery:456'").fetchone() == ('LINKED',)
