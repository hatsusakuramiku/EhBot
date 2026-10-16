"""The unattended half of automatic approval.

Why this exists
---------------
Rules used to fire from exactly one place: `_render_candidates` in
`app/web/routes/candidates.py` called the automatic decision for every row it
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
was restarted. Zero disables full-queue scans. Durable ingestion follow-ups are
still reconciled at startup and on every pass, so a lost callback cannot strand
a committed candidate. The wake event only lowers that reconciliation latency.
"""

from __future__ import annotations

import asyncio
import logging

from app.review.models import REVIEW_APPROVE, REVIEW_REJECT
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
#:
#: The batch is gallery-linked only (R59). Automatic rules never decide a
#: candidate without a gallery id, so letting those fill the window would make
#: it exactly the never-moving kind this ordering exists to avoid: a hundred
#: undecidable rows at the head and every candidate behind them invisible.
SWEEP_BATCH_SIZE = 100


class AutoApprovalSweeper:
    """Applies automatic-approval rules to the pending queue on a timer."""

    def __init__(
        self,
        database,
        orchestrator,
        settings_service: SystemSettingsService,
        metadata_enricher=None,
        notify=None,
    ) -> None:
        self._database = database
        self._orchestrator = orchestrator
        self._settings = settings_service
        # Optional so a deployment without ExHentai, and the tests that drive
        # this class with fakes, keep working unchanged.
        self._metadata_enricher = metadata_enricher
        self._notify = notify
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._sweep_lock = asyncio.Lock()
        # A failed or non-matching candidate must not occupy the first page
        # forever. The cursor is only a scan position; it does not change the
        # candidate's ordering or approval semantics.
        self._sweep_cursor: int | None = None

    def wake(self) -> None:
        """Promptly reconcile durable ingestion intents; no work lives in this event."""
        self._wake.set()

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
        """Recover ingestion immediately; interval zero disables only full scans."""
        interval = None
        next_sweep = None
        while True:
            try:
                await self.process_ingest_followups()
                minutes = await self._settings.auto_approval_interval_minutes()
                now = asyncio.get_running_loop().time()
                if minutes != interval:
                    next_sweep = now + minutes * 60 if minutes > 0 else None
                    interval = minutes
                if next_sweep is not None and now >= next_sweep:
                    await self.sweep_once()
                    next_sweep = asyncio.get_running_loop().time() + minutes * 60
                delay = min(DISABLED_RECHECK_SECONDS, max(0, next_sweep - now)) if next_sweep is not None else DISABLED_RECHECK_SECONDS
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - defensive scheduler loop
                LOGGER.exception(
                    "auto_approval_sweep_failed",
                    extra={"error_code": "AUTO_APPROVAL_SWEEP_FAILED"},
                )
                delay = DISABLED_RECHECK_SECONDS
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except TimeoutError:
                pass
            self._wake.clear()

    async def process_ingest_followups(self) -> int:
        """Drain persisted publication intents, also with periodic scans disabled."""
        from app.candidates.followups import due_followups, finish_followup, followup_ready
        async with self._sweep_lock:
            identities = await asyncio.to_thread(due_followups, self._database, SWEEP_BATCH_SIZE)
            if not identities:
                return 0
            await self._enrich_metadata(tuple(dict.fromkeys(row[3] for row in identities)))
            decided = 0
            for identity in identities:
                candidate_id = identity[3]
                try:
                    decision = await self._orchestrator.apply_automatic_decision(candidate_id)
                    completed = await asyncio.to_thread(followup_ready, self._database, candidate_id)
                    if completed and decision is None:
                        from app.auto_approval.service import AutomaticApprovalService
                        # A still-matching rule declined because its source or
                        # identity changed; keep the intention for a later pass.
                        completed = await AutomaticApprovalService(self._database).matching_rule(candidate_id) is None
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - retain this identity's intent
                    LOGGER.exception('ingest_followup_failed', extra={'candidate_id': candidate_id, 'error_code': 'INGEST_FOLLOWUP_FAILED'})
                    completed, decision = False, None
                await asyncio.to_thread(finish_followup, self._database, identity, completed)
                if decision in {REVIEW_APPROVE, REVIEW_REJECT}:
                    decided += 1
                    if self._notify is not None:
                        self._notify(candidate_id=candidate_id)
            return decided

    async def sweep_once(self) -> int:
        async with self._sweep_lock:
            return await self._sweep_once()

    async def _sweep_once(self) -> int:
        """Apply rules to the pending queue once, returning how many were decided.

        The return value counts both outcomes -- approved plus rejected -- because
        「本轮做了几件事」 is what the caller and the log line want; the two kinds
        are counted apart below so the log can say which happened.

        Public and separately callable because that is what makes the schedule
        testable without waiting for it: a test drives this directly rather than
        starting the task and sleeping. `apply_automatic_decision` is already the
        only path that may decide, and it declines rather than raising when no
        rule matches, so this method does not need to know what a rule is.
        """
        kwargs = {
            "limit": SWEEP_BATCH_SIZE,
            "oldest_first": True,
            "require_gallery": True,
        }
        if self._sweep_cursor is not None:
            kwargs["after_id"] = self._sweep_cursor
        candidate_ids = await self._database.pending_candidate_ids(**kwargs)
        if not candidate_ids:
            self._sweep_cursor = None
            return 0
        self._sweep_cursor = candidate_ids[-1]
        # Metadata before rules, for the whole batch in one call. A rule reads
        # metadata: 「{Category} = 'Manga'」 cannot match a candidate whose gallery
        # has not been read, so an unenriched batch is not 「no rule matched」 --
        # it is a decision made on absent evidence. Enrichment used to happen
        # only when an operator opened 待审核, which meant the unattended sweep
        # judged exactly the candidates nobody had looked at yet.
        #
        # Batched rather than per-candidate because gdata takes 25 galleries per
        # request, and one pass over a hundred candidates should be a handful of
        # requests rather than a hundred.
        await self._enrich_metadata(candidate_ids)
        approved = 0
        rejected = 0
        for candidate_id in candidate_ids:
            try:
                decision = await self._orchestrator.apply_automatic_decision(
                    candidate_id
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one candidate must not stop the sweep
                # A single undecidable candidate -- an enqueue whose provider is
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
                continue
            if decision == REVIEW_APPROVE:
                approved += 1
            elif decision == REVIEW_REJECT:
                rejected += 1
            if decision in {REVIEW_APPROVE, REVIEW_REJECT} and self._notify is not None:
                self._notify(candidate_id=candidate_id)
        if approved or rejected:
            # Logged only when something happened: a sweep that decides nothing
            # is the normal case and would otherwise write a line every 30
            # minutes forever, burying the ones that mean something. The two
            # counts are split so a silently-firing reject rule is visible.
            LOGGER.info(
                "auto_approval_sweep_completed approved=%d rejected=%d scanned=%d",
                approved,
                rejected,
                len(candidate_ids),
            )
        return approved + rejected

    async def _enrich_metadata(self, candidate_ids: tuple[int, ...]) -> None:
        """Fill in missing gallery metadata for a batch, tolerating failure.

        Best-effort on purpose. ExHentai being unreachable is a reason for a
        sweep to decide less, not a reason for it to stop: candidates a rule
        cannot judge stay pending and are re-examined next pass, which is what
        already happens to a candidate no rule matches.
        """
        if self._metadata_enricher is None:
            return
        try:
            await self._metadata_enricher(candidate_ids)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - enrichment is best-effort
            LOGGER.exception(
                "auto_approval_metadata_enrichment_failed",
                extra={"error_code": "AUTO_APPROVAL_METADATA_FAILED"},
            )


__all__ = [
    "DISABLED_RECHECK_SECONDS",
    "SWEEP_BATCH_SIZE",
    "AutoApprovalSweeper",
]
