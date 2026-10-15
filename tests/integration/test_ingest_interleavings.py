"""Reference corrections, source edits and removal share real stop arbitration."""
import asyncio
from dataclasses import replace

import pytest

from app.candidates.ingestor import CandidateIngestor
from app.downloads.archived import ArchivedWorkService
from app.downloads.service import DownloadService
from tests.integration.test_ingest_entries import message
from tests.integration.test_ingest_pipeline import setup


@pytest.mark.asyncio
@pytest.mark.parametrize('order', ['reference-first', 'observation-first'])
@pytest.mark.parametrize('change', ['gallery', 'deactivate', 'purge'])
@pytest.mark.parametrize('initial_gid', [123, None])
async def test_reference_and_observation_interleave_without_relabelling_other_sources(tmp_path, monkeypatch, order, change, initial_gid):
    db, (a, b) = await setup(tmp_path)
    ingestor = CandidateIngestor(db)
    for msg in (message(a, gid=initial_gid), message(b, gid=initial_gid), message(a, gid=456, message_id=90), message(a, gid=789, message_id=91)):
        await ingestor.process_message(msg)
    with db.connection() as c:
        source, = c.execute('SELECT id FROM candidates WHERE ex_gid IS ?', (initial_gid,)).fetchone()
        target, = c.execute('SELECT id FROM candidates WHERE ex_gid = 456').fetchone()
        edited_target, = c.execute('SELECT id FROM candidates WHERE ex_gid = 789').fetchone()
        source_ids = c.execute('SELECT sm.id FROM source_messages sm JOIN candidate_messages cm ON sm.id = cm.source_message_id '
                               'WHERE cm.candidate_id = ? ORDER BY sm.id', (source,)).fetchall()
        c.execute("UPDATE candidates SET status = 'APPROVED' WHERE id = ?", (source,))
    download = DownloadService(db, tmp_path / 'work')
    candidate = await db.get_candidate(source)
    job = await download.enqueue_telegram_download(source, candidate.messages[0].attachments[0])
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def produce(job):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
    monkeypatch.setattr(download, '_handle_job', produce)
    running = asyncio.create_task(download._process_one())
    stopping = None
    async def edit():
        if change == 'deactivate':
            await db.deactivate_candidate_message(message(a).chat_id, 42, account_id=a)
        else:
            await ingestor.process_message(replace(message(a, gid=789), is_edit=True))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        if order == 'observation-first':
            await edit()
        reference = await db.set_candidate_eh_ref(source, 456, 'corrected-token')
        assert reference['status'] == 'PENDING'
        if order == 'reference-first':
            await edit()
        removals = ArchivedWorkService(db, tmp_path / 'library', tmp_path / 'work')
        if change == 'purge':
            assert (await removals.purge_work(source))['cleanup_status'] == 'PENDING'
            download._cleanup_removals = removals.process_removals
        stopping = asyncio.create_task(download.process_operations())
        await asyncio.wait_for(cleaning.wait(), 3)
        assert not stopping.done()
        with db.connection() as c:
            if change == 'purge':
                # Business deletion is immediate; its independent stop/removal
                # handles must remain pending until the actual producer exits.
                assert c.execute('SELECT status FROM queue_removals').fetchone() == ('PENDING',)
                assert c.execute('SELECT status FROM queue_operations WHERE task_id = ?', (job.task_id,)).fetchone() == ('PENDING',)
            else:
                assert c.execute('SELECT status FROM queue_tasks WHERE task_id = ?', (job.task_id,)).fetchone() == ('STOPPING',)
            assert c.execute('SELECT count(*) FROM candidates').fetchone() == (2 if change == 'purge' else 3,)
    finally:
        release.set()
        if stopping is None:
            running.cancel()
        await asyncio.gather(running, *([stopping] if stopping else []), return_exceptions=True)
    # Run once more to reconcile failures after a business removal, if needed.
    await download.process_operations()
    with db.connection() as c:
        assert c.execute('PRAGMA foreign_key_check').fetchall() == []
        assert c.execute('SELECT id, ex_gid FROM candidates ORDER BY id').fetchall() == [(target, 456), (edited_target, 789)]
        if change == 'purge':
            assert c.execute('SELECT count(*) FROM candidate_messages WHERE source_message_id IN (?, ?)',
                             (source_ids[0][0], source_ids[1][0])).fetchone() == (0,)
            assert c.execute('SELECT count(*) FROM download_jobs').fetchone() == (0,)
            assert c.execute('SELECT status FROM ingest_reference_changes').fetchone() == ('FAILED',)
            assert c.execute('SELECT status FROM ingest_observation_changes').fetchone() == ('FAILED',)
        else:
            assert c.execute('SELECT status, result_candidate_id FROM ingest_reference_changes').fetchone() == ('COMPLETED', target)
            assert c.execute('SELECT status FROM ingest_observation_changes').fetchone() == ('COMPLETED',)
            assert c.execute('SELECT candidate_id FROM candidate_messages WHERE source_message_id = ?', source_ids[1]).fetchone() == (target,)
            assert c.execute('SELECT candidate_id, state FROM download_jobs').fetchone() == (target, 'CANCELLED')
            assert c.execute('SELECT candidate_id FROM candidate_messages WHERE source_message_id = ?', source_ids[0]).fetchone() == (
                (edited_target,) if change == 'gallery' else None)


@pytest.mark.asyncio
@pytest.mark.parametrize('relation', ['media-group', 'reply'])
@pytest.mark.parametrize('initial_gid', [None, 123])
async def test_linked_relations_complete_unknown_identity_but_never_override_a_known_gallery(tmp_path, relation, initial_gid):
    db, (a, b) = await setup(tmp_path)
    ingestor = CandidateIngestor(db)
    group = 'album' if relation == 'media-group' else None
    for account in (a, b):
        await ingestor.process_message(replace(message(account, gid=initial_gid), media_group_id=group))
    supplement = replace(message(a, gid=456, message_id=43), media_group_id=group,
        reply_to_message_id=42 if relation == 'reply' else None)
    await ingestor.process_message(supplement)
    await ingestor.process_message(replace(supplement, account_id=b))
    with db.connection() as c:
        assert c.execute('SELECT count(*) FROM candidates').fetchone() == (1 if initial_gid is None else 2,)
        target, = c.execute('SELECT id FROM candidates WHERE ex_gid = 456').fetchone()
        original_owner, = c.execute('SELECT cm.candidate_id FROM candidate_messages cm JOIN source_messages sm '
                                    'ON sm.id = cm.source_message_id WHERE sm.message_id = 42 LIMIT 1').fetchone()
        assert (original_owner == target) == (initial_gid is None)
        assert c.execute('SELECT count(*) FROM candidate_messages WHERE candidate_id = ?', (target,)).fetchone() == (
            4 if initial_gid is None else 2,)
    # An edit to the already linked member moves only that account's source.
    await ingestor.process_message(replace(supplement, ex_gid=789, is_edit=True))
    with db.connection() as c:
        assert c.execute('SELECT count(*) FROM candidate_messages WHERE candidate_id = ?', (target,)).fetchone() == (
            3 if initial_gid is None else 1,)
        assert c.execute('SELECT count(*) FROM candidate_messages WHERE candidate_id = ?', (original_owner,)).fetchone() == (
            3 if initial_gid is None else 2,)
        assert c.execute('SELECT count(*) FROM candidate_messages cm JOIN candidates c ON c.id = cm.candidate_id WHERE c.ex_gid = 789').fetchone() == (1,)
        assert c.execute('PRAGMA foreign_key_check').fetchall() == []
