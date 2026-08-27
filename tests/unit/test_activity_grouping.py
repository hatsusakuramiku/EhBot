"""Queue grouping, the needs-attention roll-up, and the transfer line (R4).

These are unit tests over `app.api.activity` and `app.api.serializers` rather
than over the page, because the thing worth pinning is the *policy*: which
section a job lands in, whether a stalled torrent counts as a failure, and what
the queue is allowed to say about a transfer. The page renders whatever these
return, and `tests/integration/test_activity_web.py` covers that it does.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.api.activity import attention_summary, group_jobs, queue_snapshot
from app.api.serializers import job_summary
from app.downloads.models import (
    CONVERSION_STATE_WAITING_PASSWORD,
    CONVERSION_STATE_WAITING_VOLUMES,
    DEFAULT_JOB_PRIORITY,
    PROVIDER_CONVERSION,
    PROVIDER_EH_TORRENT,
    DownloadJobSummary,
)


def make_job(
    job_id: int = 1,
    *,
    state: str = "PENDING",
    provider: str = "TELEGRAM",
    error_code: str | None = None,
    details: dict | None = None,
    priority: int = DEFAULT_JOB_PRIORITY,
) -> DownloadJobSummary:
    return DownloadJobSummary(
        job_id=job_id,
        candidate_id=100 + job_id,
        provider=provider,
        state=state,
        attempt_count=0,
        error_code=error_code,
        error_message=None,
        artifact_path=None,
        artifact_size=None,
        created_at="2026-08-26T00:00:00Z",
        updated_at="2026-08-26T00:00:00Z",
        details=details or {},
        priority=priority,
    )


def stalled_torrent(job_id: int = 1, *, minutes: int = 30) -> DownloadJobSummary:
    """A torrent with no seeder, stalled for `minutes`."""
    return make_job(
        job_id,
        state="WAITING_TORRENT",
        provider=PROVIDER_EH_TORRENT,
        details={
            "progress": 0.42,
            "num_seeds": 0,
            "stalled_since": time.time() - minutes * 60,
        },
    )


# --------------------------------------------------------------- grouping


def test_each_state_lands_in_its_own_section() -> None:
    sections = group_jobs(
        [
            make_job(1, state="DOWNLOADING"),
            make_job(2, state="PENDING"),
            make_job(3, state="PAUSED"),
            stalled_torrent(4),
        ]
    )
    assert [section["group"]["code"] for section in sections] == [
        # Display order, not insertion order: what needs the operator first.
        "attention",
        "active",
        "waiting",
        "paused",
    ]
    assert [section["count"] for section in sections] == [1, 1, 1, 1]


def test_an_empty_section_is_dropped_rather_than_rendered_as_zero() -> None:
    # An operator reading「需干预 0」has to stop and confirm it really is zero.
    sections = group_jobs([make_job(1, state="DOWNLOADING")])
    assert [section["group"]["code"] for section in sections] == ["active"]


def test_a_section_count_always_equals_its_own_rows() -> None:
    # The heading's number comes from the section's list, so the two cannot
    # drift. This is the assertion that would fail if a count were passed in.
    sections = group_jobs([make_job(i, state="PENDING") for i in range(1, 6)])
    for section in sections:
        assert section["count"] == len(section["jobs"])


# ------------------------------------------------------ needs attention


def test_a_stalled_torrent_needs_attention_without_being_a_failure() -> None:
    job = stalled_torrent()
    assert job.attention_reason == "STALLED_TORRENT"
    assert job.queue_group == "attention"
    # The acceptance criterion in the plan:「停滞种子不被判失败」. It is still
    # WAITING_TORRENT, still cancellable, and the swarm may still deliver it.
    assert job.state == "WAITING_TORRENT"
    assert job.is_cancellable
    payload = job_summary(job)
    assert payload["state"]["code"] == "WAITING_TORRENT"
    assert payload["attention"]["code"] == "STALLED_TORRENT"
    assert payload["state"]["label"] != payload["state"]["code"]


def test_a_torrent_with_a_seeder_is_not_stalled() -> None:
    job = make_job(
        1,
        state="WAITING_TORRENT",
        provider=PROVIDER_EH_TORRENT,
        details={"progress": 0.42, "num_seeds": 3},
    )
    assert job.stalled_minutes is None
    assert job.attention_reason is None
    assert job.queue_group == "active"


def test_reasons_are_ordered_by_specificity_not_by_arrival() -> None:
    # 「缺少解压密码」is a more useful thing to read than「任务失败」, so the
    # specific asks come first however the jobs happen to be listed.
    summary = attention_summary(
        [
            make_job(1, state="FAILED", error_code="ARCHIVE_UNREADABLE"),
            make_job(
                2,
                state=CONVERSION_STATE_WAITING_PASSWORD,
                provider=PROVIDER_CONVERSION,
            ),
            make_job(
                3,
                state=CONVERSION_STATE_WAITING_VOLUMES,
                provider=PROVIDER_CONVERSION,
            ),
        ]
    )
    assert [entry["reason"]["code"] for entry in summary["reasons"]] == [
        "MISSING_VOLUMES",
        "MISSING_PASSWORD",
        "FAILED",
    ]
    assert summary["total"] == 3


def test_the_roll_up_total_equals_the_sum_of_its_reasons() -> None:
    summary = attention_summary([stalled_torrent(i) for i in range(1, 4)])
    assert summary["total"] == 3
    assert summary["reasons"][0]["count"] == 3
    # Job ids travel with the reason so the workbench can link into the queue.
    assert summary["reasons"][0]["job_ids"] == [1, 2, 3]


def test_a_job_needing_nothing_contributes_nothing() -> None:
    summary = attention_summary([make_job(1, state="DOWNLOADING")])
    assert summary == {"total": 0, "reasons": []}


# --------------------------------------------------- the transfer line


def test_the_transfer_line_carries_numbers_and_no_state_label() -> None:
    job = make_job(
        1,
        state="WAITING_TORRENT",
        provider=PROVIDER_EH_TORRENT,
        details={
            "progress": 0.38,
            "num_seeds": 3,
            "dlspeed": 320 * 1024,
            "eta": 240,
        },
    )
    detail = job_summary(job)["torrent"]["detail"]
    assert detail == "38% · 做种者 3 · ↓320 KiB/s · 剩 4 分"


def test_an_unknown_eta_is_omitted_rather_than_printed() -> None:
    # qBittorrent reports 8640000 for「unknown」rather than omitting the field,
    # and「剩 144000 分」is worse than saying nothing.
    job = make_job(
        1,
        state="WAITING_TORRENT",
        provider=PROVIDER_EH_TORRENT,
        details={"progress": 0.1, "num_seeds": 1, "eta": 8640000},
    )
    assert job.eta_seconds is None
    assert "剩" not in job_summary(job)["torrent"]["detail"]


def test_a_job_with_nothing_to_report_gets_no_line() -> None:
    assert job_summary(make_job(1, state="PENDING"))["torrent"]["detail"] is None


def test_a_seeding_job_says_so_beside_its_completed_state() -> None:
    job = make_job(
        1,
        state="COMPLETED",
        provider=PROVIDER_EH_TORRENT,
        details={"seeding": True, "upspeed": 8 * 1024},
    )
    payload = job_summary(job)
    # 「已完成」alone would tell the operator this job stopped using their
    # upstream, which is exactly what it has not done.
    assert payload["state"]["code"] == "COMPLETED"
    assert payload["note"]["label"] == "正在做种"
    assert payload["torrent"]["detail"] == "↑8 KiB/s"
    assert payload["actions"]["stop_seeding"] is True


def test_a_finished_job_that_is_not_seeding_has_no_note() -> None:
    job = make_job(1, state="COMPLETED", provider="TELEGRAM")
    assert job_summary(job)["note"] is None


# ------------------------------------------------------------ the snapshot


class _FakeService:
    """The two queue reads `queue_snapshot` makes, and nothing else."""

    def __init__(self, downloads, packing) -> None:
        self._downloads = downloads
        self._packing = packing

    async def list_active_jobs(self):
        return self._downloads

    async def list_active_pack_jobs(self):
        return self._packing


def snapshot_of(downloads, packing) -> dict:
    return asyncio.run(queue_snapshot(_FakeService(downloads, packing)))


def test_the_two_queues_stay_separate_but_share_the_roll_up() -> None:
    # A packaging job carries `provider='CONVERSION'` and never competes for a
    # download slot, so it belongs in its own tab -- but a packaging job stuck
    # on a password is as much a reason to show the banner as a stalled
    # download, and the operator must not have to change tab to discover it.
    snapshot = snapshot_of(
        [stalled_torrent(1)],
        [
            make_job(
                2,
                state=CONVERSION_STATE_WAITING_PASSWORD,
                provider=PROVIDER_CONVERSION,
            )
        ],
    )
    assert snapshot["counts"] == {"downloads": 1, "packing": 1}
    assert [s["group"]["code"] for s in snapshot["downloads"]] == ["attention"]
    assert [s["group"]["code"] for s in snapshot["packing"]] == ["attention"]
    assert snapshot["attention"]["total"] == 2
    assert [e["reason"]["code"] for e in snapshot["attention"]["reasons"]] == [
        "MISSING_PASSWORD",
        "STALLED_TORRENT",
    ]


@pytest.mark.parametrize(
    ("states", "live"),
    [
        # Something is advancing on its own, so the page keeps asking.
        (["DOWNLOADING"], True),
        (["PENDING"], True),
        # A stalled torrent stays live: it sits under 需干预, but only another
        # request can reveal that a peer appeared.
        (["WAITING_TORRENT"], True),
        # Nothing moves without the operator, so the poll stops entirely.
        (["PAUSED"], False),
        (["FAILED"], False),
        (["COMPLETED"], False),
        (["PAUSED", "DOWNLOADING"], True),
    ],
)
def test_live_says_whether_anything_can_change_without_the_operator(
    states: list[str], live: bool
) -> None:
    jobs = [make_job(i, state=state) for i, state in enumerate(states, start=1)]
    assert snapshot_of(jobs, [])["live"] is live


def test_an_empty_queue_is_not_live() -> None:
    snapshot = snapshot_of([], [])
    assert snapshot["live"] is False
    assert snapshot["downloads"] == []
    assert snapshot["attention"]["total"] == 0
