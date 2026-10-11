"""Read a new candidate's gallery without an operator having to open a page.

Why this exists
---------------
Since R62 a work's name comes from its gallery page and nowhere else -- the
caption is not parsed for one -- and a candidate with no name waits in 待补充.
Enrichment used to happen on three unattended-ish paths (opening the candidate
list, the approval sweeper's batch, and just before packing), so a candidate
nobody opened simply sat in 待补充 with the answer one HTTP call away. The
comment on `ExHentaiService.enrich_missing_metadata` even names the cause: rules
were judged on absent evidence because nothing read the gallery first.

So the scrape gets its own task, woken by ingestion, the way the approval sweeper
got its own task once approving as a side effect of rendering a page turned out
to be the bug (see `app/auto_approval/sweeper.py`).

Shape
-----
One `asyncio.Task` started and stopped by the lifespan, its own interval loop,
and a scan of the database rather than an in-memory queue. The queue would be
tighter, but the ingestion hook (`ConnectionManager.on_candidates_ingested`) is a
no-argument callback -- it announces that *something* arrived, not which rows --
and a scan also picks up the backlog a deploy inherits. Scanning newest-first
means the candidate that just arrived is the one read first.

Failure
-------
A gallery that cannot be read -- deleted, login-walled, an outage -- leaves no
`EXHENTAI` row, so the scan would offer it again forever. Attempts are therefore
counted per candidate in memory with a growing delay (30s, 2m, 10m) and then
given up until the process restarts; opening the candidate's page or the next
sweep still retries by the other paths. The state is deliberately in memory: the
proposal requires no migration, and a restart re-trying a handful of failures is
a cheaper answer than a retry column nobody would prune.
"""

from __future__ import annotations

import asyncio
import logging
import time

from app.db.database import Database


LOGGER = logging.getLogger(__name__)

#: Candidates per pass. Small: gdata takes 25 galleries per request, and a pass
#: that reads a batch is meant to finish long before the next one is due.
SCRAPE_BATCH_SIZE = 25

#: How long the loop waits with nothing to do. Not the latency an operator sees
#: -- ingestion wakes the loop immediately -- it is how quickly a backlog left
#: by a restart or a failure starts moving again.
IDLE_SECONDS = 30.0

#: Delay before re-offering a gallery that came back empty. Indexed by attempt
#: number; the last value repeats, so a persistent failure settles at ten
#: minutes rather than a hot loop.
RETRY_BACKOFF_SECONDS = (30.0, 120.0, 600.0)

#: Statuses worth reading a gallery for. A candidate the operator approved,
#: rejected or that is already being packed has moved past the question the
#: scrape answers, and 待补充 is where an unread candidate waits.
SCRAPE_STATUSES = ("NEEDS_INFO", "PENDING_REVIEW", "NEEDS_REVISION")


class MetadataScrapeWorker:
    """Reads gallery metadata for candidates that still have none."""

    def __init__(
        self,
        database: Database,
        scrape,
        *,
        batch_size: int = SCRAPE_BATCH_SIZE,
        idle_seconds: float = IDLE_SECONDS,
    ) -> None:
        self._database = database
        # `scrape(candidate_ids)` is `ExHentaiService.enrich_missing_metadata`
        # through the wiring guard; injected rather than imported so a test can
        # drive this class without an HTTP client, and so a deployment without
        # ExHentai credentials simply passes a no-op.
        self._scrape = scrape
        self._batch_size = batch_size
        self._idle_seconds = idle_seconds
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._attempts: dict[int, int] = {}
        self._retry_at: dict[int, float] = {}

    def wake(self) -> None:
        """Run a pass as soon as the loop is free, without waiting the interval.

        Called by the ingestion hook. Safe to call from a task and before
        `start()`: the event keeps the wake until the loop first waits on it.
        """
        self._wake.set()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(
            self._run(), name="metadata-scrape-worker"
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    async def _run(self) -> None:
        while True:
            try:
                try:
                    await asyncio.wait_for(
                        self._wake.wait(), timeout=self._idle_seconds
                    )
                except asyncio.TimeoutError:
                    pass
                self._wake.clear()
                await self.scrape_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - defensive worker loop
                LOGGER.exception(
                    "metadata_scrape_failed",
                    extra={"error_code": "METADATA_SCRAPE_FAILED"},
                )

    async def scrape_once(self) -> int:
        """Read every due gallery once, returning how many succeeded.

        Public so a test drives this directly instead of sleeping on the loop.
        """
        candidates = await asyncio.to_thread(self._due_candidates_sync)
        if not candidates:
            return 0
        await self._scrape(candidates)
        missing = await asyncio.to_thread(
            self._still_missing_sync, tuple(candidates)
        )
        now = time.monotonic()
        for candidate_id in candidates:
            if candidate_id in missing:
                attempt = self._attempts.get(candidate_id, 0) + 1
                self._attempts[candidate_id] = attempt
                delay = RETRY_BACKOFF_SECONDS[
                    min(attempt, len(RETRY_BACKOFF_SECONDS)) - 1
                ]
                self._retry_at[candidate_id] = now + delay
            else:
                self._attempts.pop(candidate_id, None)
                self._retry_at.pop(candidate_id, None)
        return len(candidates) - len(missing)

    def _due_candidates_sync(self) -> list[int]:
        """The newest candidates with a gallery reference and no metadata.

        Newest-first: an arriving candidate is read before an old one that has
        been failing. A candidate still in its retry delay is filtered out here
        in Python -- the backoff is per process and deliberately not in the
        database -- and the window is widened past the batch size so a few
        backed-off rows at the head cannot hide the rest of the queue.
        """
        with self._database.connection() as connection:
            rows = connection.execute(
                "SELECT c.id FROM candidates c "
                "WHERE c.ex_gid IS NOT NULL AND c.ex_gallery_token IS NOT NULL "
                "AND c.status IN (" + ",".join("?" for _ in SCRAPE_STATUSES) + ") "
                "AND NOT EXISTS (SELECT 1 FROM metadata_values mv "
                "WHERE mv.candidate_id = c.id AND mv.value_source = 'EXHENTAI') "
                "ORDER BY c.created_at DESC, c.id DESC LIMIT ?",
                (*SCRAPE_STATUSES, self._batch_size * 4),
            ).fetchall()
        now = time.monotonic()
        due = [
            int(row[0])
            for row in rows
            if self._retry_at.get(int(row[0]), 0.0) <= now
        ]
        return due[: self._batch_size]

    def _still_missing_sync(self, candidate_ids: tuple[int, ...]) -> set[int]:
        if not candidate_ids:
            return set()
        placeholders = ",".join("?" for _ in candidate_ids)
        with self._database.connection() as connection:
            rows = connection.execute(
                "SELECT DISTINCT candidate_id FROM metadata_values "
                "WHERE value_source = 'EXHENTAI' AND candidate_id IN ("
                + placeholders
                + ")",
                candidate_ids,
            ).fetchall()
        enriched = {int(row[0]) for row in rows}
        return {candidate_id for candidate_id in candidate_ids
                if candidate_id not in enriched}


__all__ = ["MetadataScrapeWorker"]
