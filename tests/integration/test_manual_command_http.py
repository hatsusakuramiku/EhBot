"""Manual admission shares durable results across JSON, WS and plain forms."""
from fastapi.testclient import TestClient

from app.main import create_app
from tests.integration.test_manual_add import _settings, _authenticate, _configure_exhentai, _stub_gallery_fetch
from tests.integration.test_realtime import hello, call
from tests.integration.test_work_enqueue_http import stop_workers
from tests.integration.test_work_review_http import Forms


URL = 'https://exhentai.org/g/987654/appletoken99/'


def test_manual_replay_crosses_channels_even_after_account_unavailable(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False) as client:
        _authenticate(client, settings)
        stop_workers(client)
        _configure_exhentai(client, monkeypatch)
        calls = _stub_gallery_fetch(client, monkeypatch, metadata={'Title': 'Shared manual'})
        page = client.get('/manual-add')
        form = next(item['fields'] for item in Forms(page.text).forms if item['action'].endswith('/manual-add'))
        csrf = form['csrf_token']
        headers = {'X-CSRF-Token': csrf}
        assert client.post('/api/v1/candidates/manual', json={'url': URL}, headers=headers).status_code == 428
        params = {'url': URL, 'idempotency_key': form['idempotency_key']}
        first = client.post('/api/v1/candidates/manual', json=params, headers=headers)
        assert first.status_code == 200 and first.json()['status'] == 'COMPLETED', first.text
        operation_id = first.json()['manual_add_operation_id']
        assert client.get('/api/v1/manual-add-operations/' + operation_id).json() == first.json()
        from types import SimpleNamespace
        monkeypatch.setattr(client.app.state.connection_manager, 'snapshot',
            lambda: SimpleNamespace(exhentai=SimpleNamespace(state='disconnected')))
        with client.websocket_connect('/ws/ui', headers={'Origin': 'http://testserver'}) as ws:
            ready = hello(client, ws)
            name = next(name for name, entry in ready['commands'].items()
                        if entry['path'] == '/api/v1/candidates/manual')
            replay, _ = call(ws, name, {'body': params})
            assert replay['type'] == 'command.result' and replay['data'] == first.json()
        response = client.post('/manual-add', data={**form, 'input': URL})
        assert response.status_code == 303
        assert len(calls) == 1
        assert client.get('/manual-add/operations/' + operation_id).status_code == 303

        # Purge through the real service, then deliberately reuse its numeric ID.
        database = client.app.state.database
        archived = client.app.state.archived_work_service
        candidate_id = first.json()['candidate_id']
        from functools import partial
        client.portal.call(partial(archived.purge_work, candidate_id, delete_files=True, operator_name='admin'))
        client.portal.call(archived.process_removals)
        with database.connection() as connection:
            assert connection.execute('SELECT 1 FROM candidates WHERE id = ?', (candidate_id,)).fetchone() is None
            connection.execute("INSERT INTO candidates(id, status) VALUES(?, 'PENDING_REVIEW')", (candidate_id,))
        assert client.get('/api/v1/manual-add-operations/' + operation_id).json() == first.json()
        for response in (client.post('/manual-add', data={**form, 'input': URL}),
                         client.get('/manual-add/operations/' + operation_id)):
            assert response.status_code == 410 and '原作品已被删除' in response.text
            assert 'location' not in response.headers
        assert len(calls) == 1


def test_manual_failure_replays_original_error_and_preserves_link_and_key(tmp_path, monkeypatch):
    from app.exhentai.service import ExHentaiDownloadError
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False) as client:
        _authenticate(client, settings)
        stop_workers(client)
        _configure_exhentai(client, monkeypatch)
        _stub_gallery_fetch(client, monkeypatch, error=ExHentaiDownloadError('FIXTURE_REFUSAL', 'Fixture refusal'))
        page = client.get('/manual-add')
        form = next(item['fields'] for item in Forms(page.text).forms if item['action'].endswith('/manual-add'))
        response = client.post('/manual-add', data={**form, 'input': URL})
        assert response.status_code == 400 and URL in response.text
        assert 'data-dirty="true"' in response.text
        retained = next(item['fields'] for item in Forms(response.text).forms if item['action'].endswith('/manual-add'))
        assert retained['idempotency_key'] == form['idempotency_key']
        first = client.post('/api/v1/candidates/manual', json={'url': URL, 'idempotency_key': form['idempotency_key']},
                            headers={'X-CSRF-Token': form['csrf_token']})
        assert first.status_code == 400 and first.json()['error']['code'] == 'FIXTURE_REFUSAL'
        operation = first.json()['error']['details']['manual_add_operation_id']
        state = client.get('/api/v1/manual-add-operations/' + operation)
        assert state.status_code == 200 and state.headers['cache-control'] == 'no-store'
        assert state.json()['status'] == 'FAILED'
        assert client.post('/manual-add', data={'csrf_token': form['csrf_token'], 'input': URL}).status_code == 428


def test_duplicate_receipt_and_rendered_link_keep_original_work_identity(tmp_path, monkeypatch):
    from functools import partial
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False) as client:
        _authenticate(client, settings)
        stop_workers(client)
        _configure_exhentai(client, monkeypatch)
        calls = _stub_gallery_fetch(client, monkeypatch, metadata={'Title': 'Original duplicate'})
        monkeypatch.setattr(client.app.state.connection_manager, 'user_download_available', lambda: False)
        form = next(item['fields'] for item in Forms(client.get('/manual-add').text).forms
                    if item['action'].endswith('/manual-add'))
        headers = {'X-CSRF-Token': form['csrf_token']}
        api = '/api/v1/candidates/manual'
        first = client.post(api, json={'url': URL, 'idempotency_key': 'first'}, headers=headers).json()
        payload = {'url': URL, 'idempotency_key': form['idempotency_key']}
        duplicate = client.post(api, json=payload, headers=headers)
        assert duplicate.status_code == 409
        details = duplicate.json()['error']['details']
        assert details['existing_work_id'] == first['work_id']
        link = f"/works/{first['candidate_id']}?work_id={first['work_id']}"
        response = client.post('/manual-add', data={**form, 'input': URL})
        assert response.status_code == 409 and link in response.text
        assert 'Original duplicate' in response.text
        assert client.get(link).status_code == 200
        operation_url = '/manual-add/operations/' + details['manual_add_operation_id']
        assert link in client.get(operation_url).text

        archived = client.app.state.archived_work_service
        client.portal.call(partial(archived.purge_work, first['candidate_id'], delete_files=True, operator_name='admin'))
        client.portal.call(archived.process_removals)
        with client.app.state.database.connection() as connection:
            connection.execute("INSERT INTO candidates(id, status, ex_gid) VALUES(?, 'PENDING_REVIEW', 321)",
                               (first['candidate_id'],))
        assert client.post(api, json=payload, headers=headers).json() == duplicate.json()
        for response in (client.post('/manual-add', data={**form, 'input': URL}), client.get(operation_url)):
            assert '原作品已被删除' in response.text
            assert link not in response.text and '跳转到该候选' not in response.text
            assert '#321' not in response.text
        assert client.get(link).status_code == 410
        assert len(calls) == 1
