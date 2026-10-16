"""JSON, WS and native source corrections use the same version and receipt."""
from html import unescape
import json
import re

from fastapi.testclient import TestClient
import pytest

from app.main import create_app
from tests.integration.test_api_domains import seed, csrf_token
from tests.integration.test_api_v1 import log_in, make_settings
from tests.integration.test_realtime import hello, call
from tests.integration.test_work_enqueue_http import stop_workers
from tests.integration.test_work_review_http import Forms
from tests.integration.test_work_source_commands import CASES
from tests.work_commands import observed_review


class SourceForms(Forms):
    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        attrs = dict(attrs)
        if tag == 'input' and self.current is not None and attrs.get('name'):
            self.current['fields'][attrs['name']] = attrs.get('value', '')


@pytest.mark.parametrize('action,values', CASES)
def test_public_preconditions_cross_transport_replay_and_native_conflict(tmp_path, action, values):
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False) as client:
        stop_workers(client)
        seed(settings, [(1, 'PENDING_REVIEW', 'Source fixture')])
        log_in(client, settings)
        headers = {'X-CSRF-Token': csrf_token(client)}
        path = '/api/v1/works/1/' + action
        native = '/works/1/' + action
        assert client.put(path, json=values, headers=headers).status_code == 428
        assert client.post(native, data={**values, 'csrf_token': headers['X-CSRF-Token']}).status_code == 428
        page = client.get('/works/1')
        form = next(row['fields'] for row in SourceForms(page.text).forms if row['action'] == native)
        body = {**observed_review(client), **values}
        assert form['work_id'] == body['work_id'] and form['expected_revision'] == str(body['expected_revision'])
        first = client.put(path, json=body, headers=headers)
        assert first.status_code == 200, first.text
        assert first.headers['cache-control'] == 'no-store'
        with client.websocket_connect('/ws/ui', headers={'Origin': 'http://testserver'}) as ws:
            ready = hello(client, ws)
            name = next(name for name, row in ready['commands'].items() if row['path'] == '/api/v1/works/{candidate_id}/' + action)
            replay, _ = call(ws, name, {'params': {'candidate_id': '1'}, 'body': body})
            assert replay['type'] == 'command.result' and replay['data'] == first.json(), replay
        assert client.post(native, data={**body, 'csrf_token': headers['X-CSRF-Token']}).status_code == 303
        draft = {**body, 'idempotency_key': 'other-device'}
        rejected = client.post(native, data={**draft, 'csrf_token': headers['X-CSRF-Token']})
        assert rejected.status_code == 409
        retained = next(row['fields'] for row in SourceForms(rejected.text).forms if row['action'] == native)
        assert retained['expected_revision'] == str(body['expected_revision'])
        assert retained['idempotency_key'] == draft['idempotency_key'] and retained[next(iter(values))] == next(iter(values.values()))
        confirmation = unescape(re.search(r'name="confirmed_command"\s+value="([^"]+)"', rejected.text)[1])
        assert json.loads(confirmation)['idempotency_key'] != draft['idempotency_key']
        assert client.post(native, data={**draft, 'csrf_token': headers['X-CSRF-Token'],
                                        'confirmed_command': confirmation}).status_code == 303


def test_merge_target_confirmation_and_original_result_link_guard(tmp_path):
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False) as client:
        stop_workers(client)
        seed(settings, [(1, 'PENDING_REVIEW', 'Source'), (2, 'PENDING_REVIEW', 'Target')])
        log_in(client, settings)
        with client.app.state.database.connection() as connection:
            connection.execute('UPDATE candidates SET ex_gid = 123 WHERE id = 2')
        body = {**observed_review(client), **CASES[2][1], 'csrf_token': csrf_token(client)}
        response = client.post('/works/1/eh-ref', data=body)
        assert response.status_code == 409
        confirmation = unescape(re.search(r'name="confirmed_command"\s+value="([^"]+)"', response.text)[1])
        accepted = client.post('/works/1/eh-ref', data={**body, 'confirmed_command': confirmation})
        assert accepted.status_code == 303 and '/works/2?work_id=' in accepted.headers['location']
        with client.app.state.database.connection() as connection:
            connection.execute('DELETE FROM review_actions WHERE candidate_id = 2')
            connection.execute('DELETE FROM metadata_values WHERE candidate_id = 2')
            connection.execute('DELETE FROM candidates WHERE id = 2')
            connection.execute("INSERT INTO candidates(id, status) VALUES(2, 'PENDING_REVIEW')")
        replay = client.post('/works/1/eh-ref', data={**body, 'confirmed_command': confirmation})
        assert replay.status_code == 303 and replay.headers['location'] == accepted.headers['location']
        assert client.get(replay.headers['location']).status_code == 410
