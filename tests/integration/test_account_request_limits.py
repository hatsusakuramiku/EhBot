"""Real adapter calls obey shared bounds, physical cleanup and durable cooldown."""
import asyncio

import httpx
import pytest

from app.connections.lifecycle import AccountLifecycle
from app.connections.models import ProviderConnectionError
from app.connections.runtime import AccountRuntime
from app.connections.telegram import TelegramBotApi
from app.connections.telegram_user import TelegramUserClient, TelegramUserCredentials, TelegramUserError
from tests.integration.test_account_registry import registry
from tests.integration.test_account_lifecycle import active_account, no_queue_operations


@pytest.mark.asyncio
async def test_shared_request_budget_and_per_account_serialization(registry):
    accounts = [await active_account(registry, 'TG_BOT', str(n)) for n in range(6)]
    runtime = AccountRuntime(registry, AccountLifecycle(registry))
    release, four_entered = asyncio.Event(), asyncio.Event()
    active, peak, per_account, peaks = 0, 0, {}, {}
    async def handler(request):
        nonlocal active, peak
        account = request.url.path.split('/')[1]
        per_account[account] = per_account.get(account, 0) + 1
        peaks[account] = max(peaks.get(account, 0), per_account[account])
        active += 1
        peak = max(peak, active)
        if active == 4:
            four_entered.set()
        try:
            await release.wait()
            return httpx.Response(200, json={'ok': True, 'result': {'file_id': 'x', 'file_path': 'x.zip'}})
        finally:
            active -= 1
            per_account[account] -= 1
    async with httpx.AsyncClient(base_url='https://api.telegram.org', transport=httpx.MockTransport(handler)) as client:
        async def call(index):
            account, _ = accounts[index]
            async with runtime.use(account['account_id']):
                await TelegramBotApi(str(index), client).get_file('file')
        tasks = [asyncio.create_task(call(index)) for index in [0, 0, 1, 2, 3, 4, 5]]
        try:
            await asyncio.wait_for(four_entered.wait(), 4)
            assert peak == 4 and sum(task.done() for task in tasks) == 0
        finally:
            release.set()
            await asyncio.gather(*tasks)
    assert peak == 4 and all(value == 1 for value in peaks.values())


@pytest.mark.asyncio
async def test_long_poll_does_not_occupy_download_request_lane(registry):
    account, _ = await active_account(registry, 'TG_BOT')
    runtime = AccountRuntime(registry, AccountLifecycle(registry))
    polling, release = asyncio.Event(), asyncio.Event()
    async def handler(request):
        if request.url.path.endswith('getUpdates'):
            polling.set()
            await release.wait()
            return httpx.Response(200, json={'ok': True, 'result': []})
        return httpx.Response(200, json={'ok': True, 'result': {'file_id': 'x', 'file_path': 'x.zip'}})
    async with httpx.AsyncClient(base_url='https://api.telegram.org', transport=httpx.MockTransport(handler)) as client:
        async def poll():
            async with runtime.use(account['account_id']):
                await TelegramBotApi('fixture', client).get_updates(None)
        task = asyncio.create_task(poll())
        try:
            await asyncio.wait_for(polling.wait(), 3)
            async with runtime.use(account['account_id']):
                result = await asyncio.wait_for(TelegramBotApi('fixture', client).get_file('file'), 3)
                assert result.file_path == 'x.zip' and not task.done()
        finally:
            release.set()
            await task


@pytest.mark.asyncio
async def test_cancelled_http_holds_account_permit_until_cleanup_exits(registry):
    account, _ = await active_account(registry, 'TG_BOT')
    lifecycle = AccountLifecycle(registry)
    runtime = AccountRuntime(registry, lifecycle)
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = 0
    async def handler(request):
        nonlocal calls
        calls += 1
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
    async with httpx.AsyncClient(base_url='https://api.telegram.org', transport=httpx.MockTransport(handler)) as client:
        async def call():
            async with runtime.use(account['account_id']):
                await TelegramBotApi('fixture', client).get_file('file')
        first = asyncio.create_task(call())
        await asyncio.wait_for(entered.wait(), 3)
        second = asyncio.create_task(call())
        first.cancel()
        await asyncio.wait_for(cleaning.wait(), 3)
        stopping = None
        try:
            assert calls == 1 and not first.done() and not second.done()
            await lifecycle.request_stop(account['account_id'], 'disable', expected_revision=account['revision'],
                                         idempotency_key='stop-budget-waiters')
            stopping = asyncio.create_task(lifecycle.process_operations(
                stop_runtime=runtime.stop, process_queue_operations=no_queue_operations))
            await asyncio.sleep(0)
            assert not stopping.done()
        finally:
            release.set()
            if stopping is None:
                second.cancel()
            await asyncio.gather(*(task for task in (first, second, stopping) if task is not None), return_exceptions=True)
        assert calls == 1 and stopping.result() == 1 and not runtime._leases


@pytest.mark.asyncio
async def test_bot_cooldown_survives_runtime_restart_and_blocks_other_lanes(registry):
    account, _ = await active_account(registry, 'TG_BOT')
    calls = []
    async def handler(request):
        calls.append(request.url.path)
        return httpx.Response(429, json={'ok': False, 'error_code': 429, 'parameters': {'retry_after': 90}})
    async with httpx.AsyncClient(base_url='https://api.telegram.org', transport=httpx.MockTransport(handler)) as client:
        api = TelegramBotApi('fixture', client)
        runtime = AccountRuntime(registry, AccountLifecycle(registry))
        async with runtime.use(account['account_id']):
            with pytest.raises(ProviderConnectionError):
                await api.get_updates(None)
        restarted = AccountRuntime(registry, AccountLifecycle(registry))
        async with restarted.use(account['account_id']):
            with pytest.raises(ProviderConnectionError) as error:
                await api.get_file('file')
            assert error.value.code == 'TELEGRAM_RATE_LIMITED' and error.value.retry_after > 0
        assert len(calls) == 1
    with registry._database.connection() as c:
        assert c.execute('SELECT count(*) FROM telegram_cooldowns').fetchone()[0] == 1


@pytest.mark.asyncio
async def test_mtproto_flood_wait_is_saved_after_client_disconnect(registry):
    account = await registry.create('TG_USER', 'User', {'api_id': '1234', 'api_hash': 'fixture', 'phone': '+123456'})
    account = await registry.save_session(account['account_id'], expected_revision=account['revision'],
        session='fixture-session', remote_identity='44', platform_name='User')
    account = await registry.enable(account['account_id'], expected_revision=account['revision'])
    class FloodWaitError(Exception):
        seconds = 75
    closed = []
    class Client:
        async def is_user_authorized(self):
            raise FloodWaitError()
        async def disconnect(self):
            closed.append(True)
    adapter = TelegramUserClient(TelegramUserCredentials(1234, 'fixture'), 'fixture-session')
    async def connect():
        return Client()
    adapter._connect = connect
    runtime = AccountRuntime(registry, AccountLifecycle(registry))
    async with runtime.use(account['account_id']):
        with pytest.raises(TelegramUserError) as error:
            await adapter.verify()
        assert error.value.code == 'TELEGRAM_USER_RATE_LIMITED'
    assert closed == [True]
    with registry._database.connection() as c:
        assert c.execute('SELECT retry_at > CURRENT_TIMESTAMP FROM telegram_cooldowns').fetchone()[0] == 1


@pytest.mark.asyncio
async def test_startup_resumes_listener_after_saved_cooldown_expires(registry):
    from app.connections.service import AccountService
    account, _ = await active_account(registry, 'TG_BOT')
    runtime = AccountRuntime(registry, AccountLifecycle(registry))
    runtime.requests._record(account['account_id'], ProviderConnectionError(
        'TELEGRAM_RATE_LIMITED', 'fixture limit', retry_after=90))
    requests = []
    async def handler(request):
        requests.append(request.url.path)
        if request.url.path.endswith('getMe'):
            return httpx.Response(200, json={'ok': True, 'result': {'id': 1234, 'username': 'fixture', 'first_name': 'Bot'}})
        await asyncio.Event().wait()
    async with httpx.AsyncClient(base_url='https://api.telegram.org', transport=httpx.MockTransport(handler)) as client:
        service = AccountService(registry, telegram_client=client, exhentai_client=client)
        await service.start()
        try:
            assert requests == [] and service._cooling_restarts
            service._operation_task.cancel()
            await asyncio.gather(service._operation_task, return_exceptions=True)
            service._operation_task = None
            with registry._database.connection() as c:
                c.execute("UPDATE telegram_cooldowns SET retry_at = datetime('now', '-1 second')")
            service._cooling_restarts[account['account_id']] = 0
            await service._resume_cooling_accounts()
            assert service.account_status(account['account_id'])['state'] == 'connected'
            assert sum(path.endswith('getMe') for path in requests) == 1
            assert service._cooling_restarts == {}
        finally:
            await service.stop()
