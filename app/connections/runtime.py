"""Account-scoped runtime borrowing and physical request stop confirmation."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from app.connections.accounts import AccountContext, AccountError, AccountRegistry
from app.connections.execution import AccountExecution, current_account_executions
from app.connections.lifecycle import AccountLifecycle
from app.downloads.execution import current_execution, run_sync


current_account_context: ContextVar[AccountContext | None] = ContextVar("account_context", default=None)


@dataclass(eq=False, slots=True)
class _Lease:
    task: asyncio.Task
    exited: asyncio.Future


class AccountRuntime:
    """Keep leases per account/version, covering queue and non-queue requests.

    Every provider request/listener must borrow its exact account for its entire
    lifetime. Cancellation cleanup must await actual HTTP/thread/client exit.
    This module does not schedule listeners or select a global current account.
    """

    def __init__(self, registry: AccountRegistry, lifecycle: AccountLifecycle) -> None:
        self._registry = registry
        self._lifecycle = lifecycle
        self._leases: dict[tuple[str, int], set[_Lease]] = {}

    @asynccontextmanager
    async def use(self, account_id: str, *, for_verification: bool = False):
        """Borrow current immutable credentials; True also permits disabled tests.

        for_verification=False requires the current version to be enabled and
        verified. True permits a connection/login test before activation; a
        pending stop rejects both modes. Session strings stay private.
        """
        context = await run_sync(self._registry._context_sync, account_id)
        key = (context.account_id, context.credential_revision)
        lease = _Lease(asyncio.current_task(), asyncio.get_running_loop().create_future())
        self._leases.setdefault(key, set()).add(lease)
        token = None
        context_token = None
        try:
            # Register before the DB check. A stop racing this check either
            # sees and cancels the lease, or the check rejects its intention.
            await run_sync(self._validate_sync, context, for_verification)
            execution = current_execution.get()
            if execution is not None and not for_verification:
                await self._lifecycle.bind_execution(context, execution)
            token = current_account_executions.set((*current_account_executions.get(), AccountExecution(
                self._registry._database.path, context.account_id, context.credential_revision, not for_verification,
            )))
            context_token = current_account_context.set(context)
            yield context
        finally:
            if context_token is not None:
                current_account_context.reset(context_token)
            if token is not None:
                current_account_executions.reset(token)
            leases = self._leases[key]
            leases.remove(lease)
            if not leases:
                del self._leases[key]
            lease.exited.set_result(None)

    def _validate_sync(self, context, for_verification):
        with self._registry._database.connection() as connection:
            row = self._registry._row(connection, context.account_id)
            if (row["stop_operation_id"] is not None
                    or row["credential_revision"] != context.credential_revision
                    or (not for_verification and (not row["enabled"]
                        or row["verified_credential_revision"] != context.credential_revision))):
                raise AccountError("ACCOUNT_UNAVAILABLE", "所选账号版本已停用、改变或正在停止")

    async def stop(self, account_id: str, credential_revision: int) -> bool:
        """Cancel old borrowers and wait for every lease's physical cleanup.

        The coordinator has already durably blocked new borrowing. A lease
        alone cannot prove a remote torrent stopped; the lifecycle separately
        awaits its P3 queue dependencies before committing configuration.
        """
        leases = tuple(self._leases.get((account_id, credential_revision), ()))
        current_task = asyncio.current_task()
        if any(lease.task is current_task for lease in leases):
            raise AccountError("ACCOUNT_STOP_SELF", "停止处理不能在账号请求内部运行")
        for task in {lease.task for lease in leases}:
            # Repeated cancellation can interrupt a provider's finally block.
            if not task.done() and not task.cancelling():
                task.cancel()
        if leases:
            await asyncio.gather(*(asyncio.shield(lease.exited) for lease in leases))
        return True
