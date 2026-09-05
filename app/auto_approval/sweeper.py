"""The unattended half of automatic approval.

Why this exists
---------------
Rules used to fire from exactly one place: `_render_candidates` in
`app/web/routes/candidates.py` called `apply_automatic_approval` for every row it
was about to draw on the 待审核 tab. That made approval a side effect of
*rendering a page*, with three consequences an operator eventually hits.

* A deployment nobody is watching approves nothing. The whole point of a rule is
  that it does not need a human, and this one needed a human to open a tab.
* Only the page an operator happened to be on was swept. The tab is paginated,
  so candidates past the first page waited for somebody to scroll to them.
* The 待审核 tab took the cost of the sweep -- N metadata reads plus a possible
  approve-and-enqueue each -- inside a request the operator was waiting on.

So the sweep moves here, into a task that owns the schedule, and the page render
keeps its call as a latency optimisation: a candidate that arrived two seconds
ago is approved before the operator has finished reading the row, rather than at
the next tick.

Shape
-----
One `asyncio.Task`, started and stopped by the lifespan the way
`DownloadService`'s worker is, and modelled on it deliberately -- an interval
loop that catches its own exceptions is the pattern this codebase already has,
and a second scheduling mechanism would be a second thing to reason about when a
sweep stops happening.

The interval is re-read from the settings service on every pass rather than
captured at construction: it is operator-editable on the 系统 tab, and a value
captured at startup would mean a saved interval did nothing until the container
was restarted. Zero means 「不要自动跑」, and it is honoured by sleeping the poll
interval and re-checking rather than by exiting the loop, so turning the sweep
back on also takes effect without a restart.
"""

from __future__ import annotations

import asyncio
import logging

from app.settings.service import SystemSettingsService


LOGGER = logging.getLogger(__name__)

#: How long the loop waits before re-reading a `0` interval. It is not the sweep
#: cadence -- it is how quickly the sweep starts again after an operator turns it
#: back on, and a minute is short enough to feel immediate without making a
#: disabled sweeper a busy loop.
DISABLED_RECHECK_SECONDS = 60.0

#: Candidates examined per pass. The same ceiling `pending_candidate_ids`
#: defaults to, named here because this caller is the reason it matters: a
#: backlog larger than this is swept across several passes rather than in one
#: long transaction, which keeps a first run on a big database from holding the
#: event loop while it approves five thousand books.
#:
#: Several passes only work because the batch is read oldest-first. Newest-first
#: plus a limit is a window that never moves: a candidate no rule matches sits at
#: the top of it forever and everything older than the hundredth row is never
#: evaluated at all. Oldest-first makes the ceiling a queue -- the candidate that
#: has waited longest is the one examined next.
SWEEP_BATCH_SIZE = 100


class AutoApprovalSweeper:
    """Applies automatic-approval rules to the pending queue on a timer."""

    def __init__(
        self,
        database,
        orchestrator,
        settings_service: SystemSettingsService,
    ) -> None:
        self._database = database
        self._orchestrator = orchestrator
        self._settings = settings_service
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(
            self._run(), name="auto-approval-sweeper"
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    async def _run(self) -> None:
        """Sweep, sleep, repeat -- with the interval re-read each time.

        The first sweep waits one interval rather than running at startup. A
        deployment that has just come up is the least likely moment for the
        queue to have changed, and sweeping immediately would mean every
        container restart re-scans the whole pending queue.
        """
        while True:
            try:
                minutes = await self._settings.auto_approval_interval_minutes()
            except Exception:  # noqa: BLE001 - defensive scheduler loop
                # A settings read that fails must not kill the sweeper: the
                # default cadence is a better answer than never running again.
                LOGGER.exception(
                    "auto_approval_interval_unreadable",
                    extra={"error_code": "AUTO_APPROVAL_INTERVAL_UNREADABLE"},
                )
                minutes = 0
            if minutes <= 0:
                await asyncio.sleep(DISABLED_RECHECK_SECONDS)
                continue
            await asyncio.sleep(minutes * 60)
            try:
                await self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - defensive scheduler loop
                LOGGER.exception(
                    "auto_approval_sweep_failed",
                    extra={"error_code": "AUTO_APPROVAL_SWEEP_FAILED"},
                )

    async def sweep_once(self) -> int:
        """Apply rules to the pending queue once, returning how many approved.

        Public and separately callable because that is what makes the schedule
        testable without waiting for it: a test drives this directly rather than
        starting the task and sleeping. `apply_automatic_approval` is already the
        only path that may approve, and it declines rather than raising when no
        rule matches, so this method does not need to know what a rule is.
        """
        candidate_ids = await self._database.pending_candidate_ids(
            limit=SWEEP_BATCH_SIZE, oldest_first=True
        )
        if not candidate_ids:
            return 0
        approved = 0
        for candidate_id in candidate_ids:
            try:
                if await self._orchestrator.apply_automatic_approval(
                    candidate_id
                ):
                    approved += 1
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one candidate must not stop the sweep
                # A single unapprovable candidate -- an enqueue whose provider is
                # misconfigured, a metadata row that will not parse -- is a
                # reason to skip that candidate, not to abandon the other
                # ninety-nine. The candidate stays pending for a human.
                LOGGER.exception(
                    "auto_approval_candidate_failed",
                    extra={
                        "candidate_id": candidate_id,
                        "error_code": "AUTO_APPROVAL_CANDIDATE_FAILED",
                    },
                )
        if approved:
            # Logged only when something happened: a sweep that approves nothing
            # is the normal case and would otherwise write a line every 30
            # minutes forever, burying the ones that mean something.
            LOGGER.info(
                "auto_approval_sweep_completed approved=%d scanned=%d",
                approved,
                len(candidate_ids),
            )
        return approved


__all__ = [
    "DISABLED_RECHECK_SECONDS",
    "SWEEP_BATCH_SIZE",
    "AutoApprovalSweeper",
]
