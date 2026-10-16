"""Shared settings contracts across real HTTP, WS and plain forms."""
from fastapi.testclient import TestClient

from app.main import create_app
from tests.integration.test_manual_add import _settings, _authenticate
from tests.integration.test_realtime import hello, call
from tests.integration.test_work_enqueue_http import stop_workers
from tests.integration.test_work_review_http import Forms


def fields(client, section, action):
    page = client.get('/settings/' + section)
    assert page.status_code == 200, page.text
    return next(item['fields'] for item in Forms(page.text).forms if item['action'].endswith(action))


def test_same_receipt_http_ws_form_and_system_runtime_refresh(tmp_path):
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False) as client:
        _authenticate(client, settings)
        stop_workers(client)
        form = fields(client, 'system', '/settings/system')
        headers = {'X-CSRF-Token': form['csrf_token']}
        assert client.put('/api/v1/settings/system', json={'timezone': 'Asia/Shanghai'}, headers=headers).status_code == 428
        body = {'expected_revision': int(form['expected_revision']), 'idempotency_key': form['idempotency_key'],
                'timezone': 'Asia/Shanghai'}
        first = client.put('/api/v1/settings/system', json=body, headers=headers)
        assert first.status_code == 200, first.text
        assert client.app.state.display_timezone == 'Asia/Shanghai'
        with client.websocket_connect('/ws/ui', headers={'Origin': 'http://testserver'}) as ws:
            ready = hello(client, ws)
            name = next(name for name, entry in ready['commands'].items() if entry['path'] == '/api/v1/settings/system')
            replay, _ = call(ws, name, {'body': body})
            assert replay['type'] == 'command.result' and replay['data'] == first.json()
        response = client.post('/settings/system', data={'csrf_token': form['csrf_token'], **body})
        assert response.status_code == 303, response.text
        from app.settings.service import DEFAULT_DOWNLOAD_SOURCE_PRIORITY
        assert client.app.state.download_source_priority == DEFAULT_DOWNLOAD_SOURCE_PRIORITY


def test_conflict_preserves_original_form_version_key_and_editable_values(tmp_path):
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False) as client:
        _authenticate(client, settings)
        stop_workers(client)
        original = fields(client, 'system', '/settings/system')
        headers = {'X-CSRF-Token': original['csrf_token']}
        winner = client.put('/api/v1/settings/system', headers=headers, json={
            'expected_revision': int(original['expected_revision']), 'idempotency_key': 'winner', 'timezone': 'UTC'})
        assert winner.status_code == 200, winner.text
        response = client.post('/settings/system', data={**original, 'timezone': 'Asia/Tokyo'})
        assert response.status_code == 409, response.text
        assert 'data-dirty="true"' in response.text
        retained = next(item['fields'] for item in Forms(response.text).forms if item['action'].endswith('/settings/system'))
        assert retained['expected_revision'] == original['expected_revision']
        assert retained['idempotency_key'] == original['idempotency_key']
        assert 'value="Asia/Tokyo"' in response.text
        assert client.app.state.display_timezone == 'UTC'
        import re
        from html import unescape
        confirmation = unescape(re.search(r'name="confirmed_command" value="([^"]+)"', response.text)[1])
        data = {**retained, 'timezone': 'Asia/Tokyo', 'confirmed_command': confirmation}
        for _ in range(2):
            assert client.post('/settings/system', data=data).status_code == 303
        assert client.app.state.display_timezone == 'Asia/Tokyo'


def test_template_second_field_refusal_rolls_back_first_and_preserves_draft(tmp_path):
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False) as client:
        _authenticate(client, settings)
        stop_workers(client)
        form = fields(client, 'paths', '/archive-settings/paths/template')
        before = client.get('/api/v1/settings/paths').json()
        response = client.post('/archive-settings/paths/template', data={**form, 'title_source': 'english',
                                                                       'library_template': '../{title}'})
        assert response.status_code == 400, response.text
        assert 'value="../{title}"' in response.text
        retained = next(item['fields'] for item in Forms(response.text).forms if item['action'].endswith('/paths/template'))
        assert retained['expected_revision'] == form['expected_revision']
        assert retained['idempotency_key'] == form['idempotency_key']
        after = client.get('/api/v1/settings/paths').json()
        assert after['settings_versions'] == before['settings_versions']
        assert after['title_source'] == before['title_source'] == 'japanese'
        preview = client.post('/archive-settings/paths/template/preview', data={**form,
            'title_source': 'english', 'library_template': 'Draft/{title}'})
        assert preview.status_code == 200, preview.text
        retained = next(item['fields'] for item in Forms(preview.text).forms if item['action'].endswith('/paths/template'))
        assert retained['expected_revision'] == form['expected_revision']
        assert retained['idempotency_key'] == form['idempotency_key']
        assert 'value="Draft/{title}"' in preview.text


def test_versions_and_values_are_not_read_from_different_commits(tmp_path, monkeypatch):
    import app.api.settings as module
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False) as client:
        _authenticate(client, settings)
        stop_workers(client)
        original = module._SECTION_BUILDERS['system']
        calls = 0

        async def race(request):
            nonlocal calls
            result = await original(request)
            calls += 1
            if calls == 1:
                await client.app.state.system_settings_service.save({'timezone': 'Asia/Tokyo'})
            return result

        monkeypatch.setitem(module._SECTION_BUILDERS, 'system', race)
        response = client.get('/api/v1/settings/system')
        assert response.status_code == 200 and response.json()['system']['timezone'] == 'Asia/Tokyo'
        assert calls == 2
