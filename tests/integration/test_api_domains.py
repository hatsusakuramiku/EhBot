"""Integration tests for the R1 domain endpoints and their write gates."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from tests.work_commands import observed_work, observed_review, observed_review_batch

from fastapi.testclient import TestClient

from app.api.actions import MAX_BATCH
from app.api.deps import CSRF_HEADER
from app.config import Settings
from app.db.database import Database
from app.main import create_app
from tests.integration.test_api_v1 import log_in, make_settings


def seed(settings: Settings, rows: list[tuple[int, str, str]]) -> None:
    """Insert `(id, status, title)` candidates into an initialised database."""
    database = Database(settings.data_path / "ehbot.db")
    asyncio.run(database.initialize())
    with sqlite3.connect(database.path) as connection:
        for candidate_id, status, title in rows:
            connection.execute(
                "INSERT INTO candidates (id, status, filter_result, "
                "filter_reason, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                (candidate_id, status, "ACCEPT", "", "2026-01-01", "2026-01-01"),
            )
            connection.execute(
                "INSERT INTO metadata_values (candidate_id, field_name, "
                "field_value, value_source, confidence, is_manual, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (candidate_id, "Title", title, "EX", 1.0, 0, "2026-01-01"),
            )


def seed_downloaded(settings: Settings, candidate_id: int) -> int:
    """Give a candidate a completed download with an ARCHIVE artifact.

    `打包` needs something to pack, and the readiness gate reads exactly what a
    real download leaves behind: a COMPLETED `download_jobs` row with an ARCHIVE
    artifact under it.
    """
    database = Database(settings.data_path / "ehbot.db")
    with sqlite3.connect(database.path) as connection:
        cursor = connection.execute(
            "INSERT INTO download_jobs (candidate_id, idempotency_key, provider, "
            "state, details_json) VALUES (?,?,?,?,?)",
            (candidate_id, f"seed-{candidate_id}", "TELEGRAM", "COMPLETED", "{}"),
        )
        job_id = cursor.lastrowid
        connection.execute(
            "INSERT INTO artifacts (job_id, artifact_type, path, size_bytes) "
            "VALUES (?,?,?,?)",
            (job_id, "ARCHIVE", "/tmp/book.zip", 10),
        )
    return int(job_id)


def csrf_token(client: TestClient) -> str:
    """Read the token the page layer embeds, as the browser would."""
    return client.get("/").context["csrf_token"]


class TestCandidateListing:
    def test_returns_the_paging_envelope_with_counts(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            seed(settings, [(1, "PENDING_REVIEW", "Book 1")])
            log_in(client, settings)
            payload = client.get("/api/v1/candidates").json()

        assert set(payload) >= {
            "items",
            "total",
            "page",
            "page_size",
            "pages",
            "counts",
            "tab",
        }
        # Counts ride along with the list so a tab badge cannot disagree with
        # the grid beneath it.
        assert payload["counts"]["pending_review"] == 1

    def test_a_status_gets_its_label_and_tone_resolved(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            seed(settings, [(1, "PENDING_REVIEW", "Book 1")])
            log_in(client, settings)
            item = client.get("/api/v1/candidates").json()["items"][0]

        # The browser must never have to translate an enum itself.
        assert item["status"]["label"] == "\u5f85\u5ba1\u6838"
        assert item["status"]["tone"] == "waiting"

    def test_an_unknown_tab_is_refused_with_the_allowed_set(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            log_in(client, settings)
            response = client.get("/api/v1/candidates?tab=nope")

        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == "TAB_UNKNOWN"
        # Listing what is allowed makes the failure self-explanatory.
        assert "all" in error["details"]["allowed"]

    def test_an_unknown_sort_is_refused(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            log_in(client, settings)
            response = client.get("/api/v1/candidates?sort=nope")

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "SORT_UNKNOWN"

    def test_page_size_has_no_upper_limit(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            log_in(client, settings)
            payload = client.get(
                "/api/v1/candidates?page_size=99999"
            ).json()

        assert payload["page_size"] == 99999

    def test_the_needs_info_tab_selects_what_it_counts(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            seed(
                settings,
                [(1, "NEEDS_INFO", "A"), (2, "NEEDS_INFO", "B")],
            )
            log_in(client, settings)
            payload = client.get("/api/v1/candidates?tab=needs_info").json()

        assert payload["total"] == 2


class TestWorkDetail:
    def test_returns_metadata_timeline_and_jobs(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            seed(settings, [(1, "PENDING_REVIEW", "Book 1")])
            log_in(client, settings)
            payload = client.get("/api/v1/works/1").json()

        assert payload["candidate_id"] == 1
        assert set(payload) >= {"metadata", "timeline", "jobs", "messages"}
        assert payload["metadata"][0]["field_label"] == "\u6807\u9898"

    def test_a_missing_work_is_404_not_400(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            log_in(client, settings)
            response = client.get("/api/v1/works/404")

        # The request was well formed; the row is simply gone.
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "CANDIDATE_NOT_FOUND"


class TestActivity:
    def test_an_idle_queue_reports_not_live(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            log_in(client, settings)
            payload = client.get("/api/v1/queue").json()

        # False is what lets an idle tab stop polling entirely.
        assert payload["live"] is False
        assert payload["counts"] == {"downloads": 0, "packing": 0}


class TestSummary:
    def test_aggregates_without_requiring_optional_services(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            seed(settings, [(1, "PENDING_REVIEW", "Book 1")])
            log_in(client, settings)
            response = client.get("/api/v1/summary")

        # Torrent and Telegraph are off in test settings; the dashboard must
        # still render rather than 503.
        assert response.status_code == 200
        payload = response.json()
        assert payload["candidates"]["pending_review"] == 1
        assert payload["attention"]["total"] == 0


class TestWriteGates:
    def test_a_write_without_a_csrf_header_is_refused(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            seed(settings, [(1, "PENDING_REVIEW", "Book 1")])
            log_in(client, settings)
            response = client.post(
                "/api/v1/candidates/batch",
                json={"action": "reject", "candidate_ids": [1]},
            )

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "CSRF_INVALID"

    def test_an_anonymous_write_is_401_before_any_csrf_check(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            response = client.post(
                "/api/v1/candidates/batch",
                json={"action": "reject", "candidate_ids": [1]},
                follow_redirects=False,
            )

        # Order matters: an expired session must not read as a CSRF failure.
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "NOT_AUTHENTICATED"

    def test_reject_transitions_the_candidate(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            seed(settings, [(1, "PENDING_REVIEW", "Book 1")])
            log_in(client, settings)
            response = client.post(
                "/api/v1/candidates/batch",
                json={"action": "reject", "candidate_ids": [1], **observed_review_batch(client, [1])},
                headers={CSRF_HEADER: csrf_token(client)},
            )
            status = client.get("/api/v1/works/1").json()["status"]["code"]

        assert response.status_code == 200
        assert status == "REJECTED"

    def test_approving_an_unroutable_candidate_is_refused(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            seed(settings, [(1, "PENDING_REVIEW", "Book 1")])
            log_in(client, settings)
            response = client.post(
                "/api/v1/candidates/batch",
                json={"action": "approve", "candidate_ids": [1], **observed_review_batch(client, [1])},
                headers={CSRF_HEADER: csrf_token(client)},
            )
            status = client.get("/api/v1/works/1").json()["status"]["code"]

        # No attachment, no torrent, no preview: nothing to download. The batch
        # reports that per candidate rather than failing the whole selection --
        # one unroutable item must not refuse the other forty-nine -- so the
        # refusal is a `skipped` entry carrying the domain code.
        assert response.status_code == 200
        payload = response.json()
        assert payload["applied"] == []
        assert [entry["candidate_id"] for entry in payload["skipped"]] == [1]
        assert payload["skipped"][0]["code"] == "CANDIDATE_NOT_DOWNLOADABLE"
        # And the refusal must leave the candidate untouched.
        assert status == "PENDING_REVIEW"

    def test_an_oversized_batch_is_refused_rather_than_truncated(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            log_in(client, settings)
            response = client.post(
                "/api/v1/candidates/batch",
                json={
                    "action": "reject",
                    "candidate_ids": list(range(MAX_BATCH + 5)),
                },
                headers={CSRF_HEADER: csrf_token(client)},
            )

        # Silently acting on part of a selection would be worse than refusing.
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "BATCH_TOO_LARGE"

    def test_an_unknown_job_action_lists_the_allowed_ones(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            log_in(client, settings)
            response = client.post(
                "/api/v1/jobs/1/explode",
                json={},
                headers={CSRF_HEADER: csrf_token(client)},
            )

        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == "ACTION_UNKNOWN"
        assert "retry" in error["details"]["allowed"]

    def test_acting_on_a_missing_job_is_404(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            log_in(client, settings)
            response = client.post(
                "/api/v1/jobs/999/retry",
                json={"task_id": "removed-task", "expected_revision": 1, "idempotency_key": "missing-job"},
                headers={CSRF_HEADER: csrf_token(client)},
            )

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "JOB_NOT_FOUND"

    def test_batch_delete_removes_the_candidate(self, tmp_path: Path) -> None:
        """The mobile client's 彻底删除 rides the batch endpoint (M9)."""
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            seed(settings, [(1, "PENDING_REVIEW", "Book 1")])
            log_in(client, settings)
            response = client.post(
                "/api/v1/candidates/batch",
                json={"action": "delete", "candidate_ids": [1], **observed_review_batch(client, [1])},
                headers={CSRF_HEADER: csrf_token(client)},
            )
            gone = client.get("/api/v1/works/1")

        assert response.status_code == 200
        payload = response.json()
        assert [entry["candidate_id"] for entry in payload["applied"]] == [1]
        # The row is gone, not merely moved on: the detail endpoint is a 404.
        assert gone.status_code == 404

    def test_delete_reports_the_file_decision_without_deleting_any(
        self, tmp_path: Path
    ) -> None:
        """`delete` keeps files; `delete-files` is a different word."""
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            seed(settings, [(1, "PENDING_REVIEW", "Book 1")])
            log_in(client, settings)
            response = client.post(
                "/api/v1/candidates/batch",
                json={"action": "delete", "candidate_ids": [1], **observed_review_batch(client, [1])},
                headers={CSRF_HEADER: csrf_token(client)},
            )

        applied = response.json()["applied"][0]
        # The safe word reports the decision explicitly: nothing was removed.
        assert applied["deleted_files"] == []
        assert applied["failed_files"] == []

    def test_an_unknown_batch_action_lists_review_and_delete_words(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            log_in(client, settings)
            response = client.post(
                "/api/v1/candidates/batch",
                json={"action": "explode", "candidate_ids": [1]},
                headers={CSRF_HEADER: csrf_token(client)},
            )

        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == "ACTION_UNKNOWN"
        assert set(error["details"]["allowed"]) == {
            "approve",
            "reject",
            "delete",
            "delete-files",
        }

    def test_deleting_a_missing_candidate_is_skipped_not_fatal(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            log_in(client, settings)
            response = client.post(
                "/api/v1/candidates/batch",
                json={"action": "delete-files", "candidate_ids": [999], 'idempotency_key': 'previously-observed',
                      'versions': {'999': {'work_id': 'removed-work-uuid', 'expected_revision': 1}}},
                headers={CSRF_HEADER: csrf_token(client)},
            )

        # One missing row must not refuse the rest of the selection.
        assert response.status_code == 200
        payload = response.json()
        assert payload["applied"] == []
        assert [entry["candidate_id"] for entry in payload["skipped"]] == [999]


class TestMetadataPatch:
    def test_requeue_returns_a_rejected_candidate_to_review(
        self, tmp_path: Path
    ) -> None:
        """The mobile client's 重新排队 rides the work detail path (M9)."""
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            seed(settings, [(1, "REJECTED", "Book 1")])
            log_in(client, settings)
            response = client.post(
                "/api/v1/works/1/requeue",
                json={**observed_review(client), "note": "误驳回"},
                headers={CSRF_HEADER: csrf_token(client)},
            )
            status = client.get("/api/v1/works/1").json()["status"]["code"]

        assert response.status_code == 200
        assert response.json()["status"] == "PENDING_REVIEW"
        assert status == "PENDING_REVIEW"

    def test_requeue_refuses_a_status_that_is_already_in_the_queue(
        self, tmp_path: Path
    ) -> None:
        """Which statuses may be requeued is the service's decision, once."""
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            seed(settings, [(1, "PENDING_REVIEW", "Book 1")])
            log_in(client, settings)
            response = client.post(
                "/api/v1/works/1/requeue",
                json=observed_review(client),
                headers={CSRF_HEADER: csrf_token(client)},
            )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "REVIEW_INVALID_TRANSITION"

    def test_requeue_needs_the_same_write_gates_as_any_other_action(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            seed(settings, [(1, "REJECTED", "Book 1")])
            log_in(client, settings)
            response = client.post("/api/v1/works/1/requeue", json={})

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "CSRF_INVALID"

    def test_an_override_is_applied_and_echoed_back(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            seed(settings, [(1, "PENDING_REVIEW", "Book 1")])
            log_in(client, settings)
            response = client.patch(
                "/api/v1/works/1/metadata",
                json={**observed_work(client), "fields": {"Artist": "Sensei"}},
                headers={CSRF_HEADER: csrf_token(client)},
            )

        assert response.status_code == 200
        assert response.json()["metadata"]["Artist"] == "Sensei"

    def test_an_invalid_field_is_refused(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            seed(settings, [(1, "PENDING_REVIEW", "Book 1")])
            log_in(client, settings)
            response = client.patch(
                "/api/v1/works/1/metadata",
                json={"fields": {"Not-A-Field": "x"}},
                headers={CSRF_HEADER: csrf_token(client)},
            )

        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == "METADATA_FIELD_INVALID"
        assert error["details"]["unknown"] == ["Not-A-Field"]

    def test_patching_a_missing_work_is_404(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            log_in(client, settings)
            response = client.patch(
                "/api/v1/works/404/metadata",
                json={"work_id": "deleted-work", "expected_revision": 1,
                      "idempotency_key": "missing-work-edit", "fields": {"Artist": "Sensei"}},
                headers={CSRF_HEADER: csrf_token(client)},
            )

        assert response.status_code == 404

    def test_a_malformed_body_is_reported_as_such(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            seed(settings, [(1, "PENDING_REVIEW", "Book 1")])
            log_in(client, settings)
            response = client.patch(
                "/api/v1/works/1/metadata",
                content=b"not json",
                headers={
                    CSRF_HEADER: csrf_token(client),
                    "Content-Type": "application/json",
                },
            )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "BODY_INVALID"


class TestMetadataLocks:
    def test_a_lock_alone_is_a_valid_request(self, tmp_path: Path) -> None:
        """Pinning is not an edit.

        The value the operator wants held is one ExHentai already supplied, so
        requiring `fields` alongside `locks` would force the interface to
        re-send text it is not changing.
        """
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            seed(settings, [(1, "PENDING_REVIEW", "Book 1")])
            log_in(client, settings)
            response = client.patch(
                "/api/v1/works/1/metadata",
                json={**observed_work(client), "locks": {"Title": True}},
                headers={CSRF_HEADER: csrf_token(client)},
            )
            entry = next(
                item
                for item in client.get("/api/v1/works/1").json()["metadata"]
                if item["field_name"] == "Title"
            )

        assert response.status_code == 200
        payload = response.json()
        assert payload["locked"] == ["Title"]
        assert payload["unlocked"] == []
        assert payload["updated"] == []
        # The flag has to reach the read layer, or the interface cannot render
        # a pinned field as pinned.
        assert entry["is_locked"] is True

    def test_a_lock_is_released_by_the_same_endpoint(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            seed(settings, [(1, "PENDING_REVIEW", "Book 1")])
            log_in(client, settings)
            token = csrf_token(client)
            client.patch(
                "/api/v1/works/1/metadata",
                json={**observed_work(client), "locks": {"Title": True}},
                headers={CSRF_HEADER: token},
            )
            response = client.patch(
                "/api/v1/works/1/metadata",
                json={**observed_work(client), "locks": {"Title": False}},
                headers={CSRF_HEADER: token},
            )

        assert response.json()["unlocked"] == ["Title"]
        assert response.json()["locked"] == []

    def test_an_edit_and_a_lock_pin_the_value_just_written(
        self, tmp_path: Path
    ) -> None:
        """Order matters: the lock must land on the new value, not the old one."""
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            seed(settings, [(1, "PENDING_REVIEW", "Book 1")])
            log_in(client, settings)
            response = client.patch(
                "/api/v1/works/1/metadata",
                json={
                    **observed_work(client),
                    "fields": {"Artist": "Sensei"},
                    "locks": {"Artist": True},
                },
                headers={CSRF_HEADER: csrf_token(client)},
            )

        assert response.status_code == 200
        payload = response.json()
        assert payload["updated"] == ["Artist"]
        assert payload["locked"] == ["Artist"]
        assert payload["metadata"]["Artist"] == "Sensei"

    def test_an_invalid_field_in_locks_is_refused(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            seed(settings, [(1, "PENDING_REVIEW", "Book 1")])
            log_in(client, settings)
            response = client.patch(
                "/api/v1/works/1/metadata",
                json={"locks": {"Not-A-Field": True}},
                headers={CSRF_HEADER: csrf_token(client)},
            )

        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == "METADATA_FIELD_INVALID"
        assert error["details"]["unknown"] == ["Not-A-Field"]

    def test_locks_must_be_a_mapping(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            seed(settings, [(1, "PENDING_REVIEW", "Book 1")])
            log_in(client, settings)
            response = client.patch(
                "/api/v1/works/1/metadata",
                json={"locks": ["Title"]},
                headers={CSRF_HEADER: csrf_token(client)},
            )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "LOCKS_INVALID"

    def test_an_empty_body_still_asks_for_something_to_do(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            seed(settings, [(1, "PENDING_REVIEW", "Book 1")])
            log_in(client, settings)
            response = client.patch(
                "/api/v1/works/1/metadata",
                json={},
                headers={CSRF_HEADER: csrf_token(client)},
            )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "FIELDS_REQUIRED"

    def test_locking_a_field_with_no_value_is_a_404(self, tmp_path: Path) -> None:
        """`Artist` has no row in the fixture, so there is nothing to pin."""
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            seed(settings, [(1, "PENDING_REVIEW", "Book 1")])
            log_in(client, settings)
            response = client.patch(
                "/api/v1/works/1/metadata",
                json={**observed_work(client), "locks": {"Artist": True}},
                headers={CSRF_HEADER: csrf_token(client)},
            )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "METADATA_VALUE_NOT_FOUND"


class TestNeedsRevisionRemoved:
    """R84: 「要求修订」 is gone -- over JSON as well as on the page."""

    def test_the_needs_revision_endpoint_no_longer_exists(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            seed(settings, [(1, "PENDING_REVIEW", "Book 1")])
            log_in(client, settings)
            response = client.post(
                "/api/v1/works/1/needs-revision",
                json={"reason": "x"},
                headers={CSRF_HEADER: csrf_token(client)},
            )

        assert response.status_code == 404


class TestConvert:
    """R70: 打包 / 重新打包 over JSON, one endpoint for both."""

    def test_pressing_convert_queues_a_packaging_job(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            seed(settings, [(1, "APPROVED", "Book 1")])
            download_job = seed_downloaded(settings, 1)
            log_in(client, settings)
            response = client.post(
                "/api/v1/works/1/convert",
                json={**observed_review(client, 1), },
                headers={CSRF_HEADER: csrf_token(client)},
            )

        assert response.status_code == 200
        assert response.json()["job_id"] > 0
        # A *new* task, not the download it is packing.
        assert response.json()["job_id"] != download_job

    def test_convert_refuses_a_work_with_nothing_downloaded(
        self, tmp_path: Path
    ) -> None:
        """The readiness gate is the archive artifact, not the status."""
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            seed(settings, [(1, "APPROVED", "Book 1")])
            log_in(client, settings)
            response = client.post(
                "/api/v1/works/1/convert",
                json={**observed_review(client, 1), },
                headers={CSRF_HEADER: csrf_token(client)},
            )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "ARCHIVE_NOT_READY"

    def test_a_second_press_reuses_the_same_job(self, tmp_path: Path) -> None:
        """「打包」and「重新打包」are one button with two labels."""
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            seed(settings, [(1, "APPROVED", "Book 1")])
            seed_downloaded(settings, 1)
            log_in(client, settings)
            first = client.post(
                "/api/v1/works/1/convert",
                json={**observed_review(client, 1), },
                headers={CSRF_HEADER: csrf_token(client)},
            ).json()["job_id"]
            second = client.post(
                "/api/v1/works/1/convert",
                json={**observed_review(client, 1), },
                headers={CSRF_HEADER: csrf_token(client)},
            ).json()["job_id"]

        assert first == second

    def test_a_missing_work_is_a_404(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            log_in(client, settings)
            response = client.post(
                "/api/v1/works/404/convert",
                json={'work_id': 'removed-work', 'expected_revision': 1, 'idempotency_key': 'missing-work', },
                headers={CSRF_HEADER: csrf_token(client)},
            )

        assert response.status_code == 404

    def test_it_needs_the_same_write_gates_as_any_other_action(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            seed(settings, [(1, "APPROVED", "Book 1")])
            log_in(client, settings)
            response = client.post("/api/v1/works/1/convert", json={})

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "CSRF_INVALID"


    def test_convert_binds_only_the_requested_works_artifact(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        app = create_app(settings)
        with TestClient(app) as client:
            seed(settings, [(1, 'APPROVED', 'One'), (2, 'APPROVED', 'Two')])
            own_job = seed_downloaded(settings, 1)
            foreign_job = seed_downloaded(settings, 2)
            log_in(client, settings)
            headers = {CSRF_HEADER: csrf_token(client)}
            with sqlite3.connect(settings.data_path / 'ehbot.db') as c:
                own = c.execute('SELECT artifact_id FROM artifacts WHERE job_id = ?', (own_job,)).fetchone()[0]
                foreign = c.execute('SELECT artifact_id FROM artifacts WHERE job_id = ?', (foreign_job,)).fetchone()[0]
            for invalid in ('', True, 123, []):
                result = client.post('/api/v1/works/1/convert', json={**observed_review(client, 1), 'input_artifact_id': invalid}, headers=headers)
                assert result.status_code == 400
                assert result.json()['error']['code'] == 'ARTIFACT_INVALID'
            wrong = client.post('/api/v1/works/1/convert', json={**observed_review(client, 1), 'input_artifact_id': foreign}, headers=headers)
            assert wrong.status_code == 400 and wrong.json()['error']['code'] == 'ARCHIVE_NOT_READY'
            selected = client.post('/api/v1/works/1/convert', json={**observed_review(client, 1), 'input_artifact_id': own}, headers=headers)
            assert selected.status_code == 200
            with sqlite3.connect(settings.data_path / 'ehbot.db') as c:
                assert c.execute("SELECT json_extract(details_json, '$.input_artifact_id') FROM download_jobs WHERE id = ?",
                                 (selected.json()['job_id'],)).fetchone()[0] == own


class TestFetchMetadata:
    """R71: 重获元数据 over JSON, on the call the page makes."""

    def test_a_candidate_with_no_gallery_is_refused_by_the_service(
        self, tmp_path: Path
    ) -> None:
        """The refusal an operator actually hits, and the proof the route
        reached `fetch_metadata_for_candidate` rather than answering itself."""
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            seed(settings, [(1, "PENDING_REVIEW", "Book 1")])
            log_in(client, settings)
            response = client.post(
                "/api/v1/works/1/fetch-metadata",
                json=observed_work(client),
                headers={CSRF_HEADER: csrf_token(client)},
            )

        assert response.status_code == 400
        assert (
            response.json()["error"]["code"] == "CANDIDATE_HAS_NO_EX_REFERENCE"
        )

    def test_a_missing_work_is_a_404(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            log_in(client, settings)
            response = client.post(
                "/api/v1/works/404/fetch-metadata",
                json={"work_id": "deleted-work", "expected_revision": 1, "idempotency_key": "missing-work-fetch"},
                headers={CSRF_HEADER: csrf_token(client)},
            )

        assert response.status_code == 404

    def test_it_needs_the_same_write_gates_as_any_other_action(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            seed(settings, [(1, "PENDING_REVIEW", "Book 1")])
            log_in(client, settings)
            response = client.post("/api/v1/works/1/fetch-metadata", json={})

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "CSRF_INVALID"

def set_candidate_fields(settings: Settings, candidate_id: int, **fields) -> None:
    """Set columns on a seeded candidate (gallery ref, preview url, hash)."""
    database = Database(settings.data_path / "ehbot.db")
    assignments = ", ".join(f"{name} = ?" for name in fields)
    with sqlite3.connect(database.path) as connection:
        connection.execute(
            f"UPDATE candidates SET {assignments} WHERE id = ?",
            (*fields.values(), candidate_id),
        )


class TestPullSource:
    """R72: 按来源拉取 over JSON, on the calls the page's buttons make."""

    def test_each_source_queues_the_job_its_own_route_would(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            seed(settings, [(1, "APPROVED", "Book 1"), (2, "APPROVED", "Book 2"), (3, "APPROVED", "Book 3")])
            for candidate_id in (1, 2, 3):
                set_candidate_fields(settings, candidate_id, ex_gid=12345 + candidate_id)
            log_in(client, settings)
            jobs = {
                provider: client.post(
                    f"/api/v1/works/{candidate_id}/sources/{provider}",
                    json={**observed_review(client, candidate_id), },
                    headers={CSRF_HEADER: csrf_token(client)},
                )
                for candidate_id, provider in enumerate(("EXHENTAI", "TELEGRAPH", "EH_TORRENT"), 1)
            }

        for provider, response in jobs.items():
            assert response.status_code == 200, provider
            assert response.json()["provider"] == provider
            assert response.json()["job_id"] > 0
        # One job per provider: the idempotency key names the source, so asking
        # for a different one is not a no-op.
        assert len({r.json()["job_id"] for r in jobs.values()}) == 3

    def test_the_provider_is_case_insensitive(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            seed(settings, [(1, "APPROVED", "Book 1")])
            log_in(client, settings)
            response = client.post(
                "/api/v1/works/1/sources/exhentai",
                json={**observed_review(client, 1), },
                headers={CSRF_HEADER: csrf_token(client)},
            )

        assert response.status_code == 200
        assert response.json()["provider"] == "EXHENTAI"

    def test_a_bot_pull_without_an_archive_attachment_is_refused(
        self, tmp_path: Path
    ) -> None:
        """The page's sentence, from the page's guard."""
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            seed(settings, [(1, "APPROVED", "Book 1")])
            log_in(client, settings)
            responses = [
                client.post(
                    f"/api/v1/works/1/sources/{provider}",
                    json={**observed_review(client, 1), },
                    headers={CSRF_HEADER: csrf_token(client)},
                )
                for provider in ("TELEGRAM", "TELEGRAM_USER")
            ]

        for response in responses:
            assert response.status_code == 400
            assert response.json()["error"]["code"] == "ARCHIVE_ATTACHMENT_MISSING"

    def test_an_unknown_source_lists_the_allowed_ones(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            seed(settings, [(1, "APPROVED", "Book 1")])
            log_in(client, settings)
            response = client.post(
                "/api/v1/works/1/sources/CONVERSION",
                json={**observed_review(client, 1), },
                headers={CSRF_HEADER: csrf_token(client)},
            )

        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == "PROVIDER_UNKNOWN"
        # `CONVERSION` is not a source an operator picks, exactly as the detail
        # page never offers it.
        assert error["details"]["allowed"] == [
            "EH_TORRENT",
            "EXHENTAI",
            "TELEGRAM",
            "TELEGRAM_USER",
            "TELEGRAPH",
        ]

    def test_a_missing_work_is_a_404(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            log_in(client, settings)
            response = client.post(
                "/api/v1/works/404/sources/EXHENTAI",
                json={'work_id': 'removed-work', 'expected_revision': 1, 'idempotency_key': 'missing-work', },
                headers={CSRF_HEADER: csrf_token(client)},
            )

        assert response.status_code == 404

    def test_it_needs_the_same_write_gates_as_any_other_action(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            seed(settings, [(1, "APPROVED", "Book 1")])
            log_in(client, settings)
            response = client.post("/api/v1/works/1/sources/EXHENTAI", json={})

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "CSRF_INVALID"
