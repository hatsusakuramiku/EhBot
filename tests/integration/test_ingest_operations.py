"""Durable identity results, CAS retries and rollback/new-input races."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
import json

import pytest
from fastapi.testclient import TestClient

from app.candidates import changes
from app.candidates.entries import IngestError, check_claim, claim_entry, register_observation
from app.candidates.operations import get_operation, list_operations, retry_operation
from app.candidates.references import process_reference_changes
from app.db.database import Database
from app.downloads.queue import QueueControl
from app.downloads.archived import ArchivedWorkService
from app.main import create_app
from tests.integration.test_ingest_entries import message
from tests.integration.test_ingest_references import seed
from tests.integration.test_realtime import call, hello
from tests.integration.test_work_detail_web import authenticate, make_settings


def reference_conflict(db):
    """Use a real stop intent followed by a real conflicting merge."""
    source = seed(db)
    target = seed(db, gid=123, mid=90)
    with db.connection() as c:
        c.executemany("INSERT INTO metadata_values(candidate_id, field_name, field_value, value_source, is_manual) "
                      "VALUES(?, 'Title', ?, 'MANUAL', 1)", [(source, 'one'), (target, 'two')])
        c.execute("INSERT INTO download_jobs(candidate_id, idempotency_key, provider, state) "
                  "VALUES(?, 'to-stop', 'EXHENTAI', 'DOWNLOADING')", (source,))
    pending = asyncio.run(db.set_candidate_eh_ref(source, 123, 'private-gallery-value'))
    queue = QueueControl(db)
    for operation in queue.pending_operations():
        queue.finish_operation(operation)
    process_reference_changes(db)
    oid = 'reference:' + pending['operation_id']
    assert get_operation(db, oid)['status'] == 'FAILED'
    return source, target, oid


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / 'operations.db')
    asyncio.run(db.initialize())
    return db


def test_conflict_is_visible_and_retry_atomically_replays_receipt_after_merge(database):
    source, target, oid = reference_conflict(database)
    failed = get_operation(database, oid)
    assert failed['error_code'] == 'INGEST_MANUAL_METADATA_CONFLICT'
    assert failed['retryable'] and failed['revision'] == 2
    assert 'private-gallery-value' not in json.dumps(failed)
    with database.connection() as c:
        c.execute("UPDATE metadata_values SET field_value = 'two' WHERE candidate_id = ?", (source,))
    command = {'expected_revision': failed['revision'], 'idempotency_key': 'same-retry'}
    accepted = retry_operation(database, oid, **command)
    assert accepted['status'] == 'PENDING' and accepted['revision'] == 3
    assert process_reference_changes(database)[0]['status'] == 'COMPLETED'
    final = get_operation(database, oid)
    assert final['result_candidate_id'] == target and final['revision'] == 4
    assert final in list_operations(database, target)
    assert retry_operation(database, oid, **command) == accepted
    with database.connection() as c:
        assert c.execute('SELECT id FROM candidates WHERE id = ?', (source,)).fetchone() is None
        assert c.execute('SELECT count(*) FROM ingest_command_receipts').fetchone() == (1,)
    with pytest.raises(IngestError, match='IDEMPOTENCY_CONFLICT'):
        retry_operation(database, oid, expected_revision=4, idempotency_key='same-retry')


def test_two_clients_with_the_same_failed_revision_have_one_winner(database):
    _, _, oid = reference_conflict(database)
    revision = get_operation(database, oid)['revision']
    def submit(key):
        try:
            return retry_operation(database, oid, expected_revision=revision, idempotency_key=key)['status']
        except IngestError as exc:
            return exc.code
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(submit, ['first-client', 'second-client'])) == ['PENDING', 'REVISION_CONFLICT']


@pytest.mark.parametrize('change', ['remove', 'new-reference'])
def test_old_failed_reference_cannot_revive_removed_or_newly_corrected_work(database, change):
    source, _, oid = reference_conflict(database)
    failed = get_operation(database, oid)
    if change == 'remove':
        service = ArchivedWorkService(database, database.path.parent / 'library', database.path.parent / 'work')
        asyncio.run(service.purge_work(source, delete_files=False))
    else:
        asyncio.run(database.set_candidate_eh_ref(source, 999, 'replacement'))
    assert get_operation(database, oid)['retryable'] is False
    with pytest.raises(IngestError, match='INGEST_CANDIDATE_NOT_FOUND|INGEST_REFERENCE_CHANGED'):
        retry_operation(database, oid, expected_revision=failed['revision'], idempotency_key='old-input')
    with database.connection() as c:
        assert c.execute('SELECT count(*) FROM ingest_command_receipts').fetchone() == (0,)


@pytest.mark.parametrize('revision,key,code', [
    (None, 'key', 'PRECONDITION_REQUIRED'), (True, 'key', 'PRECONDITION_REQUIRED'),
    (0, 'key', 'PRECONDITION_REQUIRED'), (1, '', 'IDEMPOTENCY_KEY_REQUIRED'),
])
def test_retry_requires_explicit_command_preconditions(database, revision, key, code):
    with pytest.raises(IngestError, match=code):
        retry_operation(database, 'reference:missing', expected_revision=revision, idempotency_key=key)


def pending_observation(db):
    source = seed(db)
    with db.connection() as c:
        sid = c.execute('SELECT id FROM source_messages').fetchone()[0]
        c.execute("INSERT INTO download_jobs(candidate_id, idempotency_key, provider, state, details_json) "
                  "VALUES(?, 'observation-stop', 'TELEGRAM', 'DOWNLOADING', ?)",
                  (source, json.dumps({'observation_id': sid})))
        changes.defer_observation_change(c, db, replace(message(gid=None), is_edit=True, message_text='first-edit'))
    for operation in QueueControl(db).pending_operations():
        QueueControl(db).finish_operation(operation)
    return source, sid, 'observation:' + str(sid)


def test_failed_source_change_can_retry_after_conflict_resolution(database, monkeypatch):
    source, sid, oid = pending_observation(database)
    original_edit = changes.edit_observation
    def conflict(*args):
        raise IngestError('INGEST_MANUAL_METADATA_CONFLICT')
    monkeypatch.setattr(changes, 'edit_observation', conflict)
    assert changes.process_observation_changes(database) == [source]
    failed = get_operation(database, oid)
    assert failed['retryable'] and failed['revision'] == 2
    accepted = retry_operation(database, oid, expected_revision=2, idempotency_key='source-retry')
    assert accepted['revision'] == 3
    monkeypatch.setattr(changes, 'edit_observation', original_edit)
    changes.process_observation_changes(database)
    assert get_operation(database, oid)['status'] == 'COMPLETED'
    with database.connection() as c:
        assert c.execute('SELECT message_text FROM source_messages WHERE id = ?', (sid,)).fetchone() == ('first-edit',)


def test_failure_writeback_does_not_mark_newer_source_edit_failed(database, monkeypatch):
    _, sid, oid = pending_observation(database)
    original_connection = database.connection
    original_edit = changes.edit_observation
    newer = replace(message(gid=None), is_edit=True, message_text='newer-edit')
    injected = False
    @contextmanager
    def interleaved_connection():
        nonlocal injected
        try:
            with original_connection() as c:
                yield c
        except IngestError:
            # The first operation has rolled back. A listener commits newer
            # input before the failed processor can open its writeback txn.
            if not injected:
                injected = True
                with original_connection() as c:
                    changes.defer_observation_change(c, database, newer)
            raise
    def conflict(*args):
        raise IngestError('INGEST_MANUAL_METADATA_CONFLICT')
    monkeypatch.setattr(database, 'connection', interleaved_connection)
    monkeypatch.setattr(changes, 'edit_observation', conflict)
    assert changes.process_observation_changes(database) == []
    pending = get_operation(database, oid)
    assert injected and pending['status'] == 'PENDING' and pending['revision'] == 2
    monkeypatch.setattr(changes, 'edit_observation', original_edit)
    changes.process_observation_changes(database)
    assert get_operation(database, oid)['status'] == 'COMPLETED'
    with database.connection() as c:
        assert c.execute('SELECT message_text FROM source_messages WHERE id = ?', (sid,)).fetchone() == ('newer-edit',)


def test_new_source_edit_invalidates_old_failed_retry(database, monkeypatch):
    _, _, oid = pending_observation(database)
    def conflict(*args):
        raise IngestError('INGEST_MANUAL_METADATA_CONFLICT')
    monkeypatch.setattr(changes, 'edit_observation', conflict)
    changes.process_observation_changes(database)
    failed = get_operation(database, oid)
    # A later, immediate edit needs no new stop; it still invalidates old input.
    asyncio.run(database.save_candidate_message(None, replace(message(gid=None), is_edit=True, message_text='latest')))
    assert not get_operation(database, oid)['retryable']
    with pytest.raises(IngestError, match='INGEST_OBSERVATION_CHANGED'):
        retry_operation(database, oid, expected_revision=failed['revision'], idempotency_key='stale-source')


@pytest.mark.parametrize('binding', ['observation_id', 'download_observation_id'])
def test_source_retry_stops_a_new_execution_bound_to_the_old_observation(database, monkeypatch, binding):
    source, sid, oid = pending_observation(database)
    def conflict(*args):
        raise IngestError('INGEST_MANUAL_METADATA_CONFLICT')
    monkeypatch.setattr(changes, 'edit_observation', conflict)
    changes.process_observation_changes(database)
    with database.connection() as c:
        job = c.execute("INSERT INTO download_jobs(candidate_id, idempotency_key, provider, state, details_json) "
                        "VALUES(?, 'new-execution', 'TELEGRAM', 'DOWNLOADING', ?)",
                        (source, json.dumps({binding: sid}))).lastrowid
        task_id = c.execute('SELECT task_id FROM download_jobs WHERE id = ?', (job,)).fetchone()[0]
    retry_operation(database, oid, expected_revision=get_operation(database, oid)['revision'], idempotency_key='restop')
    with database.connection() as c:
        assert c.execute('SELECT state FROM download_jobs WHERE id = ?', (job,)).fetchone() == ('STOPPING',)
        assert task_id in json.loads(c.execute('SELECT task_ids_json FROM ingest_observation_changes').fetchone()[0])
    assert changes.process_observation_changes(database) == []
    assert get_operation(database, oid)['status'] == 'PENDING'


def test_source_merge_keeps_a_queryable_completed_result_and_operation_version(database):
    source = seed(database)
    target = seed(database, gid=123, mid=90)
    with database.connection() as c:
        sid = c.execute('SELECT source_message_id FROM candidate_messages WHERE candidate_id = ?', (source,)).fetchone()[0]
        c.execute("INSERT INTO download_jobs(candidate_id, idempotency_key, provider, state) "
                  "VALUES(?, 'merge-history', 'EXHENTAI', 'DOWNLOADING')", (source,))
        changes.defer_observation_change(c, database, replace(message(gid=123), is_edit=True))
    for operation in QueueControl(database).pending_operations():
        QueueControl(database).finish_operation(operation)
    changes.process_observation_changes(database)
    result = get_operation(database, 'observation:' + str(sid))
    assert result['status'] == 'COMPLETED' and result['result_candidate_id'] == target
    assert result['candidate_id'] == target and result['revision'] == 3
    assert result['original_candidate_id'] == source
    assert result in list_operations(database, target)
    assert result in list_operations(database, source)


def test_source_completion_reserves_target_key_and_revokes_its_old_processor(database):
    source = seed(database)
    with database.connection() as c:
        entry = register_observation(c, message(gid=123, message_id=90))
        claim = claim_entry(c, entry['id'], owner='target-processor')
        c.execute("INSERT INTO download_jobs(candidate_id, idempotency_key, provider, state) "
                  "VALUES(?, 'completion-stop', 'EXHENTAI', 'DOWNLOADING')", (source,))
        changes.defer_observation_change(c, database, replace(message(gid=123), is_edit=True))
        with pytest.raises(IngestError, match='INGEST_STALE_PROCESSOR'):
            check_claim(c, claim)
        assert claim_entry(c, entry['id'], owner='replacement') is None
    for operation in QueueControl(database).pending_operations():
        QueueControl(database).finish_operation(operation)
    changes.process_observation_changes(database)
    with database.connection() as c:
        assert c.execute('SELECT id, ex_gid FROM candidates').fetchall() == [(source, 123)]
        assert c.execute('SELECT count(*) FROM candidate_messages WHERE candidate_id = ?', (source,)).fetchone() == (2,)
        assert c.execute('PRAGMA foreign_key_check').fetchall() == []


def test_http_web_and_ws_share_safe_results_and_versioned_retry(tmp_path):
    settings = make_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        authenticate(client, settings)
        # Only this test controls the background operation transitions.
        client.portal.call(app.state.download_service.stop)
        source, target, oid = reference_conflict(app.state.database)
        path = '/api/v1/ingest-operations/' + oid
        failed = client.get(path)
        assert failed.status_code == 200 and failed.headers['cache-control'] == 'no-store'
        assert failed.json()['state']['label'] == '变更失败'
        assert 'private-gallery-value' not in failed.text
        page = client.get(f'/works/{source}')
        assert '重试变更' in page.text and '人工或锁定字段冲突' in page.text
        assert client.get(f'/api/v1/works/{source}').json()['ingest_operations'][0] == failed.json()
        body = {'expected_revision': failed.json()['revision'], 'idempotency_key': 'http-retry'}
        assert client.post(path + '/retry', json=body).status_code == 403
        headers = {'X-CSRF-Token': page.context['csrf_token']}
        assert client.post(path + '/retry', json={'idempotency_key': 'missing-revision'}, headers=headers).status_code == 428
        with client.websocket_connect('/ws/ui', headers={'Origin': 'http://testserver'}) as ws:
            ready = hello(client, ws)
            assert 'api.api_ingest_retry' in ready['commands']
            result, _ = call(ws, 'api.api_ingest_retry', {'params': {'operation_id': oid}, 'body': body})
            assert result['type'] == 'command.result', result
            assert result['status'] == 202 and result['data']['status'] == 'PENDING'
        assert client.post(path + '/retry', json=body, headers=headers).json() == result['data']
        assert client.post(path + '/retry', json={**body, 'idempotency_key': 'other-client'}, headers=headers).status_code == 409
        assert '等待停止并应用变更' in client.get(f'/works/{source}').text
        # A later asynchronous failure invalidates both clients' earlier view.
        sequence = app.state.event_bus.sequence
        client.portal.call(app.state.download_service.process_operations)
        assert app.state.event_bus.sequence > sequence
        failed_again = client.get(path).json()
        assert failed_again['status'] == 'FAILED' and failed_again['revision'] == 4
        form_page = client.get(f'/works/{source}')
        form_result = client.post(f'/works/{source}/ingest-operations/{oid}/retry', data={
            'csrf_token': form_page.context['csrf_token'], 'expected_revision': failed_again['revision'],
            'idempotency_key': 'form-retry',
        }, follow_redirects=False)
        assert form_result.status_code == 303
        assert client.get(path).json()['status'] == 'PENDING'
        with app.state.database.connection() as c:
            c.execute("UPDATE metadata_values SET field_value = 'two' WHERE candidate_id = ?", (source,))
        process_reference_changes(app.state.database)
        completed = client.get(path).json()
        assert completed['status'] == 'COMPLETED' and completed['result_candidate_id'] == target
        assert '变更已完成' in client.get(f'/works/{target}').text
        redirected = client.get(f'/works/{source}', follow_redirects=False)
        assert redirected.status_code == 303 and redirected.headers['location'] == f'/works/{target}'
        token = client.post('/api/v1/auth/login', json={
            'username': 'admin', 'password': 'new-password-with-12-characters',
        }).json()['access_token']
        client.cookies.clear()
        assert client.get(path).status_code == 401
        bearer = {'Authorization': 'Bearer ' + token}
        assert client.get(path, headers=bearer).json() == completed
        # Cookie and Bearer share the administrator's receipt namespace, and
        # Bearer writes require no CSRF token.
        assert client.post(path + '/retry', json=body, headers=bearer).json() == result['data']
        final_retry = client.post(path + '/retry', json={
            'expected_revision': completed['revision'], 'idempotency_key': 'completed-retry',
        }, headers=bearer)
        assert final_retry.status_code == 409 and final_retry.json()['error']['code'] == 'INGEST_OPERATION_NOT_FAILED'
