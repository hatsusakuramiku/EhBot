"""Legacy forms, shared HTTP/WS and batch requests use observed work versions."""
from html.parser import HTMLParser

from fastapi.testclient import TestClient

from app.main import create_app
from tests.integration.test_api_domains import seed, csrf_token
from tests.integration.test_api_v1 import log_in, make_settings
from tests.integration.test_realtime import hello, call
from tests.work_commands import observed_review, observed_review_batch


class Forms(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.forms = []
        self.current = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'form':
            self.current = {'action': attrs.get('action'), 'fields': {}}
            self.forms.append(self.current)
        elif tag == 'input' and self.current is not None and attrs.get('type') == 'hidden':
            self.current['fields'][attrs['name']] = attrs.get('value', '')

    def handle_endtag(self, tag):
        if tag == 'form':
            self.current = None


def test_old_review_urls_require_versions_and_web_conflict_retains_original_conditions(tmp_path):
    settings = make_settings(tmp_path)
    settings.data_path.mkdir(parents=True, exist_ok=True)
    seed(settings, [(1, 'PENDING_REVIEW', 'Review fixture')])
    with TestClient(create_app(settings), follow_redirects=False) as client:
        log_in(client, settings)
        csrf = csrf_token(client)
        headers = {'X-CSRF-Token': csrf}
        assert client.post('/api/v1/works/1/requeue', headers=headers, json={}).status_code == 428
        for action in ('approve', 'reject', 'requeue'):
            assert client.post('/candidates/1/' + action, data={'csrf_token': csrf}).status_code == 428
        first = observed_review(client)
        assert client.post('/api/v1/works/1/review', headers=headers,
                           json={**first, 'action': 'reject'}).status_code == 200
        client.post('/api/v1/works/1/requeue', headers=headers, json=observed_review(client))
        first['idempotency_key'] = 'other-client-stale-command'
        conflict = client.post('/candidates/1/reject', data={**first, 'csrf_token': csrf})
        assert conflict.status_code == 409, conflict.text
        fields = next(form['fields'] for form in Forms(conflict.text).forms if form['action'] == '/candidates/1/reject')
        assert fields['work_id'] == first['work_id']
        assert fields['expected_revision'] == str(first['expected_revision'])
        assert fields['idempotency_key'] == first['idempotency_key']
        assert client.get('/api/v1/works/1').json()['status']['code'] == 'PENDING_REVIEW'


def test_legacy_requeue_and_new_review_endpoint_share_one_receipt(tmp_path):
    settings = make_settings(tmp_path)
    settings.data_path.mkdir(parents=True, exist_ok=True)
    seed(settings, [(1, 'REJECTED', 'Review fixture')])
    with TestClient(create_app(settings)) as client:
        log_in(client, settings)
        headers = {'X-CSRF-Token': csrf_token(client)}
        first = observed_review(client)
        accepted = client.post('/api/v1/works/1/requeue', headers=headers, json=first)
        assert accepted.status_code == 200
        new = client.post('/api/v1/works/1/review', headers=headers, json={**first, 'action': 'requeue'})
        assert new.status_code == 200
        assert new.json() == {key: value for key, value in accepted.json().items() if key != 'status'}


def test_list_contains_snapshot_versions_and_plain_form_batch_and_quick_approve_use_them(tmp_path):
    settings = make_settings(tmp_path)
    settings.data_path.mkdir(parents=True, exist_ok=True)
    seed(settings, [(1, 'PENDING_REVIEW', 'Review fixture')])
    with TestClient(create_app(settings), follow_redirects=False) as client:
        log_in(client, settings)
        page = client.get('/candidates')
        fields = next(form['fields'] for form in Forms(page.text).forms if form['action'] == '/candidates/batch-review')
        snapshot = client.get('/api/v1/candidates?tab=pending').json()['items'][0]
        assert fields['work_id_1'] == snapshot['work_id']
        assert fields['expected_revision_1'] == str(snapshot['revision'])
        # There is no downloadable source. A valid quick approval reaches the
        # domain refusal, rather than failing the version contract.
        quick = client.post('/candidates/1/approve', data=fields)
        assert quick.status_code == 303 and 'PRECONDITION' not in quick.headers['location']
        response = client.post('/candidates/batch-review', data={**fields, 'candidate_ids': '1', 'action': 'reject'})
        assert response.status_code == 303
        assert client.get('/api/v1/works/1').json()['status']['code'] == 'REJECTED'
        replay = client.post('/candidates/batch-review', data={**fields, 'candidate_ids': '1', 'action': 'reject'})
        assert replay.headers['location'] == response.headers['location']


def test_batch_http_ws_and_form_retry_share_fixed_original_results(tmp_path):
    settings = make_settings(tmp_path)
    settings.data_path.mkdir(parents=True, exist_ok=True)
    seed(settings, [(1, 'PENDING_REVIEW', 'Review fixture')])
    with TestClient(create_app(settings)) as client:
        log_in(client, settings)
        csrf = csrf_token(client)
        headers = {'X-CSRF-Token': csrf}
        assert client.post('/api/v1/candidates/batch', headers=headers,
            json={'action': 'reject', 'candidate_ids': [1]}).status_code == 428
        first = {**observed_review_batch(client, [1]), 'action': 'reject', 'candidate_ids': [1]}
        accepted = client.post('/api/v1/candidates/batch', headers=headers, json=first)
        assert accepted.status_code == 200, accepted.text
        assert client.post('/api/v1/works/1/requeue', headers=headers, json=observed_review(client)).status_code == 200
        with client.websocket_connect('/ws/ui', headers={'Origin': 'http://testserver'}) as ws:
            hello(client, ws)
            replay, _ = call(ws, 'api.api_batch_review', {'body': first})
            assert replay['type'] == 'command.result', replay
            assert replay['data'] == accepted.json()
        fields = {'csrf_token': csrf, 'action': 'reject', 'candidate_ids': '1', 'idempotency_key': first['idempotency_key'],
                  **{name + '_1': value for name, value in first['versions']['1'].items()}}
        assert client.post('/candidates/batch-review', data=fields).status_code == 200
        assert client.get('/api/v1/works/1').json()['status']['code'] == 'PENDING_REVIEW'
