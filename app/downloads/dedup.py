"""按画廊 ID 的一键去重（R59）。

Why this exists
---------------
One gallery is one work, and the identity the upstream gives us is its numeric
gallery id. Ingestion enforces that from now on (`CandidateIngestor`), but a
database that has been running for a while still holds rows from before the
rule existed -- and a race between the two ingestion channels can always slip
one past the gate. This module is the operator-facing repair: group the rows
that share a gallery id, keep one of each group, remove the rest.

The keeping order is the operator's, and it is a *total* order so a group
never has to be decided by anything else:

1. a work that finished packaging beats one that did not;
2. among packaged works, more pages wins;
3. with equal pages, the oldest (smallest candidate id) wins.

Two things worth knowing before editing:

* **Removal is `purge_work`, not a second delete.** The audit row, the
  in-flight guards and the path-inside-the-library check all live there; a
  dedup pass that reimplemented them would be a second definition of "delete a
  work" and would drift.
* **A loser's in-flight download is cancelled here, not by the operator.** The
  whole point is one click, and `purge_work` refuses a work whose job is still
  open. A *packaging* job is different: the worker holds that row and there is
  no cancel for it, so that one loser is reported as skipped rather than
  half-deleted -- the next pass clears it once the pack has finished.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from app.downloads.models import (
    DOWNLOAD_STATE_CANCELLED,
    DOWNLOAD_STATE_COMPLETED,
    PROVIDER_CONVERSION,
)


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DedupEntry:
    """One candidate in a duplicate group, with what the ranking reads."""

    candidate_id: int
    ex_gid: int
    status: str
    title: str | None
    packaged: bool
    page_count: int | None

    @property
    def keep_key(self) -> tuple[int, int, int]:
        """The operator's order, as a tuple `max()` can pick a winner with.

        Sorted descending, so a packaged work (1) beats an unpacked one (0),
        more pages beats fewer (`None` counts as -1: a page count only exists
        once a CBZ does), and the negated id makes the oldest win the tie.
        """
        pages = self.page_count if self.page_count is not None else -1
        return (1 if self.packaged else 0, pages, -self.candidate_id)

    def payload(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "ex_gid": self.ex_gid,
            "status": self.status,
            "title": self.title,
            "packaged": self.packaged,
            "page_count": self.page_count,
        }


@dataclass(frozen=True, slots=True)
class DedupGroup:
    """One gallery id with more than one candidate, and the decision."""

    ex_gid: int
    keep: DedupEntry
    remove: tuple[DedupEntry, ...]

    def payload(self) -> dict:
        return {
            "ex_gid": self.ex_gid,
            "keep": {
                **self.keep.payload(),
                "reason": "保留：已打包 / 页数较多 / 最早入库",
            },
            "remove": [
                {**entry.payload(), "reason": removal_reason(entry, self.keep)}
                for entry in self.remove
            ],
        }


def removal_reason(loser: DedupEntry, keeper: DedupEntry) -> str:
    """Why this candidate is the one that goes, in the operator's words."""
    if keeper.packaged and not loser.packaged:
        return "未完成下载或打包"
    keeper_pages = keeper.page_count if keeper.page_count is not None else 0
    loser_pages = loser.page_count if loser.page_count is not None else 0
    if keeper.packaged and loser.packaged and loser_pages < keeper_pages:
        return f"页数较少（{loser_pages} < {keeper_pages}）"
    return "入库较晚"


def group_candidates(rows: Iterable[dict]) -> list[DedupGroup]:
    """Group `duplicate_gallery_groups` rows and pick one winner per group.

    Pure, so the ranking can be tested without a database. The rows arrive
    ordered by `(ex_gid, id)`, but the grouping does not depend on that: it
    only needs every member of a group to be present.
    """
    buckets: dict[int, list[DedupEntry]] = {}
    for row in rows:
        entry = DedupEntry(
            candidate_id=int(row["candidate_id"]),
            ex_gid=int(row["ex_gid"]),
            status=str(row["status"]),
            title=row["title"],
            packaged=bool(row["packaged"]),
            page_count=(
                int(row["page_count"]) if row["page_count"] is not None else None
            ),
        )
        buckets.setdefault(entry.ex_gid, []).append(entry)
    groups: list[DedupGroup] = []
    for ex_gid, entries in buckets.items():
        if len(entries) < 2:
            continue
        ordered = sorted(entries, key=lambda entry: entry.keep_key, reverse=True)
        groups.append(
            DedupGroup(ex_gid=ex_gid, keep=ordered[0], remove=tuple(ordered[1:]))
        )
    groups.sort(key=lambda group: group.ex_gid)
    return groups


async def build_dedup_plan(database) -> dict:
    """Every duplicate group with its kept and removed rows, and no writes."""
    groups = group_candidates(await database.duplicate_gallery_groups())
    return {
        "groups": [group.payload() for group in groups],
        "group_count": len(groups),
        "removable": sum(len(group.remove) for group in groups),
    }


async def _cancel_open_downloads(download_service, candidate_id: int) -> int:
    """Cancel the loser's unfinished downloads so the purge guard lets it go.

    Terminal rows and the packaging task are left alone: a completed download
    is not in flight, and a packaging task is not cancellable from here (its
    worker holds the row -- see the module docstring). Returns how many were
    cancelled, which the caller reports rather than silently swallowing.
    """
    cancelled = 0
    for job in await download_service.list_jobs_for_candidate(candidate_id):
        if job.provider == PROVIDER_CONVERSION:
            continue
        if job.state in (DOWNLOAD_STATE_COMPLETED, DOWNLOAD_STATE_CANCELLED):
            continue
        await download_service.cancel_job(job.job_id)
        cancelled += 1
    return cancelled


async def apply_dedup(
    database,
    archived_service,
    download_service,
    *,
    operator_name: str = "admin",
    announce: Callable[[int], None] | None = None,
) -> dict:
    """Remove every duplicate beyond each group's winner. Reports per row.

    A genuine fault is re-raised rather than folded into `skipped`, the same
    rule `apply_downloaded_batch` follows: a broken filesystem must not read as
    「12 件已移除」. A domain refusal (the packaging task still running) is a
    skip with its reason attached, because the operator's next action is to run
    this again rather than to repair anything.
    """
    groups = group_candidates(await database.duplicate_gallery_groups())
    removed: list[dict] = []
    skipped: list[dict] = []
    for group in groups:
        for loser in group.remove:
            reason = removal_reason(loser, group.keep)
            try:
                cancelled = await _cancel_open_downloads(
                    download_service, loser.candidate_id
                )
                await archived_service.purge_work(
                    loser.candidate_id,
                    delete_files=True,
                    operator_name=operator_name,
                )
            except Exception as exc:  # noqa: BLE001 - re-raised when unexpected
                code = getattr(exc, "code", None)
                message = getattr(exc, "public_message", None)
                if code is None or message is None:
                    raise
                skipped.append(
                    {
                        "candidate_id": loser.candidate_id,
                        "ex_gid": group.ex_gid,
                        "code": str(code),
                        "message": str(message),
                    }
                )
                continue
            removed.append(
                {
                    "candidate_id": loser.candidate_id,
                    "ex_gid": group.ex_gid,
                    "kept_candidate_id": group.keep.candidate_id,
                    "reason": reason,
                    "cancelled_jobs": cancelled,
                }
            )
            if announce is not None:
                announce(loser.candidate_id)
    LOGGER.info(
        "gallery_dedup_completed groups=%d removed=%d skipped=%d",
        len(groups),
        len(removed),
        len(skipped),
    )
    return {
        "groups": len(groups),
        "kept": [group.keep.candidate_id for group in groups],
        "removed": removed,
        "skipped": skipped,
    }


__all__ = [
    "DedupEntry",
    "DedupGroup",
    "apply_dedup",
    "build_dedup_plan",
    "group_candidates",
    "removal_reason",
]
