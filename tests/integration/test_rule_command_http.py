"""Public rule commands share receipts through HTTP, WS and plain forms."""
from html import unescape
import re

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.integration.test_manual_add import _settings, _authenticate
from tests.integration.test_realtime import hello, call
from tests.integration.test_rule_commands import creation, observed
from tests.integration.test_settings_command_http import fields
from tests.integration.test_work_enqueue_http import stop_workers
from tests.integration.test_work_review_http import Forms


def paths(domain):
    if domain == 'paths':
        return '/api/v1/settings/paths/rules', '/archive-settings/paths/rules', 'path_rule_id'
    return '/api/v1/settings/auto-approval/rules', '/auto-approval-rules', 'rule_id'


def saved_form(command, csrf, id_field):
    return {'csrf_token': csrf, **{key: value for key, value in command.items() if key != 'condition'},
            'enabled': 'on', 'condition_field': 'Title', 'condition_operator': 'LIKE',
            'condition_value': '%example%', 'group_operator': 'AND'}


@pytest.mark.parametrize('domain', ['auto-approval', 'paths'])
def test_http_ws_form_replay_and_missing_preconditions(tmp_path, domain):
    settings = _settings(tmp_path)
    api, web, id_field = paths(domain)
    with TestClient(create_app(settings), follow_redirects=False) as client:
        _authenticate(client, settings)
        stop_workers(client)
        csrf = fields(client, domain, web)['csrf_token']
        headers = {'X-CSRF-Token': csrf}
        assert client.post(api, json={'name': 'missing'}, headers=headers).status_code == 428
        command = creation(domain)
        first = client.post(api, json=command, headers=headers)
        assert first.status_code == 200, first.text
        row = first.json()
        with client.websocket_connect('/ws/ui', headers={'Origin': 'http://testserver'}) as ws:
            ready = hello(client, ws)
            name = next(name for name, entry in ready['commands'].items() if entry['path'] == api and entry['method'] == 'POST')
            replay, _ = call(ws, name, {'body': command})
            assert replay['type'] == 'command.result' and replay['data'] == row
        assert client.post(web, data={**saved_form(command, csrf, id_field), 'rule_uuid': ''}).status_code == 303
        missing = client.request('DELETE', api + '/' + str(row['rule_id']), headers=headers, json={})
        assert missing.status_code == 428
        assert len(client.get('/api/v1/settings/' + domain).json()['rules' if domain == 'auto-approval' else 'path_rules']) == 1


@pytest.mark.parametrize('domain', ['auto-approval', 'paths'])
def test_conflict_preserves_editor_version_conditions_and_explicit_confirmation(tmp_path, domain):
    settings = _settings(tmp_path)
    api, web, id_field = paths(domain)
    with TestClient(create_app(settings), follow_redirects=False) as client:
        _authenticate(client, settings)
        stop_workers(client)
        csrf = fields(client, domain, web)['csrf_token']
        headers = {'X-CSRF-Token': csrf}
        result = client.post(api, json=creation(domain), headers=headers).json()
        page = client.get(web + '/' + str(result['rule_id']) + '/edit')
        assert page.status_code == 200, page.text
        original = next(item['fields'] for item in Forms(page.text).forms if item['action'].endswith(web))
        assert original['rule_uuid'] == result['rule_uuid']
        toggle = client.put(api + '/' + str(result['rule_id']) + '/toggle', headers=headers,
                           json=observed(result, 'winner', enabled=False))
        assert toggle.status_code == 200, toggle.text
        data = saved_form({**original, 'name': 'my draft', 'path_template': 'Draft/{title}'}, csrf, id_field)
        conflict = client.post(web, data=data)
        assert conflict.status_code == 409, conflict.text
        assert 'data-dirty="true"' in conflict.text and 'value="my draft"' in conflict.text
        retained = next(item['fields'] for item in Forms(conflict.text).forms if item['action'].endswith(web))
        assert retained['expected_revision'] == original['expected_revision']
        assert retained['idempotency_key'] == original['idempotency_key']
        confirmation = unescape(re.search(r'name="confirmed_command" value="([^"]+)"', conflict.text)[1])
        data['confirmed_command'] = confirmation
        for _ in range(2):
            response = client.post(web, data=data)
            assert response.status_code == 303, response.text
        row = client.get('/api/v1/settings/' + domain).json()['rules' if domain == 'auto-approval' else 'path_rules'][0]
        assert row['name'] == 'my draft'


@pytest.mark.parametrize('domain', ['auto-approval', 'paths'])
def test_dry_run_retains_unsaved_draft_and_original_command(tmp_path, domain):
    settings = _settings(tmp_path)
    api, web, id_field = paths(domain)
    with TestClient(create_app(settings), follow_redirects=False) as client:
        _authenticate(client, settings)
        stop_workers(client)
        csrf = fields(client, domain, web)['csrf_token']
        command = creation(domain, name='trial only')
        page = client.post(web + '/dry-run', data=saved_form(command, csrf, id_field))
        assert page.status_code == 200, page.text
        retained = next(item['fields'] for item in Forms(page.text).forms if item['action'].endswith(web))
        assert retained['expected_revision'] == '0' and retained['idempotency_key'] == command['idempotency_key']
        assert 'value="trial only"' in page.text and 'value="%example%"' in page.text
        assert client.get('/api/v1/settings/' + domain).json()['rules' if domain == 'auto-approval' else 'path_rules'] == []
