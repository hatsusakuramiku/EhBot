"""Manual request receipts share the real candidate publication transaction."""
import asyncio
from types import SimpleNamespace

import pytest

from app.review.manual import add_manual_work, manual_result, ManualRequest
from app.review.commands import WorkCommandError
from app.exhentai.service import ExHentaiDownloadError
from tests.integration.test_manual_ingest_identity import Gallery
from tests.integration.test_ingest_pipeline import setup


URL = 'https://e-hentai.org/g/123/token/'


async def add(database, gallery, key='manual', *, configured=True, url=URL):
    orchestrator = SimpleNamespace(route_source=lambda candidate: SimpleNamespace(provider=None))
    return await add_manual_work(database, gallery, orchestrator, None,
        {'url': url, 'idempotency_key': key}, configured=configured)


@pytest.mark.asyncio
async def test_concurrent_replay_reads_pending_then_original_success_without_new_fetch(tmp_path):
    database, accounts = await setup(tmp_path)
    gallery = Gallery(database)
    first = asyncio.create_task(add(database, gallery))
    try:
        await asyncio.wait_for(gallery.entered.wait(), 5)
        pending = await add(database, gallery)
        assert pending['status'] == 'RUNNING' and gallery.calls == 1
    finally:
        gallery.release.set()
    result = await first
    assert result['status'] == 'COMPLETED' and result['manual_add_operation_id'] == pending['manual_add_operation_id']
    assert await add(database, gallery, configured=False) == result
    with database.connection() as connection:
        connection.execute("UPDATE candidates SET status = 'REJECTED' WHERE id = ?", (result['candidate_id'],))
    assert await manual_result(database, result['manual_add_operation_id']) == result
    assert gallery.calls == 1


@pytest.mark.asyncio
async def test_same_key_different_link_refused_and_new_key_returns_fixed_duplicate(tmp_path):
    database, accounts = await setup(tmp_path)
    gallery = Gallery(database)
    gallery.release.set()
    first = await add(database, gallery)
    with pytest.raises(WorkCommandError) as error:
        await add(database, gallery, url='https://e-hentai.org/g/124/other/')
    assert error.value.code == 'IDEMPOTENCY_CONFLICT'
    for _ in range(2):
        with pytest.raises(WorkCommandError) as error:
            await add(database, gallery, key='duplicate')
        assert error.value.code == 'CANDIDATE_DUPLICATE'
        assert error.value.details['existing_candidate_id'] == first['candidate_id']
        assert error.value.details['existing_work_id'] == first['work_id']
    assert gallery.calls == 1


@pytest.mark.asyncio
async def test_duplicate_identity_is_captured_before_failure_receipt_write(tmp_path, monkeypatch):
    import app.review.manual as module
    database, _ = await setup(tmp_path)
    gallery = Gallery(database)
    gallery.release.set()
    first = await add(database, gallery)
    fail = module._fail

    def recycle_before_receipt(database, operation_id, status, error):
        with database.connection() as connection:
            connection.execute('DELETE FROM candidates WHERE id = ?', (first['candidate_id'],))
            connection.execute("INSERT INTO candidates(id, status) VALUES(?, 'PENDING_REVIEW')", (first['candidate_id'],))
        fail(database, operation_id, status, error)

    monkeypatch.setattr(module, '_fail', recycle_before_receipt)
    for _ in range(2):
        with pytest.raises(WorkCommandError) as failure:
            await add(database, gallery, key='duplicate-after-recycle')
        assert failure.value.details['existing_work_id'] == first['work_id']
    with database.connection() as connection:
        assert connection.execute('SELECT work_id FROM work_versions WHERE candidate_id = ?',
                                  (first['candidate_id'],)).fetchone()[0] != first['work_id']
    assert gallery.calls == 1


@pytest.mark.asyncio
async def test_supplier_refusal_fixed_and_does_not_leave_candidate(tmp_path):
    database, accounts = await setup(tmp_path)
    gallery = Gallery(database)
    gallery.error = ExHentaiDownloadError('FIXTURE_REFUSED', 'Fixture supplier refusal')
    gallery.release.set()
    for _ in range(2):
        with pytest.raises(WorkCommandError) as error:
            await add(database, gallery)
        assert error.value.code == 'FIXTURE_REFUSED'
    assert gallery.calls == 1
    with database.connection() as connection:
        assert connection.execute('SELECT count(*) FROM candidates').fetchone() == (0,)
        assert connection.execute('SELECT status FROM manual_work_requests').fetchone() == ('FAILED',)


@pytest.mark.asyncio
async def test_publish_receipt_failure_rolls_back_candidate_metadata_and_download_intent(tmp_path, monkeypatch):
    database, accounts = await setup(tmp_path)
    gallery = Gallery(database)
    gallery.release.set()
    original = ManualRequest.complete

    def fail(self, *args):
        original(self, *args)
        raise RuntimeError('Fixture receipt failure')

    monkeypatch.setattr(ManualRequest, 'complete', fail)
    with pytest.raises(RuntimeError):
        await add(database, gallery)
    with database.connection() as connection:
        for table in ('candidates', 'metadata_values', 'manual_download_intents'):
            assert connection.execute('SELECT count(*) FROM ' + table).fetchone() == (0,)
    result = await add(database, gallery)
    assert result['status'] == 'UNKNOWN' and gallery.calls == 1


@pytest.mark.asyncio
async def test_cancel_before_publication_is_unknown_and_never_fetches_on_replay(tmp_path):
    database, accounts = await setup(tmp_path)
    gallery = Gallery(database)
    first = asyncio.create_task(add(database, gallery))
    await asyncio.wait_for(gallery.entered.wait(), 5)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    result = await add(database, gallery)
    assert result['status'] == 'UNKNOWN' and gallery.calls == 1
    with database.connection() as connection:
        assert connection.execute('SELECT count(*) FROM candidates').fetchone() == (0,)


@pytest.mark.asyncio
async def test_failure_after_publication_keeps_success_receipt_and_persistent_download_intent(tmp_path, monkeypatch):
    database, accounts = await setup(tmp_path)
    gallery = Gallery(database)
    gallery.release.set()
    import app.candidates.manual as module

    async def disconnect(*args, **kwargs):
        raise RuntimeError('Fixture response lost after commit')

    monkeypatch.setattr(module, 'enqueue_manual_candidate', disconnect)
    with pytest.raises(RuntimeError):
        await add(database, gallery)
    result = await add(database, gallery)
    assert result['status'] == 'COMPLETED' and gallery.calls == 1
    with database.connection() as connection:
        assert connection.execute('SELECT candidate_id FROM manual_download_intents').fetchone() == (result['candidate_id'],)


@pytest.mark.asyncio
async def test_preconditions_and_config_refusals_do_not_accept_request(tmp_path):
    database, accounts = await setup(tmp_path)
    gallery = Gallery(database)
    for key, configured, url, code in [(None, True, URL, 'IDEMPOTENCY_KEY_REQUIRED'),
            ('a', False, URL, 'EXHENTAI_NOT_CONFIGURED'), ('b', True, 'bad link', 'INVALID_LINK')]:
        with pytest.raises(WorkCommandError) as error:
            await add(database, gallery, key, configured=configured, url=url)
        assert error.value.code == code
    with database.connection() as connection:
        assert connection.execute('SELECT count(*) FROM manual_work_requests').fetchone() == (0,)


@pytest.mark.asyncio
async def test_lost_session_cannot_publish_and_read_marks_unknown_without_new_network(tmp_path):
    from app.review.manual import _begin
    database, accounts = await setup(tmp_path)
    with database.connection() as connection:
        connection.execute("INSERT INTO worker_sessions(session_id, process_id, status, expires_at) "
                           "VALUES('fixture-session', 1, 'LIVE', datetime('now', '+1 minute'))")
    database.worker_session = SimpleNamespace(session_id='fixture-session', stopping=False)
    result, dispatch = _begin(database, URL, 'session', True)
    with database.connection() as connection:
        connection.execute("UPDATE worker_sessions SET status = 'ABANDONED' WHERE session_id = 'fixture-session'")
        with pytest.raises(WorkCommandError):
            ManualRequest(database, result['manual_add_operation_id']).complete(connection, 1)
    state = await manual_result(database, result['manual_add_operation_id'])
    assert state['status'] == 'UNKNOWN'
    gallery = Gallery(database)
    assert await add(database, gallery, key='session') == state
    assert gallery.calls == 0
