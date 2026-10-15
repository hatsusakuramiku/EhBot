"""Bound actual account requests and persist Telegram's account-wide cooldown."""
import asyncio
from contextlib import asynccontextmanager, closing
from contextvars import ContextVar
from functools import wraps

from app.connections.models import ProviderConnectionError
from app.connections.execution import current_account_executions
from app.downloads.execution import run_sync


current_requests = ContextVar('account_requests', default=None)
held_request = ContextVar('held_account_request', default=None)


class AccountRequests:
    # Long polls never consume the short-request/metadata/transfer budget.
    # These initial bounds are conservative resource limits, not throughput tuning.
    REQUESTS = 4
    RECEIVERS = 4

    def __init__(self, database):
        self.database = database
        self._global = {'receive': asyncio.Semaphore(self.RECEIVERS),
                        'request': asyncio.Semaphore(self.REQUESTS)}
        self._accounts = {}

    def _remaining(self, account_id):
        with self.database.connection() as connection:
            row = connection.execute("SELECT max(0, CAST(strftime('%s', retry_at) AS INTEGER) - "
                "CAST(strftime('%s', 'now') AS INTEGER)) FROM telegram_cooldowns WHERE account_id = ?",
                (account_id,)).fetchone()
            return row[0] if row else 0

    def _record(self, account_id, error):
        seconds = max(1, int(error.retry_after or 60))
        # Rate-limit evidence remains useful even if the request was revoked
        # while the supplier replied; it changes no task/configuration facts.
        with closing(self.database._connect()) as connection, connection:
            connection.execute("INSERT INTO telegram_cooldowns(account_id, retry_at, error_code) "
                "VALUES(?, datetime('now', ?), ?) ON CONFLICT(account_id) DO UPDATE SET "
                "retry_at = max(telegram_cooldowns.retry_at, excluded.retry_at), error_code = excluded.error_code",
                (account_id, f'+{seconds} seconds', error.code))

    async def check(self, context):
        # A stop may have been committed while this request waited for a permit.
        session = getattr(self.database, 'worker_session', None)
        if session is not None and session.stopping:
            from app.connections.accounts import AccountError
            raise AccountError('ACCOUNT_UNAVAILABLE', '后台正在停止，暂不接受新的账号请求')
        with self.database.connection() as connection:
            for execution in current_account_executions.get():
                if execution.database_path == self.database.path:
                    execution.check(connection)
        if context.kind not in {'TG_BOT', 'TG_USER'}:
            return
        seconds = await run_sync(self._remaining, context.account_id)
        if seconds:
            if context.kind == 'TG_USER':
                from app.connections.telegram_user import TelegramUserError
                error = TelegramUserError
                code = 'TELEGRAM_USER_RATE_LIMITED'
            else:
                error = ProviderConnectionError
                code = 'TELEGRAM_RATE_LIMITED'
            raise error(code, 'Telegram 账号仍在限流冷却，请稍后重试', retry_after=seconds)

    @asynccontextmanager
    async def use(self, context, lane='request'):
        task = asyncio.current_task()
        if held_request.get() == (task, context.account_id, lane):
            yield
            return
        await self.check(context)
        lock = self._accounts.setdefault((context.account_id, lane), asyncio.Lock())
        # Wait for the account before consuming a global permit; a hot account
        # cannot occupy the entire global budget with its own waiting requests.
        async with lock, self._global['receive' if lane == 'receive' else 'request']:
            await self.check(context)
            token = held_request.set((task, context.account_id, lane))
            try:
                yield
            except ProviderConnectionError as exc:
                if context.kind in {'TG_BOT', 'TG_USER'} and exc.code in {
                    'TELEGRAM_RATE_LIMITED', 'TELEGRAM_USER_RATE_LIMITED',
                }:
                    await run_sync(self._record, context.account_id, exc)
                raise
            finally:
                held_request.reset(token)


def account_request(lane='request'):
    """Apply runtime account limits around the adapter's entire physical call."""
    def decorate(method):
        @wraps(method)
        async def wrapped(*args, **kwargs):
            scope = current_requests.get()
            if scope is None:
                return await method(*args, **kwargs)
            budget, context = scope
            selected = ('receive' if args[1] == 'getUpdates' else 'request') if lane == 'bot' else lane
            async with budget.use(context, selected):
                return await method(*args, **kwargs)
        return wrapped
    return decorate
