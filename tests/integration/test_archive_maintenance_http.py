"""Shared JSON/WS/native maintenance and read-only preview coverage."""
from uuid import uuid4
import json

from tests.integration.test_v2_tasks import task_client
from tests.integration.test_api_domains import seed_downloaded
from tests.integration.test_realtime import hello, call
from tests.integration.test_work_review_http import Forms


def observed(client, scope, **values):
    snapshot = client.get('/api/v1/settings/paths').json()
    return {'expected_revision': snapshot['settings_versions'][scope], 'idempotency_key': uuid4().hex, **values}


def test_preview_and_rearchive_have_json_ws_and_native_fixed_result(task_client):
    client, headers, settings = task_client
    seed_downloaded(settings, 1)
    preview = client.post('/api/v1/settings/paths/template/preview', headers=headers,
                          json={'library_template': 'Books/{title}', 'title_source': 'english'})
    assert preview.status_code == 200, preview.text
    assert preview.json()['template_preview']['rendered'].startswith('Books/')
    assert client.post('/api/v1/settings/paths/template/preview', headers=headers,
        json={'library_template': '../unsafe'}).status_code == 400
    body = observed(client, 'rearchive', dry_run=True)
    response = client.post('/api/v1/settings/paths/rearchive', headers=headers, json=body)
    assert response.status_code == 200 and response.json()['dry_run'], response.text
    saved = client.get('/api/v1/settings/paths/rearchive', params={'idempotency_key': body['idempotency_key']})
    assert saved.status_code == 200 and saved.json()['result'] == response.json()
    assert saved.json()['status'] == 'COMPLETED' and saved.headers['cache-control'] == 'no-store'
    assert client.get('/api/v1/settings/paths/rearchive', params={'idempotency_key': 'missing'}).status_code == 404
    with client.websocket_connect('/ws/ui', headers={'Origin': 'http://testserver'}) as ws:
        hello(client, ws)
        replay, _ = call(ws, 'api.post_rearchive', {'body': body})
        assert replay['type'] == 'command.result' and replay['data'] == response.json()
        receipt, _ = call(ws, 'api.get_rearchive_receipt', {'query': [['idempotency_key', body['idempotency_key']]]}, request_id='receipt')
        assert receipt['type'] == 'command.result' and receipt['data'] == saved.json(), receipt
    form = {**body, 'dry_run': 'on', 'csrf_token': headers['X-CSRF-Token']}
    page = client.post('/archive-settings/paths/rearchive', data=form)
    assert page.status_code == 200 and '试跑结果' in page.text, page.text
    with client.app.state.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM download_jobs WHERE provider = 'CONVERSION'").fetchone() == (0,)
    run = client.post('/api/v1/settings/paths/rearchive', headers=headers, json=observed(client, 'rearchive'))
    assert run.status_code == 200 and len(run.json()['queued']) == 1, run.text


def test_rearchive_conflict_preserves_scope_key_and_switches_until_explicit_confirmation(task_client):
    client, headers, _ = task_client
    body = observed(client, 'rearchive')
    with client.app.state.database.connection() as connection:
        connection.execute("UPDATE candidates SET preview_url = 'https://telegra.ph/other' WHERE id = 1")
    page = client.post('/archive-settings/paths/rearchive', data={**body, 'force': 'on', 'dry_run': 'on',
                       'csrf_token': headers['X-CSRF-Token']})
    assert page.status_code == 409, page.text
    form = next(row['fields'] for row in Forms(page.text).forms if row['action'] == '/archive-settings/paths/rearchive')
    assert form['expected_revision'] == str(body['expected_revision'])
    assert form['idempotency_key'] == body['idempotency_key']
    assert 'name="force" value="on" type="checkbox" checked' in page.text
    assert 'name="dry_run" value="on" type="checkbox" checked' in page.text
    assert 'name="confirmed_command"' in page.text
    assert client.post('/api/v1/settings/paths/rearchive', headers=headers, json=body).status_code == 409
    assert client.post('/api/v1/settings/paths/rearchive', json=body).status_code == 403
    assert client.post('/api/v1/settings/paths/rearchive', headers=headers, json={}).status_code == 428
    confirmation = observed(client, 'rearchive')
    accepted = client.post('/archive-settings/paths/rearchive', data={**form, 'force': 'on', 'dry_run': 'on',
        'confirmed_command': json.dumps(confirmation), 'csrf_token': headers['X-CSRF-Token']})
    assert accepted.status_code == 200 and '试跑结果' in accepted.text, accepted.text
    stored = client.get('/api/v1/settings/paths/rearchive', params={'idempotency_key': confirmation['idempotency_key']})
    assert stored.json()['result']['force'] and stored.json()['result']['dry_run']


def test_cache_clear_fixed_count_and_replay_does_not_clear_later_answers(task_client):
    client, headers, _ = task_client
    def add_answer():
        with client.app.state.database.connection() as connection:
            connection.execute("INSERT INTO ai_path_suggestions(candidate_id, fingerprint, prompt_hash, relative_path, "
                               "directory, filename, model_name) VALUES(1, 'f', 'p', 'Book.cbz', '', 'Book', 'fixture')")
    add_answer()
    body = observed(client, 'path-cache')
    first = client.post('/api/v1/settings/paths/ai/cache/clear', headers=headers, json=body)
    assert first.status_code == 200 and first.json()['removed'] == 1, first.text
    add_answer()
    assert client.post('/api/v1/settings/paths/ai/cache/clear', headers=headers, json=body).json() == first.json()
    native = client.post('/archive-settings/paths/ai/cache/clear', data={**body, 'csrf_token': headers['X-CSRF-Token']})
    assert native.status_code == 200 and '已清除 1 条' in native.text
    with client.app.state.database.connection() as connection:
        assert connection.execute('SELECT count(*) FROM ai_path_suggestions').fetchone() == (1,)


def test_path_rule_trial_is_available_without_jinja_and_changes_no_rules(task_client):
    client, headers, _ = task_client
    response = client.post('/api/v1/settings/paths/rules/dry-run', headers=headers,
        json={'condition': {'kind': 'condition', 'field': 'Title', 'operator': 'LIKE', 'value': '%fixture%'}, 'case_sensitive': False})
    assert response.status_code == 200, response.text
    assert response.json()['dry_run']['matched'] == 1
    with client.app.state.database.connection() as connection:
        assert connection.execute('SELECT count(*) FROM archive_path_rules').fetchone() == (0,)
