"""Shared metadata API: retries, stale clients and durable file follow-ups."""
import asyncio
import json
from html.parser import HTMLParser
from xml.etree import ElementTree as ET
import zipfile

from fastapi.testclient import TestClient

from app.api.deps import CSRF_HEADER
from app.main import create_app
from app.downloads.archived import ArchivedWorkService
from app.review.commands import WorkCommands
from tests.integration.test_api_domains import csrf_token
from tests.integration.test_api_v1 import log_in
from tests.integration.test_archive_metadata import packed_fixture
from tests.integration.test_realtime import hello, call


def payload(client, key, **changes):
    editor = client.get('/api/v1/works/1').json()['metadata_editor']
    return {'work_id': editor['work_id'], 'expected_revision': editor['revision'],
            'idempotency_key': key, **changes}


def title(path):
    with zipfile.ZipFile(path) as archive:
        return ET.fromstring(archive.read('ComicInfo.xml')).findtext('Title')


def test_http_edit_replay_does_not_rewrite_file_or_override_a_later_edit(tmp_path, monkeypatch):
    settings, database, path, _ = packed_fixture(tmp_path)
    import app.downloads.archived as archived
    original, writes = archived.rewrite_comicinfo, []
    def record(*args):
        writes.append(args[0])
        return original(*args)
    monkeypatch.setattr(archived, 'rewrite_comicinfo', record)
    with TestClient(create_app(settings)) as client:
        log_in(client, settings)
        headers = {CSRF_HEADER: csrf_token(client)}
        a = payload(client, 'a', fields={'Title': 'First'})
        response = client.patch('/api/v1/works/1/metadata', headers=headers, json=a)
        assert response.status_code == 200, response.text
        assert response.json()['archive_synced'] is True
        first_result = response.json()
        b = payload(client, 'b', fields={'Title': 'Later'})
        assert client.patch('/api/v1/works/1/metadata', headers=headers, json=b).status_code == 200
        replay = client.patch('/api/v1/works/1/metadata', headers=headers, json=a)
        assert replay.status_code == 200 and replay.json()['metadata_sync_id'] == first_result['metadata_sync_id']
        assert replay.json()['revision'] == first_result['revision']
        assert title(path) == 'Later' and len(writes) == 2
        with database.connection() as connection:
            assert connection.execute('SELECT count(*) FROM work_command_receipts').fetchone()[0] == 2


def test_http_stale_and_unversioned_requests_leave_metadata_and_bytes_unchanged(tmp_path):
    settings, database, path, _ = packed_fixture(tmp_path)
    with TestClient(create_app(settings)) as client:
        log_in(client, settings)
        headers = {CSRF_HEADER: csrf_token(client)}
        old = payload(client, 'old', fields={'Title': 'Stale draft'})
        current = {**old, 'idempotency_key': 'current', 'fields': {'Title': 'Current'}}
        assert client.patch('/api/v1/works/1/metadata', headers=headers, json=current).status_code == 200
        before = path.read_bytes()
        conflict = client.patch('/api/v1/works/1/metadata', headers=headers, json=old)
        assert conflict.status_code == 409
        assert conflict.json()['error']['code'] == 'REVISION_CONFLICT'
        missing = client.patch('/api/v1/works/1/metadata', headers=headers, json={'fields': {'Title': 'No version'}})
        assert missing.status_code == 428
        assert path.read_bytes() == before and title(path) == 'Current'
        with database.connection() as connection:
            assert connection.execute('SELECT count(*) FROM work_command_receipts').fetchone()[0] == 1


def test_committed_metadata_sync_recovers_after_application_restart(tmp_path):
    settings, database, path, _ = packed_fixture(tmp_path)
    with TestClient(create_app(settings)) as client:
        log_in(client, settings)
        accepted = client.portal.call(WorkCommands(database).patch_metadata, 1,
            payload(client, 'interrupted', fields={'Title': 'Recovered'}), 'admin')
        assert title(path) == 'File title'
    with TestClient(create_app(settings)) as client:
        client.portal.call(client.app.state.archived_work_service.process_metadata_syncs)
        assert title(path) == 'Recovered'
        with database.connection() as connection:
            row = connection.execute('SELECT status, result_json FROM work_metadata_syncs WHERE sync_id = ?',
                                     (accepted['metadata_sync_id'],)).fetchone()
            assert row[0] == 'COMPLETED' and json.loads(row[1])['synced'] is True


def test_pending_metadata_sync_cursor_survives_restart_and_skips_failed_prefix(tmp_path):
    settings, database, _, service = packed_fixture(tmp_path)
    with database.connection() as connection:
        work_id = connection.execute(
            'SELECT work_id FROM work_versions WHERE candidate_id = 1'
        ).fetchone()[0]
        for index in range(101):
            connection.execute(
                'INSERT INTO work_metadata_syncs '
                '(sync_id, work_id, revision, fields_json, fetched_fields_json, operator_name, created_at) '
                "VALUES (?, ?, 0, '{}', '{}', 'admin', '2026-01-01 00:00:00')",
                (f'sync-{index:03d}', work_id),
            )

    calls: list[str] = []

    async def finish(sync_id):
        calls.append(sync_id)
        if sync_id != 'sync-100':
            raise RuntimeError('one item is unavailable')
        return {'synced': False, 'reason': 'WORK_CHANGED'}

    service.finish_metadata_sync = finish
    assert asyncio.run(service.process_metadata_syncs()) == 0
    assert calls == [f'sync-{index:03d}' for index in range(100)]

    restarted = ArchivedWorkService(database, settings.library_path, settings.work_path)
    restarted.finish_metadata_sync = finish
    assert asyncio.run(restarted.process_metadata_syncs()) == 1
    assert calls[100] == 'sync-100'


def test_late_sync_of_superseded_edit_never_replaces_newer_file_values(tmp_path):
    settings, database, path, service = packed_fixture(tmp_path)
    with TestClient(create_app(settings)) as client:
        log_in(client, settings)
        old = client.portal.call(WorkCommands(database).patch_metadata, 1,
            payload(client, 'delayed', fields={'Title': 'Old'}), 'admin')
        response = client.patch('/api/v1/works/1/metadata', headers={CSRF_HEADER: csrf_token(client)},
            json=payload(client, 'latest', fields={'Title': 'Latest'}))
        assert response.status_code == 200, response.text
        result = client.portal.call(service.finish_metadata_sync, old['metadata_sync_id'])
        assert result['reason'] == 'WORK_CHANGED'
        assert title(path) == 'Latest'


def test_html_conflict_preserves_typed_values_original_version_and_command_key(tmp_path):
    settings, database, path, _ = packed_fixture(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False) as client:
        log_in(client, settings)
        headers = {CSRF_HEADER: csrf_token(client)}
        old = payload(client, 'draft')
        fields = {'csrf_token': csrf_token(client), **old,
                  'field__Title': 'Unsaved <draft>', 'original__Title': 'File title',
                  'lock__Title': 'on', 'lock_original__Title': '0',
                  'new_field': 'Translator', 'new_value': 'Unsaved translator'}
        assert client.patch('/api/v1/works/1/metadata', headers=headers,
            json=payload(client, 'other', fields={'Title': 'Other device'})).status_code == 200
        response = client.post('/works/1/metadata', data=fields)
        assert response.status_code == 409, response.text
        class EditorInputs(HTMLParser):
            active = False

            def __init__(self):
                super().__init__()
                self.inputs, self.editor = {}, {}

            def handle_starttag(self, tag, attrs):
                attributes = dict(attrs)
                if tag == 'form':
                    self.active = attributes.get('id') == 'work-metadata-editor'
                    if self.active:
                        self.editor = attributes
                if tag == 'input' and self.active:
                    self.inputs[attributes['name']] = attributes

            def handle_endtag(self, tag):
                if tag == 'form':
                    self.active = False

        parsed = EditorInputs()
        parsed.feed(response.text)
        assert parsed.editor['data-dirty'] == 'true' and parsed.editor['data-revision'] == str(old['expected_revision'])
        for name, value in fields.items():
            if name != 'csrf_token' and name != 'lock__Title':
                assert parsed.inputs[name]['value'] == str(value)
        assert 'checked' in parsed.inputs['lock__Title']
        assert title(path) == 'Other device'
        assert client.post('/works/1/metadata', data=fields).status_code == 409


def test_legacy_html_conflict_retains_single_field_as_custom_draft(tmp_path):
    settings, database, path, _ = packed_fixture(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False) as client:
        log_in(client, settings)
        old = payload(client, 'legacy-draft')
        client.portal.call(WorkCommands(database).patch_metadata, 1,
            payload(client, 'other', fields={'Title': 'Other device'}), 'admin')
        response = client.post('/candidates/1/metadata', data={'csrf_token': csrf_token(client),
            **old, 'field_name': 'Title', 'field_value': 'Keep legacy draft'})
        assert response.status_code == 400
        assert response.context['metadata_draft']['new_value'] == 'Keep legacy draft'
        assert response.context['metadata_draft']['expected_revision'] == str(old['expected_revision'])


def test_edit_replay_does_not_offer_newer_snapshot_for_automatic_draft_reset(tmp_path):
    settings, database, _, _ = packed_fixture(tmp_path)
    with TestClient(create_app(settings)) as client:
        log_in(client, settings)
        headers = {CSRF_HEADER: csrf_token(client)}
        first = payload(client, 'first', fields={'Title': 'First'})
        accepted = client.patch('/api/v1/works/1/metadata', json=first, headers=headers)
        assert accepted.json()['metadata_editor'] is not None
        later = payload(client, 'later', fields={'Title': 'Later'})
        assert client.patch('/api/v1/works/1/metadata', json=later, headers=headers).status_code == 200
        replay = client.patch('/api/v1/works/1/metadata', json=first, headers=headers).json()
        assert replay['revision'] == accepted.json()['revision']
        assert replay['snapshot_changed'] and replay['metadata_editor'] is None


def test_http_and_ws_metadata_retries_share_receipt_and_refuse_stale_drafts(tmp_path):
    settings, database, path, _ = packed_fixture(tmp_path)
    with TestClient(create_app(settings)) as client:
        log_in(client, settings)
        headers = {CSRF_HEADER: csrf_token(client)}
        original = payload(client, 'cross-channel', fields={'Title': 'First'})
        accepted = client.patch('/api/v1/works/1/metadata', headers=headers, json=original).json()
        later = payload(client, 'later', fields={'Title': 'Later'})
        assert client.patch('/api/v1/works/1/metadata', headers=headers, json=later).status_code == 200
        with client.websocket_connect('/ws/ui', headers={'Origin': 'http://testserver'}) as ws:
            hello(client, ws)
            replay, _ = call(ws, 'api.patch_metadata', {'params': {'candidate_id': '1'}, 'body': original})
            assert replay['type'] == 'command.result', replay
            assert replay['data']['metadata_sync_id'] == accepted['metadata_sync_id']
            assert replay['data']['snapshot_changed'] and replay['data']['metadata_editor'] is None
            stale, _ = call(ws, 'api.patch_metadata', {'params': {'candidate_id': '1'},
                'body': {**original, 'idempotency_key': 'stale'}}, request_id='stale')
            assert stale['type'] == 'command.error' and stale['status'] == 409
        assert title(path) == 'Later'
