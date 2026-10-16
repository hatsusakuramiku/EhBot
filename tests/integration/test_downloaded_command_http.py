"""Downloaded JSON, plain forms and realtime commands use observed work versions."""
from fastapi.testclient import TestClient

from app.main import create_app
from tests.integration.test_api_domains import seed, seed_downloaded, csrf_token
from tests.integration.test_api_v1 import log_in, make_settings
from tests.integration.test_realtime import hello, call
from tests.integration.test_work_enqueue_http import stop_workers
from tests.integration.test_work_review_http import Forms
from tests.work_commands import observed_review


def setup(client, settings):
    stop_workers(client)
    seed(settings, [(1, 'APPROVED', 'Downloaded fixture')])
    seed_downloaded(settings, 1)
    log_in(client, settings)
    return csrf_token(client)


def test_downloaded_http_ws_form_batch_share_original_task_after_later_failure(tmp_path):
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False) as client:
        csrf = setup(client, settings)
        observed = client.get('/api/v1/downloaded').json()['works'][0]
        payload = {'action': 'redownload', 'candidate_ids': [1], 'idempotency_key': 'downloaded-batch',
            'versions': {'1': {'work_id': observed['work_id'], 'expected_revision': observed['revision']}}}
        first = client.post('/api/v1/downloaded/batch', json=payload, headers={'X-CSRF-Token': csrf})
        assert first.status_code == 200 and len(first.json()['applied']) == 1, first.text
        task = first.json()['applied'][0]['result']
        with client.app.state.database.connection() as connection:
            connection.execute("UPDATE download_jobs SET state = 'FAILED' WHERE id = ?", (task['job_id'],))
        with client.websocket_connect('/ws/ui', headers={'Origin': 'http://testserver'}) as ws:
            hello(client, ws)
            replay, _ = call(ws, 'api.downloaded_batch', {'body': payload})
            assert replay['type'] == 'command.result' and replay['data'] == first.json()
        form = {'csrf_token': csrf, 'action': 'redownload', 'candidate_ids': '1',
            'idempotency_key': payload['idempotency_key'], **{
                key + '_1': value for key, value in payload['versions']['1'].items()}}
        assert client.post('/downloaded/batch', data=form).status_code == 303
        with client.app.state.database.connection() as connection:
            assert connection.execute('SELECT state, task_id FROM download_jobs WHERE id = ?',
                (task['job_id'],)).fetchone() == ('FAILED', task['task_id'])


def test_old_batch_and_row_repack_require_versions_and_conflict_preserves_form(tmp_path):
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False) as client:
        csrf = setup(client, settings)
        assert client.post('/api/v1/downloaded/batch', json={'action': 'remove', 'candidate_ids': [1]},
            headers={'X-CSRF-Token': csrf}).status_code == 428
        assert client.post('/downloaded/1/repack', data={'csrf_token': csrf}).status_code == 428
        assert client.post('/downloaded/batch', data={'csrf_token': csrf,
            'action': 'remove', 'candidate_ids': '1'}).status_code == 428
        fields = next(item['fields'] for item in Forms(client.get('/downloaded').text).forms
                      if item['action'] == '/downloaded/batch')
        with client.app.state.database.connection() as connection:
            client.app.state.database.write_archive_path_pin(connection, 1, 'Other.cbz', True, 'other-device')
        response = client.post('/downloaded/1/repack', data=fields)
        assert response.status_code == 409, response.text
        retained = next(item['fields'] for item in Forms(response.text).forms
                        if item['action'] == '/downloaded/batch')
        for key in ('work_id_1', 'expected_revision_1', 'idempotency_key_1'):
            assert retained[key] == fields[key]
        response = client.post('/downloaded/batch', data={**fields, 'action': 'remove', 'candidate_ids': '1'})
        assert response.status_code == 409
        retained = next(item['fields'] for item in Forms(response.text).forms
                        if item['action'] == '/downloaded/batch')
        assert retained['idempotency_key'] == fields['idempotency_key']


def test_downloaded_removal_replay_has_original_acceptance_and_readable_cleanup(tmp_path):
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        csrf = setup(client, settings)
        snapshot = client.get('/api/v1/downloaded').json()['works'][0]
        payload = {'action': 'remove', 'candidate_ids': [1], 'idempotency_key': 'remove-batch',
            'versions': {'1': {'work_id': snapshot['work_id'], 'expected_revision': snapshot['revision']}}}
        first = client.post('/api/v1/downloaded/batch', json=payload, headers={'X-CSRF-Token': csrf})
        assert first.status_code == 200 and first.json()['skipped'] == [], first.text
        removal = first.json()['applied'][0]['result']
        client.portal.call(client.app.state.archived_work_service.process_removals)
        assert client.get('/api/v1/removals/' + removal['removal_id']).json()['status'] == 'COMPLETED'
        assert client.post('/api/v1/downloaded/batch', json=payload,
            headers={'X-CSRF-Token': csrf}).json() == first.json()
        assert client.get('/api/v1/works/1').status_code == 200


def test_rename_http_ws_and_plain_form_share_fixed_move_result(tmp_path):
    from tests.integration.test_downloaded_web import seeded, authenticate
    settings, library, ids = seeded(tmp_path)
    with TestClient(create_app(settings)) as client:
        authenticate(client, settings)
        stop_workers(client)
        candidate_id = ids['packed']
        observed = {**observed_review(client, candidate_id), 'filename': 'Shared', 'directory': 'Folder', 'repack': False}
        csrf = csrf_token(client)
        path = f'/api/v1/downloaded/{candidate_id}/rename'
        assert client.post(path, json={'filename': 'MissingVersion'}, headers={'X-CSRF-Token': csrf}).status_code == 428
        first = client.post(path, json=observed, headers={'X-CSRF-Token': csrf})
        assert first.status_code == 200 and first.json()['relative_path'] == 'Folder/Shared.cbz', first.text
        with client.websocket_connect('/ws/ui', headers={'Origin': 'http://testserver'}) as ws:
            ready = hello(client, ws)
            name = next(name for name, item in ready['commands'].items()
                        if item['path'] == '/api/v1/downloaded/{candidate_id}/rename')
            replay, _ = call(ws, name, {'params': {'candidate_id': candidate_id}, 'body': observed})
            assert replay['type'] == 'command.result' and replay['data'] == first.json()
        response = client.post(f'/downloaded/{candidate_id}/rename', data={
            **{key: value for key, value in observed.items() if key != 'repack'}, 'csrf_token': csrf}, follow_redirects=False)
        assert response.status_code == 303
        assert (settings.library_path / 'Folder' / 'Shared.cbz').exists()


def test_plain_rename_conflict_keeps_name_directory_version_and_key(tmp_path):
    from tests.integration.test_downloaded_web import seeded, authenticate
    settings, library, ids = seeded(tmp_path)
    with TestClient(create_app(settings)) as client:
        authenticate(client, settings)
        stop_workers(client)
        candidate_id = ids['packed']
        observed = observed_review(client, candidate_id)
        with client.app.state.database.connection() as connection:
            connection.execute("UPDATE candidates SET status = 'REJECTED' WHERE id = ?", (candidate_id,))
        response = client.post(f'/downloaded/{candidate_id}/rename', data={**observed, 'filename': 'Local draft',
            'directory': 'My/Folder', 'repack': 'on', 'csrf_token': csrf_token(client)}, follow_redirects=False)
        assert response.status_code == 409, response.text
        form = next(item for item in Forms(response.text).forms if item['action'] == f'/downloaded/{candidate_id}/rename')
        for key, value in observed.items():
            assert form['fields'][key] == str(value)
        assert 'value="Local draft"' in response.text and 'value="My/Folder"' in response.text
        assert 'data-rename-drawer hidden' not in response.text


def test_exact_archive_path_api_and_old_detail_form_share_result_and_keep_conflicts(tmp_path):
    from tests.integration.test_downloaded_web import seeded, authenticate
    settings, library, ids = seeded(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False) as client:
        authenticate(client, settings)
        stop_workers(client)
        candidate_id = ids['unpacked']
        observed = {**observed_review(client, candidate_id), 'filename': 'First', 'directory': 'Exact', 'repack': False}
        csrf = csrf_token(client)
        api = f'/api/v1/works/{candidate_id}/archive-path'
        web = f'/works/{candidate_id}/archive-path'
        assert client.post(api, json={}, headers={'X-CSRF-Token': csrf}).status_code == 428
        first = client.post(api, json=observed, headers={'X-CSRF-Token': csrf})
        assert first.status_code == 200 and not first.json()['moved'], first.text
        response = client.post(web, data={**{key: value for key, value in observed.items() if key != 'repack'},
                                         'csrf_token': csrf})
        assert response.status_code == 303
        stale = {**observed, 'idempotency_key': 'new-stale-command', 'directory': 'Local directory', 'repack': 'on'}
        response = client.post(web, data={**stale, 'csrf_token': csrf})
        assert response.status_code == 409
        form = next(item for item in Forms(response.text).forms if item['action'] == web)
        assert form['fields']['expected_revision'] == str(observed['expected_revision'])
        assert form['fields']['idempotency_key'] == stale['idempotency_key']
        assert 'value="Local directory"' in response.text


def test_dedup_http_ws_plain_form_replay_and_old_plan_conflict(tmp_path):
    from tests.integration.test_downloaded_web import seeded, authenticate, _duplicate_pair
    settings, library, ids = seeded(tmp_path)
    keeper, loser = _duplicate_pair(library)
    with TestClient(create_app(settings), follow_redirects=False) as client:
        authenticate(client, settings)
        stop_workers(client)
        csrf = csrf_token(client)
        headers = {'X-CSRF-Token': csrf}
        assert client.post('/api/v1/downloaded/dedup', json={}, headers=headers).status_code == 428
        preview = client.get('/api/v1/downloaded/dedup').json()
        params = {'plan_id': preview['plan_id'], 'idempotency_key': 'dedup-cross-channel'}
        first = client.post('/api/v1/downloaded/dedup', json=params, headers=headers)
        assert first.status_code == 200 and first.json()['skipped'] == [], first.text
        client.portal.call(client.app.state.archived_work_service.process_removals)
        with client.websocket_connect('/ws/ui', headers={'Origin': 'http://testserver'}) as ws:
            ready = hello(client, ws)
            name = next(name for name, item in ready['commands'].items()
                        if item['method'] == 'POST' and item['path'] == '/api/v1/downloaded/dedup')
            replay, _ = call(ws, name, {'body': params})
            assert replay['type'] == 'command.result' and replay['data'] == first.json()
        assert client.post('/downloaded/dedup', data={**params, 'csrf_token': csrf}).status_code == 303
        # A new damaged-library duplicate has its own preview, never appended to the old plan.
        later = library.add(title='Later', ex_gid=77001)
        page = client.get('/downloaded/dedup')
        form = next(item['fields'] for item in Forms(page.text).forms if item['action'] == '/downloaded/dedup')
        with client.app.state.database.connection() as connection:
            connection.execute("UPDATE candidates SET status = 'REJECTED' WHERE id = ?", (keeper,))
        conflict = client.post('/downloaded/dedup', data=form)
        assert conflict.status_code == 409, conflict.text
        retained = next(item['fields'] for item in Forms(conflict.text).forms if item['action'] == '/downloaded/dedup')
        assert retained['plan_id'] == form['plan_id'] and retained['idempotency_key'] == form['idempotency_key']
        assert client.get(f'/api/v1/works/{later}').status_code == 200
