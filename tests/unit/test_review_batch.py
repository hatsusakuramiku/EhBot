"""The shared review batch loop.

`apply_review_batch` is the one place a selection becomes a sequence of single
approvals, and both the JSON endpoint and the `/candidates` form fallback go
through it. What it has to guarantee is tested here rather than through a
client, because the interesting cases are a partial run and a replay -- states
that are awkward to reach over HTTP and easy to describe with a fake.
"""

from __future__ import annotations

import asyncio

import pytest

from app.api.actions import apply_review_batch
from app.api.contracts import ApiError
from app.review.service import ReviewError


class FakeOrchestrator:
    """Approves or rejects, refusing anything it has already acted on.

    That is the real orchestrator's rule -- `_load_reviewable` rejects a
    candidate whose status is no longer reviewable -- reduced to the part the
    batch loop depends on.
    """

    def __init__(self, *, unroutable: set[int] | None = None) -> None:
        self.unroutable = unroutable or set()
        self.done: set[int] = set()
        self.approvals: list[int] = []
        self.rejections: list[int] = []

    async def approve_and_enqueue(
        self, candidate_ids: list[int], operator: str
    ) -> tuple[int, ...]:
        (candidate_id,) = candidate_ids  # the loop must pass one at a time
        self._check(candidate_id)
        if candidate_id in self.unroutable:
            raise ReviewError(
                "CANDIDATE_NOT_DOWNLOADABLE",
                f"候选 #{candidate_id} 没有可用的下载来源",
            )
        self.done.add(candidate_id)
        self.approvals.append(candidate_id)
        return (candidate_id * 10,)

    async def reject(self, candidate_ids: list[int], operator: str) -> None:
        (candidate_id,) = candidate_ids
        self._check(candidate_id)
        self.done.add(candidate_id)
        self.rejections.append(candidate_id)

    def _check(self, candidate_id: int) -> None:
        if candidate_id in self.done:
            raise ReviewError(
                "REVIEW_INVALID_TRANSITION",
                f"候选 #{candidate_id} 当前状态不可审核",
            )


def run(orchestrator: FakeOrchestrator, action: str, ids: list[int], **kwargs) -> dict:
    return asyncio.run(
        apply_review_batch(orchestrator, action, ids, "admin", **kwargs)
    )


class TestArgumentChecking:
    def test_an_unknown_action_is_refused_before_any_candidate_is_touched(
        self,
    ) -> None:
        """The check lives in the coroutine, not in its two callers.

        The form fallback posts a raw field, so a batch reaching the
        orchestrator with whatever the browser sent is the failure this guards.
        """
        orchestrator = FakeOrchestrator()
        with pytest.raises(ApiError) as raised:
            run(orchestrator, "delete", [1, 2])
        assert raised.value.code == "ACTION_UNKNOWN"
        assert raised.value.details == {"allowed": ["approve", "reject"]}
        assert orchestrator.approvals == []
        assert orchestrator.rejections == []

    def test_an_empty_selection_is_a_no_op(self) -> None:
        result = run(FakeOrchestrator(), "approve", [])
        assert result == {
            "action": "approve",
            "requested": 0,
            "applied": [],
            "skipped": [],
        }


class TestPartialRuns:
    def test_one_refusal_does_not_refuse_the_rest(self) -> None:
        orchestrator = FakeOrchestrator(unroutable={2})
        result = run(orchestrator, "approve", [1, 2, 3])
        assert [entry["candidate_id"] for entry in result["applied"]] == [1, 3]
        assert result["applied"][0]["job_ids"] == [10]
        assert result["skipped"] == [
            {
                "candidate_id": 2,
                "code": "CANDIDATE_NOT_DOWNLOADABLE",
                "message": "候选 #2 没有可用的下载来源",
            }
        ]
        assert result["requested"] == 3

    def test_an_unexpected_error_is_not_reported_as_a_skip(self) -> None:
        """A bug must not hide behind a tidy 200 with one skipped row."""

        class Broken(FakeOrchestrator):
            async def reject(self, candidate_ids: list[int], operator: str) -> None:
                raise RuntimeError("connection lost")

        with pytest.raises(RuntimeError):
            run(Broken(), "reject", [1])


class TestIdempotence:
    def test_re_sending_a_batch_applies_nothing_twice(self) -> None:
        """The double-clicked 「批量通过并下载」.

        Every candidate of the second send is already approved, so each comes
        back as a skip with the reason -- and nothing is approved again.
        """
        orchestrator = FakeOrchestrator()
        first = run(orchestrator, "approve", [1, 2, 3])
        second = run(orchestrator, "approve", [1, 2, 3])

        assert [entry["candidate_id"] for entry in first["applied"]] == [1, 2, 3]
        assert first["skipped"] == []
        assert second["applied"] == []
        assert [entry["candidate_id"] for entry in second["skipped"]] == [1, 2, 3]
        assert {entry["code"] for entry in second["skipped"]} == {
            "REVIEW_INVALID_TRANSITION"
        }
        assert orchestrator.approvals == [1, 2, 3]

    def test_replaying_a_partial_run_finishes_it(self) -> None:
        """What an operator does after a batch reported a refusal.

        Re-sending the same selection approves only what is still pending; the
        item that could not be routed is refused again, with the same code.
        """
        orchestrator = FakeOrchestrator(unroutable={2})
        run(orchestrator, "approve", [1, 2])
        replay = run(orchestrator, "approve", [1, 2])
        assert replay["applied"] == []
        assert [entry["code"] for entry in replay["skipped"]] == [
            "REVIEW_INVALID_TRANSITION",
            "CANDIDATE_NOT_DOWNLOADABLE",
        ]
        assert orchestrator.approvals == [1]


class TestAnnouncements:
    def test_only_applied_candidates_and_their_jobs_are_announced(self) -> None:
        """A skip must leave no trace of an action that did not happen.

        The live page redraws what it is told changed; announcing a skipped
        candidate would move a row that is still where it was.
        """
        candidates: list[int] = []
        jobs: list[int] = []
        orchestrator = FakeOrchestrator(unroutable={2})
        run(
            orchestrator,
            "approve",
            [1, 2, 3],
            announce_candidate=candidates.append,
            announce_job=jobs.append,
        )
        assert candidates == [1, 3]
        assert jobs == [10, 30]
