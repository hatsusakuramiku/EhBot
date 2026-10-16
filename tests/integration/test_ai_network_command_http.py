"""Public AI probes share durable admission and reopen results without network."""
from functools import partial
from html import unescape
import re
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.ai_commands import observed_network, observed_catalogue
from tests.integration.test_ai_catalogue_command_http import seed
from tests.integration.test_manual_add import _settings, _authenticate
from tests.integration.test_realtime import hello, call
from tests.integration.test_toolchain_commands import settled
from tests.integration.test_work_enqueue_http import stop_workers
from tests.integration.test_work_review_http import Forms


@pytest.fixture
def network(tmp_path):
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.path.endswith('/models'):
            return httpx.Response(200, json={'data': [{'id': 'discovered-fixture'}, {'id': 'second-discovered'}]})
        return httpx.Response(200, json={'choices': [{'message': {'content': '{"ok":true}'}}]})
    settings = _settings(tmp_path)
    with TestClient(create_app(settings, ai_transport=httpx.MockTransport(handler)), follow_redirects=False) as client:
        _authenticate(client, settings)
        stop_workers(client)
        headers, provider, key, model = seed(client)
        client.portal.call(client.app.state.database.delete_ai_provider_key, key)
        client.portal.call(partial(client.app.state.ai_service.add_key, provider, api_key='fixture-private-key', label='fixture'))
        yield client, headers, provider, model, requests


CASES = [('model', 'models/{model}/verify', 'model_id'),
         ('provider', 'providers/{provider}/verify', 'provider_id'),
         ('all', 'verify', None),
         ('fetch', 'providers/{provider}/models/fetch', 'provider_id')]


@pytest.mark.parametrize('action,path,param', CASES)
def test_http_ws_native_replay_and_reopen_never_resend(network, action, path, param):
    client, headers, provider, model, requests = network
    suffix = path.format(provider=provider, model=model)
    api, native = '/api/v1/settings/ai/' + suffix, '/settings/ai/' + suffix
    target = None if action == 'all' else model if action == 'model' else provider
    assert client.post(api, headers=headers, json={}).status_code == 428
    body = observed_network(client, action, target)
    page = client.get('/settings/ai')
    assert page.status_code == 200
    rendered = next(row['fields'] for row in Forms(page.text).forms if urlsplit(row['action']).path == native)
    for name, value in body.items():
        if name != 'idempotency_key':
            assert rendered[name] == str(value)
    response = client.post(api, headers=headers, json=body)
    assert response.status_code == 202, response.text
    assert response.headers['cache-control'] == 'no-store'
    receipt = response.json()
    client.portal.call(settled, client.app.state.ai_network_commands)
    with client.websocket_connect('/ws/ui', headers={'Origin': 'http://testserver'}) as ws:
        ready = hello(client, ws)
        pattern = '/api/v1/settings/ai/' + path.replace('{provider}', '{provider_id}').replace('{model}', '{model_id}')
        command = next(name for name, item in ready['commands'].items() if item['path'] == pattern and item['method'] == 'POST')
        replay, _ = call(ws, command, {'body': body, 'params': {param: target} if param else {}})
        assert replay['type'] == 'command.result', replay
        assert replay['data'] == receipt
    native_response = client.post(native, data={**body, 'csrf_token': headers['X-CSRF-Token']})
    assert native_response.status_code == 303, native_response.text
    for _ in range(2):
        result = client.get(receipt['result_url'])
        assert result.status_code == 200 and result.headers['cache-control'] == 'no-store'
        assert result.json()['status'] == 'COMPLETED'
        page = client.get(native_response.headers['location'])
        assert page.status_code == 200 and 'AI 操作结果' in page.text
        assert receipt['ai_operation_id'] in client.get('/settings/ai').text
    assert len(requests) == 1
    assert 'fixture-private-key' not in result.text and 'fixture-private-key' not in page.text
    client.cookies.clear()
    assert client.get(receipt['result_url']).status_code == 401


@pytest.mark.parametrize('action,path,param', CASES)
def test_conflict_retains_original_conditions_until_explicit_confirmation(network, action, path, param):
    client, headers, provider, model, requests = network
    target = None if action == 'all' else model if action == 'model' else provider
    body = {**observed_network(client, action, target), 'csrf_token': headers['X-CSRF-Token']}
    database = client.app.state.database
    client.portal.call(database.set_ai_provider_model_enabled, model, False)
    if action in ('model', 'all', 'provider'):
        client.portal.call(database.set_ai_provider_model_enabled, model, True)
    if action in ('fetch', 'provider'):
        client.portal.call(partial(client.app.state.ai_service.add_key, provider, api_key='second-fixture-key', label='fixture'))
    native = '/settings/ai/' + path.format(provider=provider, model=model)
    rejected = client.post(native, data=body)
    assert rejected.status_code == 409, rejected.text
    draft = next(row['fields'] for row in Forms(rejected.text).forms if row['fields'].get('idempotency_key') == body['idempotency_key'])
    for name, value in body.items():
        assert draft[name] == str(value)
    assert requests == []
    confirmation = unescape(re.search(r'name="confirmed_command"\s+value="([^"]+)"', rejected.text)[1])
    first = client.post(native, data={**body, 'confirmed_command': confirmation})
    assert first.status_code == 303, first.text
    client.portal.call(settled, client.app.state.ai_network_commands)
    second = client.post(native, data={**body, 'confirmed_command': confirmation})
    assert second.status_code == 303 and second.headers['location'] == first.headers['location']
    assert len(requests) == 1


def test_listing_checklist_belongs_only_to_original_provider_configuration(network):
    client, headers, provider, model, requests = network
    body = observed_network(client, 'fetch', provider)
    response = client.post(f'/api/v1/settings/ai/providers/{provider}/models/fetch', headers=headers, json=body)
    assert response.status_code == 202
    client.portal.call(settled, client.app.state.ai_network_commands)
    location = '/settings/ai?ai_operation=' + response.json()['ai_operation_id']
    page = client.get(location)
    assert 'value="discovered-fixture"' in page.text
    client.portal.call(partial(client.app.state.ai_service.add_key, provider, api_key='second-fixture-key', label='fixture'))
    page = client.get(location)
    assert page.status_code == 200 and 'value="discovered-fixture"' not in page.text
    assert 'AI 操作结果' in page.text and len(requests) == 1


def test_deleted_numeric_id_cannot_be_confirmed_as_replacement(network):
    client, headers, provider, model, requests = network
    body = {**observed_network(client, 'model', model), 'csrf_token': headers['X-CSRF-Token']}
    database = client.app.state.database
    client.portal.call(database.delete_ai_provider_model, model)
    replacement = client.portal.call(database.add_ai_provider_model, provider, 'replacement')
    assert replacement.model_id == model
    response = client.post(f'/settings/ai/models/{model}/verify', data=body)
    assert response.status_code == 410
    draft = next(row['fields'] for row in Forms(response.text).forms if row['fields'].get('idempotency_key') == body['idempotency_key'])
    assert draft['model_uuid'] == body['model_uuid']
    assert 'name="confirmed_command"' not in response.text
    assert requests == []


@pytest.mark.parametrize('transport', ['api', 'native'])
@pytest.mark.parametrize('change', ['key', 'provider'])
def test_listing_selection_rechecks_configuration_at_save_and_replays_after_changes(network, transport, change):
    client, headers, provider, model, requests = network
    admitted = client.post(f'/api/v1/settings/ai/providers/{provider}/models/fetch', headers=headers,
                           json=observed_network(client, 'fetch', provider))
    assert admitted.status_code == 202
    client.portal.call(settled, client.app.state.ai_network_commands)
    operation_id = admitted.json()['ai_operation_id']
    page = client.get('/settings/ai?ai_operation=' + operation_id)
    values = {**observed_catalogue(client, 'models-add', provider), 'model_name': ['discovered-fixture'],
              'discovery_operation_id': operation_id}
    path = f'/api/v1/settings/ai/providers/{provider}/models'
    native = f'/settings/ai/providers/{provider}/models'
    if transport == 'api':
        response = client.post(path, headers=headers, json=values)
    else:
        response = client.post(native, data={**values, 'csrf_token': headers['X-CSRF-Token']})
    assert response.status_code == (200 if transport == 'api' else 303), response.text
    if change == 'key':
        client.portal.call(partial(client.app.state.ai_service.add_key, provider, api_key='changed-fixture-key', label='fixture'))
    else:
        client.portal.call(client.app.state.ai_service.save_provider, {'provider_id': provider, 'name': 'changed',
                           'base_url': 'https://changed.invalid/v1', 'enabled': True})
    # A completed add retains its original receipt even after the listing is stale.
    replay = client.post(path, headers=headers, json=values)
    assert replay.status_code == 200 and replay.json()['status'] == 'COMPLETED'
    stale = {**observed_catalogue(client, 'models-add', provider), 'model_name': ['second-discovered'],
             'discovery_operation_id': operation_id}
    if transport == 'api':
        rejected = client.post(path, headers=headers, json=stale)
        assert rejected.status_code == 409 and rejected.json()['error']['code'] == 'AI_DISCOVERY_STALE'
    else:
        rejected = client.post(native, data={**stale, 'csrf_token': headers['X-CSRF-Token']})
        assert rejected.status_code == 409 and '请重新拉取' in rejected.text
        draft = next(row['fields'] for row in Forms(rejected.text).forms
                     if row['fields'].get('idempotency_key') == stale['idempotency_key'])
        assert draft['discovery_operation_id'] == operation_id
        assert 'value="second-discovered"' in rejected.text
        assert 'name="confirmed_command"' not in rejected.text
    current = client.get('/api/v1/settings/ai').json()['providers'][0]
    assert all(row['name'] != 'second-discovered' for row in current['models'])
    assert current['models_revision'] == stale['expected_revision']
    assert f'name="discovery_operation_id" value="{operation_id}"' in page.text
    assert len(requests) == 1
