"""按画廊 ID 的一键去重（R59）。

Why this exists
---------------
One gallery is one work, and the identity the upstream gives us is its numeric
gallery id. Ingestion enforces that from now on (`CandidateIngestor`), but a
damaged or externally edited database can still contain duplicate rows.
P5 reconciles historical rows and enforces a database unique index. This
module remains the operator-facing repair: group the rows
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
* **Running removal uses the same durable purge as every other client.** It
  retains independent stop/file operations rather than issuing a preliminary
  cancel that could race completion or release a slot too early.
"""

from __future__ import annotations

import json
from hashlib import sha256
from uuid import uuid4
from collections.abc import Iterable
from dataclasses import dataclass

from app.candidates.models import display_title
from app.downloads.execution import run_sync
from app.review.application import remove_work
from app.review.batches import ReviewBatches
from app.review.commands import WorkCommandError, WorkCommands
from app.review.service import ReviewError
from app.downloads.archived import ArchivedWorkError
from app.conversion.convert import ConversionError


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
    def display_title(self) -> str:
        """The name to show in the group table (R62). Every entry here has a
        gallery id -- that is what makes it a duplicate -- so an untitled one
        reads as `#<gid>` rather than as another 「未命名作品」."""
        return display_title(self.title, self.ex_gid, self.candidate_id)

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
    """Persist the exact read snapshot for an explicit later confirmation."""
    return await run_sync(_build_plan, database)


def _build_plan(database):
    with database.connection() as connection:
        connection.execute('BEGIN IMMEDIATE')
        rows = database._duplicate_gallery_groups_sync(connection)
        groups = group_candidates(rows)
        versions = {row['candidate_id']: {'candidate_id': row['candidate_id'], 'work_id': row['work_id'],
                    'expected_revision': row['expected_revision']} for row in rows}
        plan = {'plan_id': uuid4().hex, 'groups': [group.payload() for group in groups],
                'group_count': len(groups), 'removable': sum(len(group.remove) for group in groups)}
        for group in plan['groups']:
            for entry in [group['keep'], *group['remove']]:
                entry.update(versions[entry['candidate_id']])
        connection.execute('INSERT INTO work_dedup_plans(plan_id, plan_json) VALUES(?, ?)',
                           (plan['plan_id'], json.dumps(plan)))
        return plan


async def read_dedup_plan(database, plan_id):
    def read():
        with database.connection() as connection:
            row = connection.execute('SELECT plan_json FROM work_dedup_plans WHERE plan_id = ?', (plan_id,)).fetchone()
            if row is None:
                raise WorkCommandError('DEDUP_PLAN_NOT_FOUND', '去重预览不存在，请重新预览', status_code=404)
            return json.loads(row[0])
    return await run_sync(read)


async def apply_dedup(database, archived_service, download_service, *, plan_id=None,
                      idempotency_key=None, operator_name='admin', announce=None):
    """Resume immutable per-row removals from the confirmed preview only."""
    if not isinstance(plan_id, str) or not plan_id:
        raise WorkCommandError('PRECONDITION_REQUIRED', '请提交已确认的去重预览编号', status_code=428)
    WorkCommands._preconditions({'work_id': plan_id, 'expected_revision': 1, 'idempotency_key': idempotency_key})
    digest = sha256(json.dumps({'action': 'dedup', 'plan_id': plan_id}, sort_keys=True).encode()).hexdigest()
    plan = await read_dedup_plan(database, plan_id)
    batch = ReviewBatches(database, archived=archived_service)
    receipt = await run_sync(batch._begin, idempotency_key, digest, {'action': 'dedup', 'plan_id': plan_id})
    if receipt is not None:
        return receipt
    removed, skipped = [], []
    for group in plan['groups']:
        keeper = {name: group['keep'][name] for name in ('candidate_id', 'work_id', 'expected_revision')}
        for loser in group['remove']:
            candidate_id = loser['candidate_id']
            key = 'dedup:' + sha256((idempotency_key + ':' + str(candidate_id)).encode()).hexdigest()
            payload = {name: loser[name] for name in ('work_id', 'expected_revision')}
            payload.update(idempotency_key=key, keeper=keeper)
            try:
                result = await remove_work(database, archived_service, candidate_id, 'delete-files', payload, operator_name)
            except (ReviewError, ArchivedWorkError, ConversionError) as exc:
                result = await run_sync(batch._refusal, candidate_id, 'delete-files', payload, exc)
            if 'batch_error' in result:
                skipped.append({'candidate_id': candidate_id, 'ex_gid': group['ex_gid'], **result['batch_error']})
            else:
                removed.append({'candidate_id': candidate_id, 'ex_gid': group['ex_gid'],
                    'kept_candidate_id': keeper['candidate_id'], 'reason': loser['reason'], 'cancelled_jobs': 0,
                    'removal_id': result['removal_id'], 'cleanup_status': result['cleanup_status']})
                if announce is not None:
                    announce(candidate_id)
    return await run_sync(batch._complete, idempotency_key, digest,
        {'plan_id': plan_id, 'groups': plan['group_count'],
         'kept': [group['keep']['candidate_id'] for group in plan['groups']], 'removed': removed, 'skipped': skipped})


__all__ = [
    "DedupEntry",
    "DedupGroup",
    "apply_dedup",
    "build_dedup_plan",
    "group_candidates",
    "removal_reason",
]
