"""Integration tests for the R1 domain endpoints and their write gates."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

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

    def test_page_size_is_clamped_not_rejected(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            log_in(client, settings)
            payload = client.get(
                "/api/v1/candidates?page_size=99999"
            ).json()

        # Refusing would only push the caller into tighter paging loops.
        assert payload["page_size"] == 200

    def test_the_needs_info_tab_covers_both_revision_states(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            seed(
                settings,
                [(1, "NEEDS_INFO", "A"), (2, "NEEDS_REVISION", "B")],
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
                json={"action": "reject", "candidate_ids": [1]},
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
                json={"action": "approve", "candidate_ids": [1]},
                headers={CSRF_HEADER: csrf_token(client)},
            )
            status = client.get("/api/v1/works/1").json()["status"]["code"]

        # No attachment, no torrent, no preview: nothing to download.
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "CANDIDATE_NOT_DOWNLOADABLE"
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
                json={},
                headers={CSRF_HEADER: csrf_token(client)},
            )

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "JOB_NOT_FOUND"


class TestMetadataPatch:
    def test_an_override_is_applied_and_echoed_back(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            seed(settings, [(1, "PENDING_REVIEW", "Book 1")])
            log_in(client, settings)
            response = client.patch(
                "/api/v1/works/1/metadata",
                json={"fields": {"Artist": "Sensei"}},
                headers={CSRF_HEADER: csrf_token(client)},
            )

        assert response.status_code == 200
        assert response.json()["metadata"]["Artist"] == "Sensei"

    def test_an_unknown_field_is_refused(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            seed(settings, [(1, "PENDING_REVIEW", "Book 1")])
            log_in(client, settings)
            response = client.patch(
                "/api/v1/works/1/metadata",
                json={"fields": {"NotAField": "x"}},
                headers={CSRF_HEADER: csrf_token(client)},
            )

        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == "METADATA_FIELD_INVALID"
        assert error["details"]["unknown"] == ["NotAField"]

    def test_patching_a_missing_work_is_404(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            log_in(client, settings)
            response = client.patch(
                "/api/v1/works/404/metadata",
                json={"fields": {"Artist": "Sensei"}},
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
                json={"locks": {"Title": True}},
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
                json={"locks": {"Title": True}},
                headers={CSRF_HEADER: token},
            )
            response = client.patch(
                "/api/v1/works/1/metadata",
                json={"locks": {"Title": False}},
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

    def test_an_unknown_field_in_locks_is_refused(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        settings.data_path.mkdir(parents=True, exist_ok=True)
        app = create_app(settings)
        with TestClient(app) as client:
            seed(settings, [(1, "PENDING_REVIEW", "Book 1")])
            log_in(client, settings)
            response = client.patch(
                "/api/v1/works/1/metadata",
                json={"locks": {"NotAField": True}},
                headers={CSRF_HEADER: csrf_token(client)},
            )

        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == "METADATA_FIELD_INVALID"
        assert error["details"]["unknown"] == ["NotAField"]

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
                json={"locks": {"Artist": True}},
                headers={CSRF_HEADER: csrf_token(client)},
            )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "METADATA_VALUE_NOT_FOUND"
