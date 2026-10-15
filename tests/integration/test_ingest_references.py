"""Gallery correction converges keys only after the real old execution exits."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from app.candidates.entries import IngestError, check_claim, claim_entry, register_observation
from app.candidates.references import process_reference_changes
from app.db.database import Database
from app.downloads.queue import QueueControl, task_snapshot
from app.downloads.service import DownloadService
from tests.integration.test_ingest_entries import message


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / 'reference.db')
    asyncio.run(db.initialize())
    return db


def seed(database, *, gid=None, mid=42):
    asyncio.run(database.save_candidate_message(None, message(gid=gid, message_id=mid)))
    with database.connection() as c:
        return c.execute('SELECT max(id) FROM candidates').fetchone()[0]


def test_fill_gallery_merges_review_and_terminal_artifacts_without_losing_identity(database):
    source = seed(database)
    target = seed(database, gid=123, mid=90)
    with database.connection() as c:
        c.execute("UPDATE candidates SET status = 'APPROVED' WHERE id = ?", (source,))
        c.execute("INSERT INTO metadata_values(candidate_id, field_name, field_value, value_source, is_manual) "
                  "VALUES(?, 'Title', 'chosen', 'MANUAL', 1)", (source,))
        job = c.execute("INSERT INTO download_jobs(candidate_id, idempotency_key, provider, state) "
                        "VALUES(?, 'history', 'TELEGRAM', 'COMPLETED')", (source,)).lastrowid
        c.execute("INSERT INTO artifacts(job_id, artifact_type, path) VALUES(?, 'ARCHIVE', 'kept.zip')", (job,))
        task_id = c.execute('SELECT task_id FROM download_jobs WHERE id = ?', (job,)).fetchone()[0]
    result = asyncio.run(database.set_candidate_eh_ref(source, 123, 'new-token'))
    assert result['status'] == 'COMPLETED' and result['result_candidate_id'] == target
    with database.connection() as c:
        assert c.execute('SELECT id, ex_gid, status FROM candidates').fetchall() == [(target, 123, 'APPROVED')]
        assert c.execute('SELECT task_id, candidate_id FROM download_jobs WHERE id = ?', (job,)).fetchone() == (task_id, target)
        assert c.execute('SELECT path FROM artifacts WHERE job_id = ?', (job,)).fetchone() == ('kept.zip',)
        assert c.execute('SELECT field_value FROM metadata_values WHERE candidate_id = ?', (target,)).fetchone() == ('chosen',)
        assert c.execute('SELECT count(*) FROM candidate_messages WHERE candidate_id = ?', (target,)).fetchone() == (2,)
        assert c.execute("SELECT processing_state, candidate_id FROM ingest_entries WHERE unique_key LIKE 'tg:%'").fetchone() == ('MERGED', None)
        assert c.execute('SELECT count(DISTINCT ingest_entry_id) FROM source_messages').fetchone() == (1,)
    # Replaying the original gallery-less observation must find the corrected work.
    assert asyncio.run(database.save_candidate_message(None, message(gid=None))) is False
    with database.connection() as c:
        assert c.execute('SELECT count(*) FROM candidates').fetchone() == (1,)


def test_pending_target_processor_is_revoked_before_its_late_publish(database):
    source = seed(database)
    with database.connection() as c:
        c.execute('BEGIN IMMEDIATE')
        entry = register_observation(c, message(gid=123, message_id=91))
        claim = claim_entry(c, entry['id'], owner='old-ai')
    result = asyncio.run(database.set_candidate_eh_ref(source, 123, 'new-token'))
    assert result['result_candidate_id'] == source
    with database.connection() as c:
        with pytest.raises(IngestError) as error:
            check_claim(c, claim)
        assert error.value.code == 'INGEST_STALE_PROCESSOR'
        assert c.execute('SELECT count(*) FROM candidates').fetchone() == (1,)
        assert c.execute('SELECT count(*) FROM candidate_messages WHERE candidate_id = ?', (source,)).fetchone() == (2,)


def test_target_processor_is_revoked_at_stop_admission_not_only_at_final_merge(database):
    source = seed(database)
    with database.connection() as c:
        c.execute("INSERT INTO download_jobs(candidate_id, idempotency_key, provider, state) "
                  "VALUES(?, 'still-running', 'EXHENTAI', 'DOWNLOADING')", (source,))
        entry = register_observation(c, message(gid=123, message_id=91))
        claim = claim_entry(c, entry['id'], owner='target-ai')
    result = asyncio.run(database.set_candidate_eh_ref(source, 123, 'new-token'))
    assert result['status'] == 'PENDING'
    with database.connection() as c:
        with pytest.raises(IngestError) as error:
            check_claim(c, claim)
        assert error.value.code == 'INGEST_STALE_PROCESSOR'
        assert claim_entry(c, entry['id'], owner='next-ai') is None
        assert c.execute('SELECT count(*) FROM candidates').fetchone() == (1,)


@pytest.mark.asyncio
async def test_live_download_must_exit_finally_before_merge(database, tmp_path):
    # Do not call the synchronous seed helper inside the running event loop.
    await database.save_candidate_message(None, message(gid=None))
    await database.save_candidate_message(None, message(gid=123, message_id=90))
    with database.connection() as c:
        source, target = [row[0] for row in c.execute('SELECT id FROM candidates ORDER BY id')]
        c.execute("UPDATE candidates SET status = 'APPROVED' WHERE id = ?", (source,))
        job_id = c.execute("INSERT INTO download_jobs(candidate_id, idempotency_key, provider, state) "
                           "VALUES(?, 'running', 'EXHENTAI', 'PENDING')", (source,)).lastrowid
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def download(cid):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
    service = DownloadService(database, tmp_path / 'work', exhentai_download=download)
    running = asyncio.create_task(service._process_one())
    stopping = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        result = await database.set_candidate_eh_ref(source, 123, 'new-token')
        assert result['status'] == 'PENDING'
        stopping = asyncio.create_task(service.process_operations())
        await asyncio.wait_for(cleaning.wait(), 5)
        assert not stopping.done()
        with database.connection() as c:
            assert c.execute('SELECT state FROM download_jobs WHERE id = ?', (job_id,)).fetchone() == ('STOPPING',)
            assert c.execute('SELECT count(*) FROM candidates').fetchone() == (2,)
            with pytest.raises(sqlite3.IntegrityError, match='INGEST_IDENTITY_CHANGING'):
                c.execute("INSERT INTO download_jobs(candidate_id, idempotency_key, provider, state) "
                          "VALUES(?, 'another-source', 'TELEGRAPH', 'PENDING')", (source,))
        assert process_reference_changes(database) == []
    finally:
        release.set()
        await asyncio.gather(running, *([stopping] if stopping else []), return_exceptions=True)
    with database.connection() as c:
        assert c.execute('SELECT candidate_id, state FROM download_jobs WHERE id = ?', (job_id,)).fetchone() == (target, 'CANCELLED')
        assert c.execute('SELECT status, result_candidate_id FROM ingest_reference_changes').fetchone() == ('COMPLETED', target)


def test_persisted_reference_recovers_after_queue_stop_and_restart(database, tmp_path):
    source = seed(database)
    with database.connection() as c:
        job = c.execute("INSERT INTO download_jobs(candidate_id, idempotency_key, provider, state) "
                        "VALUES(?, 'recover', 'EXHENTAI', 'DOWNLOADING')", (source,)).lastrowid
    result = asyncio.run(database.set_candidate_eh_ref(source, 123, 'new-token'))
    assert result['status'] == 'PENDING'
    fresh = Database(database.path)
    asyncio.run(fresh.initialize())
    asyncio.run(DownloadService(fresh, tmp_path / 'work').process_operations())
    with fresh.connection() as c:
        assert c.execute('SELECT status, result_candidate_id FROM ingest_reference_changes').fetchone() == ('COMPLETED', source)
        assert c.execute('SELECT ex_gid FROM candidates WHERE id = ?', (source,)).fetchone() == (123,)
        assert c.execute('SELECT state FROM download_jobs WHERE id = ?', (job,)).fetchone() == ('CANCELLED',)


def test_identity_change_supersedes_already_pending_switch_instead_of_starting_it(database, tmp_path):
    source = seed(database)
    with database.connection() as c:
        c.execute("UPDATE candidates SET status = 'APPROVED' WHERE id = ?", (source,))
        job = c.execute("INSERT INTO download_jobs(candidate_id, idempotency_key, provider, state) "
                        "VALUES(?, 'switching', 'EXHENTAI', 'DOWNLOADING')", (source,)).lastrowid
        QueueControl(database)._apply(c, job, 'switch-source', task_snapshot(c, job), {'provider': 'TELEGRAPH'})
    assert asyncio.run(database.set_candidate_eh_ref(source, 123, 'new-token'))['status'] == 'PENDING'
    asyncio.run(DownloadService(database, tmp_path / 'work').process_operations())
    with database.connection() as c:
        assert c.execute('SELECT provider, state FROM download_jobs').fetchall() == [('EXHENTAI', 'CANCELLED')]
        assert c.execute('SELECT ex_gid FROM candidates').fetchone() == (123,)


def test_relink_old_gallery_is_not_an_alias_for_new_old_gallery_posts(database):
    source = seed(database, gid=111)
    assert asyncio.run(database.set_candidate_eh_ref(source, 123, 'new-token'))['status'] == 'COMPLETED'
    with database.connection() as c:
        assert c.execute("SELECT processing_state, candidate_id FROM ingest_entries WHERE unique_key = 'eh:gallery:111'").fetchone() == ('REMOVED', None)
        assert c.execute("SELECT processing_state, candidate_id FROM ingest_entries WHERE unique_key = 'eh:gallery:123'").fetchone() == ('LINKED', source)


def test_manual_conflict_rolls_back_reference_and_history(database):
    source = seed(database)
    target = seed(database, gid=123, mid=90)
    with database.connection() as c:
        c.executemany("INSERT INTO metadata_values(candidate_id, field_name, field_value, value_source, is_manual) "
                      "VALUES(?, 'Title', ?, 'MANUAL', 1)", [(source, 'a'), (target, 'b')])
    with pytest.raises(IngestError) as error:
        asyncio.run(database.set_candidate_eh_ref(source, 123, 'new-token'))
    assert error.value.code == 'INGEST_MANUAL_METADATA_CONFLICT'
    with database.connection() as c:
        assert c.execute('SELECT id, ex_gid FROM candidates ORDER BY id').fetchall() == [(source, None), (target, 123)]
        assert c.execute('SELECT count(*) FROM ingest_reference_changes').fetchone() == (0,)


def test_concurrent_different_corrections_cannot_replace_pending_intent(database):
    source = seed(database)
    with database.connection() as c:
        c.execute("INSERT INTO download_jobs(candidate_id, idempotency_key, provider, state) "
                  "VALUES(?, 'running', 'EXHENTAI', 'DOWNLOADING')", (source,))
    def correct(gid):
        try:
            return asyncio.run(database.set_candidate_eh_ref(source, gid, 'token'))['status']
        except IngestError as error:
            return error.code
    with ThreadPoolExecutor(2) as workers:
        results = list(workers.map(correct, [123, 456]))
    assert sorted(results) == ['INGEST_IDENTITY_CHANGING', 'PENDING']
    with database.connection() as c:
        assert c.execute('SELECT count(*) FROM ingest_reference_changes').fetchone() == (1,)


def test_resaving_unchanged_reference_does_not_stop_download(database):
    source = seed(database, gid=123)
    with database.connection() as c:
        c.execute("INSERT INTO download_jobs(candidate_id, idempotency_key, provider, state) "
                  "VALUES(?, 'unchanged', 'EXHENTAI', 'DOWNLOADING')", (source,))
    result = asyncio.run(database.set_candidate_eh_ref(source, 123, 'fixture-token'))
    assert result['status'] == 'COMPLETED' and result['operation_id'] is None
    with database.connection() as c:
        assert c.execute('SELECT state FROM download_jobs').fetchone() == ('DOWNLOADING',)
        assert c.execute('SELECT count(*) FROM queue_operations').fetchone() == (0,)


def test_merge_keeps_source_field_lock_and_rejects_conflicting_locked_values(database):
    source = seed(database)
    target = seed(database, gid=123, mid=90)
    with database.connection() as c:
        c.execute("INSERT INTO metadata_values(candidate_id, field_name, field_value, value_source, is_locked) "
                  "VALUES(?, 'Title', 'pinned', 'EXHENTAI', 1)", (source,))
    asyncio.run(database.set_candidate_eh_ref(source, 123, 'new-token'))
    with database.connection() as c:
        assert c.execute('SELECT field_value, is_locked FROM metadata_values WHERE candidate_id = ?', (target,)).fetchone() == ('pinned', 1)
    another = seed(database, gid=None, mid=92)
    with database.connection() as c:
        c.execute("INSERT INTO metadata_values(candidate_id, field_name, field_value, value_source, is_locked) "
                  "VALUES(?, 'Title', 'different', 'EXHENTAI', 1)", (another,))
    with pytest.raises(IngestError) as error:
        asyncio.run(database.set_candidate_eh_ref(another, 123, 'new-token'))
    assert error.value.code == 'INGEST_MANUAL_METADATA_CONFLICT'
