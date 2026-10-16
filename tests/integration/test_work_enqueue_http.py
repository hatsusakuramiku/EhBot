"""HTTP, Web forms and WS replay one task acceptance without starting it twice."""
from fastapi.testclient import TestClient

from app.main import create_app
from tests.integration.test_api_domains import seed, seed_downloaded, csrf_token
from tests.integration.test_api_v1 import log_in, make_settings
from tests.integration.test_realtime import hello, call
from tests.work_commands import observed_review
from tests.integration.test_work_review_http import Forms


def stop_workers(client):
    client.portal.call(client.app.state.download_service.stop)
    client.portal.call(client.app.state.conversion_service.stop)


def test_source_and_pack_json_and_all_legacy_forms_require_observed_versions(tmp_path):
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False) as client:
        stop_workers(client)
        seed(settings, [(1, 'APPROVED', 'Enqueue fixture')])
        log_in(client, settings)
        csrf = csrf_token(client)
        headers = {'X-CSRF-Token': csrf}
        for endpoint in ('/api/v1/works/1/convert', '/api/v1/works/1/sources/EXHENTAI'):
            assert client.post(endpoint, json={}, headers=headers).status_code == 428
        for suffix in ('download', 'telegram-user', 'exhentai-archive', 'telegraph', 'torrent', 'convert'):
            response = client.post('/candidates/1/' + suffix, data={'csrf_token': csrf})
            assert response.status_code == 428, (suffix, response.text)
        with client.app.state.database.connection() as connection:
            assert connection.execute('SELECT count(*) FROM download_jobs').fetchone() == (0,)


def test_source_replay_across_http_ws_and_old_form_preserves_cancelled_task(tmp_path):
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False) as client:
        stop_workers(client)
        seed(settings, [(1, 'APPROVED', 'Enqueue fixture')])
        log_in(client, settings)
        csrf = csrf_token(client)
        first = observed_review(client)
        accepted = client.post('/api/v1/works/1/sources/EXHENTAI', json=first, headers={'X-CSRF-Token': csrf})
        assert accepted.status_code == 200, accepted.text
        result = accepted.json()
        assert result['work_id'] == first['work_id'] and result['work_revision'] > first['expected_revision']
        assert result['revision'] == result['task_revision']
        with client.app.state.database.connection() as connection:
            connection.execute("UPDATE download_jobs SET state = 'CANCELLED' WHERE id = ?", (result['job_id'],))
        with client.websocket_connect('/ws/ui', headers={'Origin': 'http://testserver'}) as ws:
            hello(client, ws)
            replay, _ = call(ws, 'api.pull_work_source',
                {'params': {'candidate_id': '1', 'provider': 'EXHENTAI'}, 'body': first})
            assert replay['type'] == 'command.result' and replay['data'] == result, replay
        assert client.post('/candidates/1/exhentai-archive', data={**first, 'csrf_token': csrf}).status_code == 303
        with client.app.state.database.connection() as connection:
            assert connection.execute('SELECT state, task_id FROM download_jobs WHERE id = ?', (result['job_id'],)).fetchone() == ('CANCELLED', result['task_id'])
        stale = client.post('/candidates/1/exhentai-archive', data={**first, 'idempotency_key': 'other-device', 'csrf_token': csrf})
        assert stale.status_code == 409
        fields = next(form['fields'] for form in Forms(stale.text).forms if form['action'] == '/candidates/1/exhentai-archive')
        assert fields['expected_revision'] == str(first['expected_revision'])
        assert fields['idempotency_key'] == 'other-device'


def test_conversion_replay_across_http_ws_and_old_form_preserves_failed_task(tmp_path):
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False) as client:
        stop_workers(client)
        seed(settings, [(1, 'APPROVED', 'Enqueue fixture')])
        seed_downloaded(settings, 1)
        log_in(client, settings)
        csrf = csrf_token(client)
        first = observed_review(client)
        accepted = client.post('/api/v1/works/1/convert', json=first, headers={'X-CSRF-Token': csrf})
        assert accepted.status_code == 200, accepted.text
        result = accepted.json()
        with client.app.state.database.connection() as connection:
            connection.execute("UPDATE download_jobs SET state = 'CONVERSION_FAILED' WHERE id = ?", (result['job_id'],))
        with client.websocket_connect('/ws/ui', headers={'Origin': 'http://testserver'}) as ws:
            hello(client, ws)
            replay, _ = call(ws, 'api.convert_work', {'params': {'candidate_id': '1'}, 'body': first})
            assert replay['type'] == 'command.result' and replay['data'] == result, replay
        assert client.post('/candidates/1/convert', data={**first, 'csrf_token': csrf}).status_code == 303
        with client.app.state.database.connection() as connection:
            assert connection.execute('SELECT state, task_id FROM download_jobs WHERE id = ?', (result['job_id'],)).fetchone() == ('CONVERSION_FAILED', result['task_id'])
