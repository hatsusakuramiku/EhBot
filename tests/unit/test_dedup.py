"""The 一键去重 ranking (R59), as pure functions.

`group_candidates` and `removal_reason` carry the operator's three rules
("已完成打包优先 / 页数多者优先 / 最旧者优先"), so they are tested without a
database: a bug here deletes the wrong book.
"""

from __future__ import annotations



from app.downloads.dedup import group_candidates, removal_reason


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
