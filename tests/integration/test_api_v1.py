"""Integration tests for the `/api/v1` surface and its auth gates."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.deps import CSRF_HEADER
from app.api.v1 import api_events
from app.config import Settings
from app.main import create_app


def make_settings(root: Path) -> Settings:
    return Settings(
        data_path=root / "data",
        library_path=root / "library",
        work_path=root / "work",
        app_secret_key="test-secret-key-with-at-least-32-characters",
        tag_translation_enabled=False,
    )


def read_bootstrap_password(settings: Settings) -> str:
    return (settings.data_path / "bootstrap_admin_password").read_text(
        encoding="utf-8"
    )


def log_in(client: TestClient, settings: Settings) -> None:
    """Authenticate and clear the forced password change.

    The API refuses a session that still owes a password change, so a test that
    wants data has to complete the same first-run flow an operator does.
    """
    password = read_bootstrap_password(settings)
    login_page = client.get("/login")
    client.post(
        "/login",
        data={
            "password": password,
            "csrf_token": login_page.context["csrf_token"],
        },
        follow_redirects=False,
    )
    change_page = client.get("/change-password")
    client.post(
        "/change-password",
        data={
            "current_password": password,
            "new_password": "a-much-longer-operator-password",
            "confirmation": "a-much-longer-operator-password",
            "csrf_token": change_page.context["csrf_token"],
        },
        follow_redirects=False,
    )


class TestAuthGates:
    def test_anonymous_api_call_is_401_json_not_a_redirect(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            response = client.get("/api/v1/meta", follow_redirects=False)

        # A redirect would be silently followed by fetch() and hand the caller
        # a login page with status 200, which is unreadable as an error.
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "NOT_AUTHENTICATED"

    def test_error_envelope_shape_is_stable(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            payload = client.get("/api/v1/meta").json()

        assert set(payload["error"]) == {"code", "message", "details"}

    def test_session_owing_a_password_change_is_refused(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            password = read_bootstrap_password(settings)
            login_page = client.get("/login")
            client.post(
                "/login",
                data={
                    "password": password,
                    "csrf_token": login_page.context["csrf_token"],
                },
                follow_redirects=False,
            )

            response = client.get("/api/v1/meta")

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "PASSWORD_CHANGE_REQUIRED"


class TestMetaEndpoint:
    def test_serves_the_status_vocabulary(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            log_in(client, settings)
            payload = client.get("/api/v1/meta").json()

        statuses = payload["statuses"]
        assert set(statuses) == {
            "candidate",
            "download",
            "conversion",
            "provider",
            "connection",
        }
        # The interface renders labels from this table, so a state cannot show
        # up as Chinese in one place and a raw enum in another.
        assert statuses["download"]["WAITING_TORRENT"]["label"] == "等待做种"
        assert statuses["download"]["WAITING_TORRENT"]["live"] is True
        assert statuses["download"]["COMPLETED"]["tone"] == "success"

    def test_reports_feature_switches_and_polling_hints(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            log_in(client, settings)
            payload = client.get("/api/v1/meta").json()

        assert payload["features"]["tag_translation"] is False
        assert payload["polling"]["interval_ms"] > 0
        # Hidden tabs must poll less often, not at the same rate.
        assert (
            payload["polling"]["idle_interval_ms"]
            > payload["polling"]["interval_ms"]
        )

    def test_disabled_source_is_reported_as_unavailable(
        self, tmp_path: Path
    ) -> None:
        settings = Settings(
            data_path=tmp_path / "data",
            library_path=tmp_path / "library",
            work_path=tmp_path / "work",
            app_secret_key="test-secret-key-with-at-least-32-characters",
            tag_translation_enabled=False,
            telegraph_enabled=False,
            torrent_enabled=False,
        )
        with TestClient(create_app(settings)) as client:
            log_in(client, settings)
            features = client.get("/api/v1/meta").json()["features"]

        # The interface hides a source it cannot use instead of offering a
        # button that always fails.
        assert features["telegraph"] is False
        assert features["torrent"] is False


class TestEventStream:
    """Tests for the SSE endpoint.

    The stream is intentionally endless, and `TestClient` drains a response
    when its context exits, so driving it through the client deadlocks. These
    tests invoke the route directly and pull frames off `body_iterator`, which
    exercises the real endpoint (auth gate, headers, bus wiring) without
    depending on a client that cannot express "read two frames and leave".
    """

    def test_stream_requires_authentication(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            response = client.get("/api/v1/events", follow_redirects=False)

        assert response.status_code == 401

    @staticmethod
    def _authenticated_request(app):
        """Minimal stand-in carrying the two things the route reads."""

        class FakeRequest:
            def __init__(self) -> None:
                self.app = app
                self.session = {
                    "authenticated": True,
                    "csrf_token": "token-value",
                }

        return FakeRequest()

    def test_headers_defeat_proxy_buffering(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        app = create_app(settings)

        async def scenario():
            response = await api_events(self._authenticated_request(app))
            await response.body_iterator.aclose()
            return response

        response = asyncio.run(scenario())

        assert response.media_type == "text/event-stream"
        assert response.headers["cache-control"] == "no-cache"
        # Without this nginx holds frames until its buffer fills, which would
        # defeat the entire endpoint.
        assert response.headers["x-accel-buffering"] == "no"

    def test_published_event_reaches_a_connected_client(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        app = create_app(settings)

        async def scenario() -> str:
            response = await api_events(self._authenticated_request(app))
            frames = response.body_iterator
            # Preamble first: the retry directive and the connected comment
            # are what stop a proxy from stalling on an empty stream.
            assert "retry:" in await anext(frames)
            assert await anext(frames) == ": connected\n\n"

            app.state.event_bus.publish(
                "download", job_id=11, state="COMPLETED"
            )
            frame = await anext(frames)
            await frames.aclose()
            return frame

        frame = asyncio.run(scenario())
        assert "event: download" in frame
        assert '"job_id":11' in frame

    def test_subscriber_is_released_on_disconnect(
        self, tmp_path: Path
    ) -> None:
        settings = make_settings(tmp_path)
        app = create_app(settings)

        async def scenario() -> tuple[int, int]:
            response = await api_events(self._authenticated_request(app))
            frames = response.body_iterator
            await anext(frames)
            connected = app.state.event_bus.subscriber_count
            # Closing is what a browser navigating away does; the generator's
            # `finally` must drop the queue or the set leaks for the life of
            # the process.
            await frames.aclose()
            return connected, app.state.event_bus.subscriber_count

        assert asyncio.run(scenario()) == (1, 0)

    def test_stats_require_authentication(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            response = client.get("/api/v1/events/stats")

        assert response.status_code == 401

    def test_stats_report_counters(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            log_in(client, settings)

            assert client.get("/api/v1/events/stats").json() == {
                "subscribers": 0,
                "dropped": 0,
            }


class TestCsrfHelper:
    def test_state_changing_call_requires_the_header(
        self, tmp_path: Path
    ) -> None:
        """The gate is exercised directly: R0 ships no write endpoints yet.

        Wiring the check into a real route happens in R1, but locking the
        behaviour now means the first write endpoint cannot ship without it.
        """
        from app.api import deps
        from app.api.contracts import ApiError

        class FakeRequest:
            def __init__(self, session: dict, headers: dict) -> None:
                self.session = session
                self.headers = headers

        matching = FakeRequest(
            {"csrf_token": "token-value"}, {CSRF_HEADER: "token-value"}
        )
        deps.require_csrf(matching)  # must not raise

        for session, headers in (
            ({"csrf_token": "token-value"}, {}),
            ({"csrf_token": "token-value"}, {CSRF_HEADER: "wrong"}),
            ({}, {CSRF_HEADER: "token-value"}),
        ):
            try:
                deps.require_csrf(FakeRequest(session, headers))
            except ApiError as exc:
                assert exc.code == "CSRF_INVALID"
                assert exc.status_code == 403
            else:  # pragma: no cover - guard against a silent regression
                raise AssertionError("CSRF check did not reject")