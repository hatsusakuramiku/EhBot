"""HTTP, WS and legacy deletion forms share the versioned removal command."""
from fastapi.testclient import TestClient

from app.main import create_app
from tests.integration.test_api_domains import csrf_token, seed
from tests.integration.test_api_v1 import log_in, make_settings
from tests.integration.test_realtime import hello, call
from tests.integration.test_work_review_http import Forms
from tests.work_commands import observed_review, observed_review_batch


def test_missing_version_refused_and_stale_delete_form_preserves_original_mode_and_conditions(tmp_path):
    settings = make_settings(tmp_path)
    settings.data_path.mkdir(parents=True, exist_ok=True)
    seed(settings, [(1, 'PENDING_REVIEW', 'Removal fixture')])
    with TestClient(create_app(settings), follow_redirects=False) as client:
        log_in(client, settings)
        csrf = csrf_token(client)
        headers = {'X-CSRF-Token': csrf}
        assert client.post('/candidates/1/delete', data={'csrf_token': csrf}).status_code == 428
        assert client.post('/api/v1/works/1/delete', headers=headers, json={}).status_code == 428
        forms = Forms(client.get('/works/1').text).forms
        payload = next(item['fields'] for item in forms
                       if item['action'] == '/candidates/1/delete' and item['fields']['mode'] == 'delete-files')
        client.post('/api/v1/works/1/review', headers=headers,
                    json={**observed_review(client), 'action': 'reject'})
        conflict = client.post('/candidates/1/delete', data=payload)
        assert conflict.status_code == 409
        retry = next(item['fields'] for item in Forms(conflict.text).forms
                     if item['action'] == '/candidates/1/delete' and item['fields']['mode'] == 'delete-files')
        for key in ('work_id', 'expected_revision', 'idempotency_key', 'mode'):
            assert retry[key] == payload[key]
        assert client.get('/works/1').status_code == 200


def test_delete_http_ws_and_form_replay_return_original_acceptance_after_candidate_gone(tmp_path):
    settings = make_settings(tmp_path)
    settings.data_path.mkdir(parents=True, exist_ok=True)
    seed(settings, [(1, 'PENDING_REVIEW', 'Removal fixture')])
    with TestClient(create_app(settings), follow_redirects=False) as client:
        log_in(client, settings)
        csrf = csrf_token(client)
        payload = observed_review(client)
        accepted = client.post('/api/v1/works/1/delete', headers={'X-CSRF-Token': csrf}, json=payload)
        assert accepted.status_code == 202, accepted.text
        assert client.get('/api/v1/works/1').status_code == 404
        with client.websocket_connect('/ws/ui', headers={'Origin': 'http://testserver'}) as ws:
            hello(client, ws)
            replay, _ = call(ws, 'api.delete_work', {'params': {'candidate_id': '1'}, 'body': payload})
            assert replay['type'] == 'command.result' and replay['data'] == accepted.json()
        assert client.post('/candidates/1/delete', data={**payload, 'csrf_token': csrf}).status_code == 303
        assert client.post('/api/v1/works/1/delete', headers={'X-CSRF-Token': csrf}, json=payload).json() == accepted.json()


def test_legacy_batch_requires_observed_versions_and_keeps_original_outcomes(tmp_path):
    settings = make_settings(tmp_path)
    settings.data_path.mkdir(parents=True, exist_ok=True)
    seed(settings, [(1, 'PENDING_REVIEW', 'Removal fixture')])
    with TestClient(create_app(settings)) as client:
        log_in(client, settings)
        csrf = csrf_token(client)
        headers = {'X-CSRF-Token': csrf}
        assert client.post('/api/v1/candidates/batch', headers=headers,
            json={'action': 'delete', 'candidate_ids': [1]}).status_code == 428
        payload = {**observed_review_batch(client, [1]), 'action': 'delete', 'candidate_ids': [1]}
        first = client.post('/api/v1/candidates/batch', headers=headers, json=payload)
        assert first.status_code == 200, first.text
        assert len(first.json()['applied']) == 1
        assert client.post('/api/v1/candidates/batch', headers=headers, json=payload).json() == first.json()


def test_batch_conflict_preserves_selection_versions_and_key_in_empty_original_tab(tmp_path):
    settings = make_settings(tmp_path)
    settings.data_path.mkdir(parents=True, exist_ok=True)
    seed(settings, [(1, 'PENDING_REVIEW', 'Removal fixture')])
    with TestClient(create_app(settings), follow_redirects=False) as client:
        log_in(client, settings)
        csrf = csrf_token(client)
        headers = {'X-CSRF-Token': csrf}
        fields = next(item['fields'] for item in Forms(client.get('/candidates').text).forms
                      if item['action'] == '/candidates/batch-review')
        client.post('/api/v1/works/1/review', headers=headers,
                    json={**observed_review(client), 'action': 'reject'})
        response = client.post('/candidates/batch-review', data={
            **fields, 'candidate_ids': '1', 'action': 'delete', 'tab': 'pending'})
        assert response.status_code == 409, response.text
        retained = next(item['fields'] for item in Forms(response.text).forms
                        if item['action'] == '/candidates/batch-review')
        for key in ('work_id_1', 'expected_revision_1', 'idempotency_key'):
            assert retained[key] == fields[key]
        assert 'name="candidate_ids" value="1" checked' in response.text
        assert client.get('/api/v1/works/1').status_code == 200
