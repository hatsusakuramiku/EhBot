"""The R73 rename endpoint, on the service the page posts to.

Seeded through `test_downloaded_web`'s `Library`, because a rename needs what a
rename needs: a published CBZ that really exists on disk. A fixture that only
wrote database rows would exercise the refusal path forever and never the move.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from tests.work_commands import observed_review

from app.api.deps import CSRF_HEADER
from app.config import Settings
from app.downloads.models import (
    CONVERSION_STATE_COMPLETED,
    CONVERSION_STATE_PENDING,
    PROVIDER_CONVERSION,
)
from app.main import create_app
from tests.integration.test_downloaded_web import (
    Library,
    authenticate,
    make_settings,
)


def csrf_token(client: TestClient) -> str:
    """Read the token the page layer embeds, as the browser would."""
    return client.get("/").context["csrf_token"]


def logged_in(settings: Settings) -> TestClient:
    client = TestClient(create_app(settings))
    client.__enter__()
    authenticate(client, settings)
    return client


def packed_library(tmp_path: Path) -> tuple[Settings, Library, int]:
    """One packed work, its CBZ on disk under `作者/已打包作品.cbz`."""
    settings = make_settings(tmp_path)
    library = Library(settings)
    candidate_id = library.add(
        title="已打包作品",
        pack_state=CONVERSION_STATE_COMPLETED,
        packaged=True,
    )
    return settings, library, candidate_id


class TestRename:
    def test_a_new_filename_moves_the_file_and_records_the_path(
        self, tmp_path: Path
    ) -> None:
        settings, library, candidate_id = packed_library(tmp_path)
        client = logged_in(settings)
        try:
            response = client.post(
                f"/api/v1/downloaded/{candidate_id}/rename",
                json={**observed_review(client, candidate_id), "filename": "新名字"},
                headers={CSRF_HEADER: csrf_token(client)},
            )
        finally:
            client.__exit__(None, None, None)

        assert response.status_code == 200
        body = response.json()
        assert body["moved"] is True
        assert body["relative_path"].endswith("新名字.cbz")
        # The point of the action: the file really moved, and the artifact now
        # points at the new location so the next pack lands there.
        assert (settings.library_path / body["relative_path"]).exists()
        assert library.cbz_path(candidate_id) == str(
            settings.library_path / body["relative_path"]
        )

    def test_the_same_name_reports_not_moved_rather_than_failing(
        self, tmp_path: Path
    ) -> None:
        settings, _library, candidate_id = packed_library(tmp_path)
        client = logged_in(settings)
        try:
            response = client.post(
                f"/api/v1/downloaded/{candidate_id}/rename",
                # Both parts, as the page's drawer sends them: a bare filename
                # means「move it to the library root」, which is a move.
                json={**observed_review(client, candidate_id), "filename": "已打包作品", "directory": "作者"},
                headers={CSRF_HEADER: csrf_token(client)},
            )
        finally:
            client.__exit__(None, None, None)

        assert response.status_code == 200
        # 「文件名未变更」is not a failure: the client says so and stops.
        assert response.json()["moved"] is False

    def test_an_unpacked_work_is_refused_by_the_service(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        library = Library(settings)
        candidate_id = library.add(title="未打包作品")
        client = logged_in(settings)
        try:
            response = client.post(
                f"/api/v1/downloaded/{candidate_id}/rename",
                json={**observed_review(client, candidate_id), "filename": "新名字"},
                headers={CSRF_HEADER: csrf_token(client)},
            )
        finally:
            client.__exit__(None, None, None)

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "WORK_NOT_PACKAGED"

    def test_a_directory_cannot_walk_out_of_the_library(
        self, tmp_path: Path
    ) -> None:
        """`..` is refused by the same validator the packer uses."""
        settings, _library, candidate_id = packed_library(tmp_path)
        client = logged_in(settings)
        try:
            response = client.post(
                f"/api/v1/downloaded/{candidate_id}/rename",
                json={**observed_review(client, candidate_id), "directory": "../outside"},
                headers={CSRF_HEADER: csrf_token(client)},
            )
        finally:
            client.__exit__(None, None, None)

        assert response.status_code == 400
        assert not (settings.library_path.parent / "outside").exists()

    def test_repack_chains_a_packaging_job(self, tmp_path: Path) -> None:
        settings, library, candidate_id = packed_library(tmp_path)
        client = logged_in(settings)
        try:
            response = client.post(
                f"/api/v1/downloaded/{candidate_id}/rename",
                json={**observed_review(client, candidate_id), "filename": "新名字", "repack": True},
                headers={CSRF_HEADER: csrf_token(client)},
            )
        finally:
            client.__exit__(None, None, None)

        assert response.status_code == 200
        assert response.json()["repacked"] is True
        # A re-pack reuses the work's one packaging row and re-queues it; that is
        # what makes 重新打包 land on the file it replaces instead of inventing
        # `book (2).cbz`.
        conversions = [
            row
            for row in library.job_states(candidate_id)
            if row[0] == PROVIDER_CONVERSION
        ]
        assert len(conversions) == 1
        assert conversions[0][1] == CONVERSION_STATE_PENDING

    def test_a_work_that_was_never_downloaded_is_refused(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        Library(settings)
        client = logged_in(settings)
        try:
            response = client.post(
                "/api/v1/downloaded/404/rename",
                json={"work_id": "removed-work", "expected_revision": 1,
                      "idempotency_key": "missing-work-rename", "filename": "新名字"},
                headers={CSRF_HEADER: csrf_token(client)},
            )
        finally:
            client.__exit__(None, None, None)

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "CANDIDATE_NOT_FOUND"

    def test_it_needs_the_same_write_gates_as_any_other_action(
        self, tmp_path: Path
    ) -> None:
        settings, _library, candidate_id = packed_library(tmp_path)
        client = logged_in(settings)
        try:
            response = client.post(
                f"/api/v1/downloaded/{candidate_id}/rename",
                json={"filename": "新名字"},
            )
        finally:
            client.__exit__(None, None, None)

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "CSRF_INVALID"
