"""Durable probes exercise real transport, immutable inputs and restart fences."""
import asyncio
import json
import sqlite3
from threading import Event

import httpx
import pytest

from app.ai.service import AiProviderService
from app.downloads.worker import WorkerSession
from app.review.commands import WorkCommandError
from app.settings.ai_network_commands import AiNetworkCommands
from tests.integration.test_ai_catalogue_commands import catalogue
from tests.integration.test_toolchain_commands import settled


async def setup(tmp_path, handler):
    database, archive, added, _ = await catalogue(tmp_path)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = AiProviderService(database, archive, http_client=http)
    provider_id = added['provider_id']
    await service.add_key(provider_id, api_key='fixture-private-key', label='fixture')
    model = await service.add_model(provider_id, 'original')
    return database, service, model, http, AiNetworkCommands(database, service)


async def observed(database, model, action='model', key='probe'):
    provider = await database.get_ai_provider(model.provider_id)
    current = await database.get_ai_provider_model(model.model_id)
    if action == 'all':
        with database.connection() as connection:
            revision = connection.execute("SELECT revision FROM settings_versions WHERE scope = 'ai-catalogue'").fetchone()[0]
        return {'expected_revision': revision, 'idempotency_key': key}
    body = dict(provider_uuid=provider.provider_uuid, keys_revision=provider.keys_revision, idempotency_key=key)
    if action == 'model':
        body.update(model_uuid=current.model_uuid, expected_revision=current.revision, provider_revision=provider.revision)
    else:
        body.update(expected_revision=provider.revision)
        if action == 'provider':
            body['models_revision'] = provider.models_revision
    return body


def answer(request):
    return httpx.Response(200, json={'choices': [{'message': {'content': '{"ok":true}'}}]})


@pytest.mark.asyncio
async def test_same_key_concurrent_and_conflicting_batch_share_one_network_request(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    requests = []
    async def handler(request):
        requests.append(request)
        entered.set()
        await release.wait()
        return answer(request)
    database, service, model, http, commands = await setup(tmp_path, handler)
    peer = AiNetworkCommands(database, service)
    body = await observed(database, model)
    try:
        a, b = await asyncio.gather(commands.submit('model', body, target_id=model.model_id),
                                   peer.submit('model', body, target_id=model.model_id))
        assert a == b and a['status'] == 'ACCEPTED'
        await asyncio.wait_for(entered.wait(), 5)
        for action, target_id in [('model', model.model_id), ('provider', model.provider_id), ('all', None)]:
            with pytest.raises(WorkCommandError) as error:
                await peer.submit(action, await observed(database, model, action, key=action), target_id=target_id)
            assert error.value.code == 'AI_NETWORK_BUSY'
        assert len(requests) == 1
    finally:
        release.set()
        await commands.stop()
        await peer.stop()
        await http.aclose()
    result = await commands.result(a['ai_operation_id'])
    assert result['status'] == 'COMPLETED' and result['result']['ok'] == 1
    assert 'fixture-private-key' not in str(result)
    replay = AiNetworkCommands(database, service)
    await database.delete_ai_provider_model(model.model_id)
    assert await replay.submit('model', body, target_id=model.model_id) == a
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['model', 'provider', 'all', 'fetch'])
async def test_admitted_request_never_adopts_replacement_configuration_or_credentials(tmp_path, monkeypatch, action):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={'data': [{'id': 'discovered'}]}) if action == 'fetch' else answer(request)
    database, service, model, http, commands = await setup(tmp_path, handler)
    body = await observed(database, model, action)
    entered, release = asyncio.Event(), asyncio.Event()
    run = commands._run
    async def delayed(*args):
        entered.set()
        await release.wait()
        await run(*args)
    monkeypatch.setattr(commands, '_run', delayed)
    target_id = None if action == 'all' else model.model_id if action == 'model' else model.provider_id
    try:
        receipt = await commands.submit(action, body, target_id=target_id)
        await asyncio.wait_for(entered.wait(), 5)
        await database.delete_ai_provider(model.provider_id)
        replacement = await service.save_provider({'name': 'replacement', 'base_url': 'https://replacement.invalid/v1', 'enabled': True})
        assert replacement.provider_id == model.provider_id
        await service.add_key(replacement.provider_id, api_key='replacement-private-key', label='fixture')
        new_model = await service.add_model(replacement.provider_id, 'replacement')
        assert new_model.model_id == model.model_id
    finally:
        release.set()
        await commands.stop()
        await http.aclose()
    assert len(requests) == 1
    assert requests[0].url.host == 'fixture.invalid'
    assert requests[0].headers['Authorization'] == 'Bearer fixture-private-key'
    if action != 'fetch':
        assert json.loads(requests[0].content)['model'] == 'original'
        assert (await database.get_ai_provider_model(model.model_id)).last_verify_ok is None
        assert (await commands.result(receipt['ai_operation_id']))['result']['entries'][0]['code'] == 'AI_CONFIGURATION_CHANGED'
    else:
        assert (await commands.result(receipt['ai_operation_id']))['result']['names'] == ['discovered']
    result = await commands.result(receipt['ai_operation_id'])
    assert 'private-key' not in json.dumps(result)
    with database.connection() as connection:
        raw = connection.execute('SELECT target_json, result_json FROM ai_network_operations').fetchone()
        assert 'private-key' not in str(raw) and 'base_url' not in str(raw)


@pytest.mark.asyncio
async def test_admission_rollback_does_not_dispatch_and_config_conflicts_do_not_record(tmp_path):
    requests = []
    def handler(request):
        requests.append(request)
        return answer(request)
    database, _, model, http, commands = await setup(tmp_path, handler)
    body = await observed(database, model)
    try:
        with pytest.raises(WorkCommandError) as error:
            await commands.submit('model', {}, target_id=model.model_id)
        assert error.value.status_code == 428
        with database.connection() as connection:
            connection.execute("CREATE TRIGGER fail_probe BEFORE INSERT ON ai_network_claims BEGIN SELECT RAISE(ABORT, 'fixture'); END")
        with pytest.raises(sqlite3.IntegrityError):
            await commands.submit('model', body, target_id=model.model_id)
        with database.connection() as connection:
            assert connection.execute('SELECT count(*) FROM ai_network_operations').fetchone() == (0,)
            assert connection.execute('SELECT count(*) FROM ai_network_claims').fetchone() == (0,)
            connection.execute('DROP TRIGGER fail_probe')
        await database.set_ai_provider_model_enabled(model.model_id, False)
        with pytest.raises(WorkCommandError) as error:
            await commands.submit('model', body, target_id=model.model_id)
        assert error.value.code == 'REVISION_CONFLICT'
        assert requests == []
    finally:
        await commands.stop()
        await http.aclose()


@pytest.mark.asyncio
async def test_disconnect_after_commit_dispatches_once_and_stop_drains_the_request(tmp_path, monkeypatch):
    committed, dispatch = Event(), Event()
    entered, release = asyncio.Event(), asyncio.Event()
    requests = []
    async def handler(request):
        requests.append(request)
        entered.set()
        await release.wait()
        return answer(request)
    database, _, model, http, commands = await setup(tmp_path, handler)
    body = await observed(database, model)
    begin = commands._begin
    def delayed(*args):
        result = begin(*args)
        committed.set()
        assert dispatch.wait(5)
        return result
    monkeypatch.setattr(commands, '_begin', delayed)
    caller = asyncio.create_task(commands.submit('model', body, target_id=model.model_id))
    stopping = None
    try:
        assert await asyncio.to_thread(committed.wait, 5)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        stopping = asyncio.create_task(commands.stop())
        dispatch.set()
        await asyncio.wait_for(entered.wait(), 5)
        assert not stopping.done()
    finally:
        dispatch.set()
        release.set()
        if stopping:
            await stopping
        else:
            await commands.stop()
        await http.aclose()
    assert len(requests) == 1
    assert (await commands.recent())[0]['status'] == 'COMPLETED'


@pytest.mark.asyncio
async def test_restart_leaves_unknown_and_never_dispatches_original_intent(tmp_path):
    requests = []
    def handler(request):
        requests.append(request)
        return answer(request)
    database, service, model, http, commands = await setup(tmp_path, handler)
    worker = WorkerSession(database)
    await worker.start()
    body = await observed(database, model)
    target, key = commands._payload('model', body, model.model_id)
    receipt, _ = commands._begin(target, key)
    await worker.stop()
    replacement = WorkerSession(database)
    await replacement.start()
    try:
        replay = AiNetworkCommands(database, service)
        assert await replay.submit('model', body, target_id=model.model_id) == receipt
        result = await replay.result(receipt['ai_operation_id'])
        assert result['status'] == 'UNKNOWN' and result['error_code'] == 'AI_REQUEST_INTERRUPTED'
        assert requests == []
        with database.connection() as connection:
            assert connection.execute('SELECT count(*) FROM ai_network_claims').fetchone() == (0,)
    finally:
        await replacement.stop()
        await http.aclose()


@pytest.mark.asyncio
async def test_raw_network_errors_do_not_enter_durable_results(tmp_path):
    def handler(request):
        return httpx.Response(400, json={'error': {'message': 'fixture-private-key must remain private'}})
    database, _, model, http, commands = await setup(tmp_path, handler)
    try:
        receipt = await commands.submit('model', await observed(database, model), target_id=model.model_id)
        await settled(commands)
        result = await commands.result(receipt['ai_operation_id'])
        assert result['status'] == 'FAILED'
        with database.connection() as connection:
            assert 'fixture-private-key' not in str(connection.execute('SELECT result_json FROM ai_network_operations').fetchall())
            assert 'fixture-private-key' not in str(connection.execute('SELECT last_verify_error FROM ai_provider_models').fetchall())
    finally:
        await commands.stop()
        await http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['provider', 'all'])
async def test_batch_captures_later_models_keys_and_provider_before_first_request(tmp_path, action):
    entered, release = asyncio.Event(), asyncio.Event()
    requests = []
    async def handler(request):
        requests.append(request)
        if len(requests) == 1:
            entered.set()
            await release.wait()
        return answer(request)
    database, service, model, http, commands = await setup(tmp_path, handler)
    second = await service.add_model(model.provider_id, 'second')
    try:
        receipt = await commands.submit(action, await observed(database, model, action),
                                        target_id=model.provider_id if action == 'provider' else None)
        await asyncio.wait_for(entered.wait(), 5)
        await database.delete_ai_provider_model(second.model_id)
        await service.add_model(model.provider_id, 'replacement')
        for key in await database.list_ai_provider_keys(model.provider_id):
            await database.delete_ai_provider_key(key.key_id)
        await service.add_key(model.provider_id, api_key='replacement-private-key', label='fixture')
        await service.save_provider({'provider_id': model.provider_id, 'name': 'changed',
                                     'base_url': 'https://replacement.invalid/v1', 'enabled': True})
    finally:
        release.set()
        await commands.stop()
        await http.aclose()
    assert [json.loads(request.content)['model'] for request in requests] == ['original', 'second']
    assert all(request.url.host == 'fixture.invalid' for request in requests)
    assert all(request.headers['Authorization'] == 'Bearer fixture-private-key' for request in requests)
    assert (await commands.result(receipt['ai_operation_id']))['result']['total'] == 2
    assert all(row.last_verify_ok is None for row in await database.list_ai_provider_models(model.provider_id))


@pytest.mark.asyncio
async def test_distinct_models_can_run_together_and_listing_claim_is_independent(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    requests = []
    async def handler(request):
        requests.append(request)
        if len(requests) == 3:
            entered.set()
        await release.wait()
        return httpx.Response(200, json={'data': []}) if request.url.path.endswith('/models') else answer(request)
    database, service, model, http, commands = await setup(tmp_path, handler)
    second = await service.add_model(model.provider_id, 'second')
    try:
        await commands.submit('model', await observed(database, model), target_id=model.model_id)
        await commands.submit('model', await observed(database, second, key='second'), target_id=second.model_id)
        listing = await observed(database, model, 'fetch', key='listing')
        await commands.submit('fetch', listing, target_id=model.provider_id)
        await asyncio.wait_for(entered.wait(), 5)
        with pytest.raises(WorkCommandError) as error:
            await commands.submit('fetch', {**listing, 'idempotency_key': 'other'}, target_id=model.provider_id)
        assert error.value.code == 'AI_NETWORK_BUSY'
        with pytest.raises(WorkCommandError) as error:
            await commands.submit('model', {**await observed(database, model), 'idempotency_key': 'listing'}, target_id=model.model_id)
        assert error.value.code == 'IDEMPOTENCY_CONFLICT'
    finally:
        release.set()
        await commands.stop()
        await http.aclose()
    assert len(requests) == 3


@pytest.mark.asyncio
async def test_lost_worker_session_fences_late_verification_and_remaining_batch(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    requests = []
    async def handler(request):
        requests.append(request)
        entered.set()
        await release.wait()
        return answer(request)
    database, service, model, http, commands = await setup(tmp_path, handler)
    await service.add_model(model.provider_id, 'second')
    worker = WorkerSession(database)
    await worker.start()
    replacement = None
    try:
        receipt = await commands.submit('provider', await observed(database, model, 'provider'), target_id=model.provider_id)
        await asyncio.wait_for(entered.wait(), 5)
        await worker.stop()
        replacement = WorkerSession(database)
        await replacement.start()
    finally:
        release.set()
        await commands.stop()
        if replacement:
            await replacement.stop()
        await http.aclose()
    assert len(requests) == 1
    assert (await commands.result(receipt['ai_operation_id']))['status'] == 'UNKNOWN'
    assert all(row.last_verify_ok is None for row in await database.list_ai_provider_models(model.provider_id))


@pytest.mark.asyncio
async def test_canceled_request_releases_claim_without_resend_and_stop_refuses_new_work(tmp_path):
    entered = asyncio.Event()
    requests = []
    async def handler(request):
        requests.append(request)
        entered.set()
        await asyncio.Event().wait()
    database, _, model, http, commands = await setup(tmp_path, handler)
    body = await observed(database, model)
    try:
        receipt = await commands.submit('model', body, target_id=model.model_id)
        await asyncio.wait_for(entered.wait(), 5)
        for task in tuple(commands._tasks):
            task.cancel()
        await settled(commands)
        assert (await commands.result(receipt['ai_operation_id']))['status'] == 'UNKNOWN'
        with database.connection() as connection:
            assert connection.execute('SELECT count(*) FROM ai_network_claims').fetchone() == (0,)
        assert await commands.submit('model', body, target_id=model.model_id) == receipt
        await commands.stop()
        with pytest.raises(WorkCommandError) as error:
            await commands.submit('model', {**body, 'idempotency_key': 'new'}, target_id=model.model_id)
        assert error.value.code == 'WORKER_STOPPING' and len(requests) == 1
    finally:
        await commands.stop()
        await http.aclose()
