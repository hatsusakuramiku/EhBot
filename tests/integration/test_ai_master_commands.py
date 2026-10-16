"""The AI master switch is an independent preference command on every transport."""
import asyncio
from html import unescape
import re

from fastapi.testclient import TestClient
import pytest

from app.main import create_app
from app.review.commands import WorkCommandError
from app.settings.commands import SettingsCommands
from tests.integration.test_manual_add import _settings, _authenticate
from tests.integration.test_realtime import hello, call
from tests.integration.test_settings_command_http import fields
from tests.integration.test_settings_commands import setup
from tests.integration.test_work_enqueue_http import stop_workers


@pytest.mark.asyncio
async def test_master_concurrency_independence_and_receipt_failure_rollback(tmp_path, monkeypatch):
    database, system, _, commands = await setup(tmp_path)
    body = {'expected_revision': (await commands.versions('ai'))['ai-master'], 'enabled': False}
    results = await asyncio.gather(*(commands.execute('ai-master', {**body, 'idempotency_key': key})
                                    for key in ('device-a', 'device-b')), return_exceptions=True)
    assert sum(isinstance(row, dict) for row in results) == 1
    assert [row.code for row in results if isinstance(row, WorkCommandError)] == ['REVISION_CONFLICT']
    assert not await system.ai_enabled()
    version = (await commands.versions('ai'))['ai-master']
    with database.connection() as connection:
        connection.execute("INSERT INTO system_settings(key, value) VALUES('ai_candidate_enabled', '1') "
                           "ON CONFLICT(key) DO UPDATE SET value = '1'")
    assert (await commands.versions('ai'))['ai-master'] == version

    def fail(*args):
        raise RuntimeError('Fixture receipt failed')
    monkeypatch.setattr(SettingsCommands, '_save_receipt', staticmethod(fail))
    with pytest.raises(RuntimeError):
        await commands.execute('ai-master', {'expected_revision': version, 'idempotency_key': 'failed', 'enabled': True})
    assert not await system.ai_enabled() and (await commands.versions('ai'))['ai-master'] == version
    await system.save_ai_enabled(True)
    assert (await commands.versions('ai'))['ai-master'] == version + 1


def test_master_http_ws_plain_form_replay_and_explicit_confirmation(tmp_path):
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), follow_redirects=False) as client:
        _authenticate(client, settings)
        stop_workers(client)
        page = client.get('/settings/ai')
        headers = {'X-CSRF-Token': page.context['csrf_token']}
        api = '/api/v1/settings/ai/master'
        assert client.put(api, headers=headers, json={'enabled': False}).status_code == 428
        form = fields(client, 'ai', '/settings/ai/master')
        body = {'expected_revision': int(form['expected_revision']), 'idempotency_key': form['idempotency_key'],
                'enabled': False}
        first = client.put(api, headers=headers, json=body)
        assert first.status_code == 200 and first.json()['scope'] == 'ai-master'
        with client.websocket_connect('/ws/ui', headers={'Origin': 'http://testserver'}) as ws:
            ready = hello(client, ws)
            name = next(name for name, entry in ready['commands'].items() if entry['path'] == api)
            replay, _ = call(ws, name, {'body': body})
            assert replay['type'] == 'command.result' and replay['data'] == first.json()
        assert client.post('/settings/ai/master', data=form).status_code == 303
        stale = client.post('/settings/ai/master', data={**form, 'idempotency_key': 'stale', 'ai_enabled': 'on'})
        assert stale.status_code == 409 and 'data-dirty="true"' in stale.text
        assert re.search(r'name="ai_enabled"[^>]+checked', stale.text)
        assert client.get('/api/v1/settings/ai').json()['ai_enabled'] is False
        confirm = unescape(re.search(r'name="confirmed_command" value="([^"]+)"', stale.text)[1])
        confirmed = {**form, 'idempotency_key': 'stale', 'ai_enabled': 'on', 'confirmed_command': confirm}
        for _ in range(2):
            assert client.post('/settings/ai/master', data=confirmed).status_code == 303
        assert client.get('/api/v1/settings/ai').json()['ai_enabled'] is True
        assert client.put(api, headers=headers, json=body).json() == first.json()
        assert client.get('/api/v1/settings/ai').json()['ai_enabled'] is True
