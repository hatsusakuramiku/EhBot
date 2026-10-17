"""Adapters delegate decisions and replay to the durable review service.

Persistence, retries and concurrent receipts are covered by the work review
integration tests. These tests exercise forwarding, errors and notifications.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.actions import apply_review_batch
from app.api.contracts import ApiError
from app.review.commands import WorkCommandError


def run(orchestrator, action="approve", ids=None, **kwargs):
    return asyncio.run(apply_review_batch(
        orchestrator, action, [1, 2, 3] if ids is None else ids, "admin", **kwargs))


def outcome():
    return {
        "action": "approve", "requested": 3,
        "applied": [{"candidate_id": 1, "job_ids": [10]}, {"candidate_id": 3, "job_ids": [30]}],
        "skipped": [{"candidate_id": 2, "code": "CANDIDATE_NOT_DOWNLOADABLE", "message": "No source"}],
    }


def test_observed_versions_and_key_reach_the_service_unchanged():
    result = outcome()
    service = SimpleNamespace(review_batch=AsyncMock(return_value=result))
    versions = {str(cid): {"work_id": f"work-{cid}", "expected_revision": cid} for cid in [1, 2, 3]}
    assert run(service, versions=versions, idempotency_key="original-key") is result
    service.review_batch.assert_awaited_once_with("approve", [1, 2, 3], versions, "original-key", "admin")


@pytest.mark.parametrize("code,status", [
    ("ACTION_UNKNOWN", 400), ("PRECONDITION_REQUIRED", 428), ("REVISION_CONFLICT", 409),
])
def test_domain_refusals_keep_their_code_status_and_details(code, status):
    service = SimpleNamespace(review_batch=AsyncMock(side_effect=WorkCommandError(
        code, "Refused", status_code=status, details={"candidate_id": 1})))
    with pytest.raises(ApiError) as error:
        run(service)
    assert (error.value.code, error.value.status_code, error.value.details) == (code, status, {"candidate_id": 1})


def test_an_empty_selection_reaches_service_validation():
    service = SimpleNamespace(review_batch=AsyncMock(side_effect=WorkCommandError(
        "CANDIDATE_IDS_INVALID", "Select a work")))
    with pytest.raises(ApiError, match="Select a work"):
        run(service, ids=[])
    service.review_batch.assert_awaited_once_with("approve", [], None, None, "admin")


def test_an_unexpected_error_is_not_reported_as_a_skip():
    service = SimpleNamespace(review_batch=AsyncMock(side_effect=RuntimeError("connection lost")))
    with pytest.raises(RuntimeError, match="connection lost"):
        run(service)


def test_replaying_a_batch_keeps_the_persisted_outcome():
    result = outcome()
    service = SimpleNamespace(review_batch=AsyncMock(return_value=result))
    assert run(service, idempotency_key="same-key") == run(service, idempotency_key="same-key") == result
    assert service.review_batch.await_count == 2


def test_only_applied_candidates_and_jobs_are_announced():
    candidates, jobs = [], []
    service = SimpleNamespace(review_batch=AsyncMock(return_value=outcome()))
    run(service, announce_candidate=candidates.append, announce_job=jobs.append)
    assert candidates == [1, 3]
    assert jobs == [10, 30]
