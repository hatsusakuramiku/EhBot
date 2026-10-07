"""The 一键去重 ranking (R59), as pure functions.

`group_candidates` and `removal_reason` carry the operator's three rules
("已完成打包优先 / 页数多者优先 / 最旧者优先"), so they are tested without a
database: a bug here deletes the wrong book.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.downloads.archived import ArchivedWorkError
from app.downloads.dedup import apply_dedup, group_candidates, removal_reason


def _row(
    candidate_id: int,
    *,
    ex_gid: int = 1,
    packaged: bool = False,
    page_count: int | None = None,
    status: str = "DOWNLOADED",
    title: str | None = None,
) -> dict:
    return {
        "candidate_id": candidate_id,
        "ex_gid": ex_gid,
        "status": status,
        "title": title or f"Book {candidate_id}",
        "packaged": packaged,
        "page_count": page_count,
    }


def test_a_packaged_work_beats_an_unpackaged_one_even_with_fewer_pages() -> None:
    """Rule 1 outranks rule 2: finished beats everything."""
    groups = group_candidates(
        [
            _row(1, packaged=True, page_count=5),
            _row(2, packaged=False),
        ]
    )
    assert len(groups) == 1
    assert groups[0].keep.candidate_id == 1
    assert [entry.candidate_id for entry in groups[0].remove] == [2]


def test_more_pages_wins_among_packaged_works() -> None:
    groups = group_candidates(
        [
            _row(1, packaged=True, page_count=200),
            _row(2, packaged=True, page_count=42),
        ]
    )
    assert groups[0].keep.candidate_id == 1
    assert [entry.candidate_id for entry in groups[0].remove] == [2]


def test_the_oldest_wins_when_the_pages_are_equal() -> None:
    """`id` is the arrival order, so the smallest is the one that came first."""
    groups = group_candidates(
        [
            _row(9, packaged=True, page_count=12),
            _row(3, packaged=True, page_count=12),
            _row(7, packaged=True, page_count=12),
        ]
    )
    assert groups[0].keep.candidate_id == 3
    assert [entry.candidate_id for entry in groups[0].remove] == [7, 9]


def test_the_oldest_wins_when_nobody_is_packaged() -> None:
    groups = group_candidates([_row(4), _row(2)])
    assert groups[0].keep.candidate_id == 2


def test_a_missing_page_count_is_treated_as_the_smallest() -> None:
    """Only a packed work has a page count, so `None` must not outrank 0."""
    groups = group_candidates(
        [
            _row(1, packaged=True, page_count=None),
            _row(2, packaged=True, page_count=1),
        ]
    )
    assert groups[0].keep.candidate_id == 2


def test_a_lone_candidate_is_not_a_group() -> None:
    assert group_candidates([_row(1)]) == []


def test_distinct_galleries_are_grouped_apart() -> None:
    groups = group_candidates(
        [
            _row(1, ex_gid=100),
            _row(2, ex_gid=100),
            _row(3, ex_gid=200),
            _row(4, ex_gid=200),
        ]
    )
    assert [group.ex_gid for group in groups] == [100, 200]
    assert [group.keep.candidate_id for group in groups] == [1, 3]


def test_removal_reasons_name_the_rule_that_lost() -> None:
    keeper = group_candidates(
        [_row(1, packaged=True, page_count=50), _row(2)]
    )[0].keep
    unpacked = group_candidates(
        [_row(1, packaged=True, page_count=50), _row(2)]
    )[0].remove[0]
    assert removal_reason(unpacked, keeper) == "未完成下载或打包"

    same_gallery = group_candidates(
        [
            _row(1, packaged=True, page_count=50),
            _row(2, packaged=True, page_count=9),
        ]
    )[0]
    assert removal_reason(same_gallery.remove[0], same_gallery.keep) == (
        "页数较少（9 < 50）"
    )

    equal = group_candidates(
        [
            _row(1, packaged=True, page_count=50),
            _row(2, packaged=True, page_count=50),
        ]
    )[0]
    assert removal_reason(equal.remove[0], equal.keep) == "入库较晚"


# ------------------------------------------------------- the removal pass


def _job(job_id: int, provider: str, state: str) -> SimpleNamespace:
    return SimpleNamespace(job_id=job_id, provider=provider, state=state)


class _FakeDatabase:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    async def duplicate_gallery_groups(self) -> list[dict]:
        return list(self._rows)


class _FakeArchived:
    def __init__(self, refusals: dict[int, Exception] | None = None) -> None:
        self.purged: list[tuple[int, bool, str]] = []
        self._refusals = refusals or {}

    async def purge_work(
        self,
        candidate_id: int,
        *,
        delete_files: bool = False,
        operator_name: str = "admin",
    ) -> dict:
        if candidate_id in self._refusals:
            raise self._refusals[candidate_id]
        self.purged.append((candidate_id, delete_files, operator_name))
        return {"candidate_id": candidate_id}


class _FakeDownloads:
    def __init__(self, jobs: dict[int, tuple]) -> None:
        self._jobs = jobs
        self.cancelled: list[int] = []

    async def list_jobs_for_candidate(self, candidate_id: int):
        return tuple(self._jobs.get(candidate_id, ()))

    async def cancel_job(self, job_id: int) -> str:
        self.cancelled.append(job_id)
        return "CANCELLED"


def test_apply_dedup_cancels_open_downloads_then_purges_with_files() -> None:
    """The cancel is what makes one click enough: purge refuses an open job."""
    rows = [_row(1, packaged=True, page_count=20), _row(2)]
    downloads = _FakeDownloads(
        {
            2: (
                _job(11, "TELEGRAM", "PENDING"),
                _job(12, "CONVERSION", "CONVERSION_COMPLETED"),
            )
        }
    )
    archived = _FakeArchived()
    announced: list[int] = []

    result = asyncio.run(
        apply_dedup(
            _FakeDatabase(rows),
            archived,
            downloads,
            operator_name="tester",
            announce=announced.append,
        )
    )

    # Only the download job is cancelled; the packaging task is not cancellable
    # from here and must be left to its worker.
    assert downloads.cancelled == [11]
    assert archived.purged == [(2, True, "tester")]
    assert result["removed"] == [
        {
            "candidate_id": 2,
            "ex_gid": 1,
            "kept_candidate_id": 1,
            "reason": "未完成下载或打包",
            "cancelled_jobs": 1,
        }
    ]
    assert result["kept"] == [1]
    assert result["skipped"] == []
    assert announced == [2]


def test_apply_dedup_skips_a_refused_removal_and_keeps_the_rest() -> None:
    """A running pack is a skip with a reason, not a half-deleted work."""
    rows = [_row(1, packaged=True), _row(2), _row(3)]
    refusal = ArchivedWorkError("WORK_PACK_RUNNING", "该作品正在打包")
    archived = _FakeArchived(refusals={2: refusal})

    result = asyncio.run(
        apply_dedup(_FakeDatabase(rows), archived, _FakeDownloads({}))
    )

    assert [entry[0] for entry in archived.purged] == [3]
    assert result["skipped"] == [
        {
            "candidate_id": 2,
            "ex_gid": 1,
            "code": "WORK_PACK_RUNNING",
            "message": "该作品正在打包",
        }
    ]
    assert [entry["candidate_id"] for entry in result["removed"]] == [3]


def test_apply_dedup_re_raises_a_fault_that_is_not_a_refusal() -> None:
    """A broken filesystem must not read as 「1 件跳过」."""

    class _Exploding(_FakeArchived):
        async def purge_work(self, candidate_id: int, **kwargs) -> dict:
            raise RuntimeError("disk on fire")

    with pytest.raises(RuntimeError):
        asyncio.run(
            apply_dedup(
                _FakeDatabase([_row(1, packaged=True), _row(2)]),
                _Exploding(),
                _FakeDownloads({}),
            )
        )
