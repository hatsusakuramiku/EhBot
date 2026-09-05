"""The 运行日志 page and `/api/v1/logs` (R16).

What these cover that the broker tests do not: the level floor is a floor and not
an equality test, the buffer is preferred over the file but the file is still
there to fall back on, the page renders without JavaScript, and the level a
credential travelled in never reaches the response.

The stream endpoint is exercised by calling the route directly and pulling frames
off `body_iterator`. `TestClient` drains a response when its context exits, and
this stream is deliberately endless, so driving it through the client deadlocks --
the same reason `TestEventStream` in `test_api_v1.py` is written that way.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from app.api.logs import DEFAULT_VIEW_LEVEL, resolve_view_level, stream_logs
from app.config import Settings
from app.logging import log_broker
from app.main import create_app


@pytest.fixture(autouse=True)
def _empty_buffer():
    """Start every test with an empty buffer.

    The broker is process wide -- deliberately, because the root logger it is fed
    from is -- so without this a test asserting 「这个级别下什么都没有」 would be
    reading records the previous test logged. Reaching into `_records` is the
    point rather than a shortcut: there is no public way to clear the buffer,
    because the page must not offer one, and a `clear()` added for tests would be
    a method production code could call.
    """
    broker = log_broker()
    broker._records.clear()  # noqa: SLF001
    yield
    broker._records.clear()  # noqa: SLF001


def _settings(root: Path, **overrides) -> Settings:
    return Settings(
        data_path=root / "data",
        library_path=root / "library",
        work_path=root / "work",
        app_secret_key="test-secret-key-with-at-least-32-characters",
        tag_translation_enabled=False,
        archive_toolchain_auto_install=False,
        torrent_enabled=False,
        # Off in every test here: an access log line per request would fill the
        # buffer with the test client's own traffic and make 「这条在里面吗」
        # depend on how many requests the fixture happened to make.
        log_access=False,
        **overrides,
    )


def _authenticate(client: TestClient, settings: Settings) -> None:
    password = (settings.data_path / "bootstrap_admin_password").read_text(
        encoding="utf-8"
    )
    login = client.get("/login")
    client.post(
        "/login",
        data={"password": password, "csrf_token": login.context["csrf_token"]},
    )
    change = client.get("/settings/passwords")
    new = "new-password-with-12-characters"
    client.post(
        "/change-password",
        data={
            "current_password": password,
            "new_password": new,
            "confirmation": new,
            "csrf_token": change.context["csrf_token"],
        },
    )


def _log(level: int, message: str, **extra) -> None:
    logging.getLogger("app.test.logs").log(level, message, extra=extra or None)


def _write_log_file(settings: Settings, payloads: list[dict]) -> None:
    """Put lines on disk without going through the pipeline.

    Written directly because the property under test is that the reader is used
    when the buffer has nothing at this level, and a record emitted through
    `logging` would be in both places by construction.
    """
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    (settings.log_dir / "ehbot.log").write_text(
        "\n".join(json.dumps(payload) for payload in payloads) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
#  The page
# ---------------------------------------------------------------------------


class TestThePage:
    def test_an_unauthenticated_caller_is_sent_to_login(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            response = client.get("/logs", follow_redirects=False)

        assert response.status_code == 303
        assert response.headers["location"].endswith("/login")

    def test_the_page_renders_a_buffered_record(self, tmp_path: Path) -> None:
        """Server-rendered, so the page is a working viewer with scripting off."""
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            _authenticate(client, settings)
            _log(logging.WARNING, "page_render_probe")
            page = client.get("/logs")

        assert page.status_code == 200
        assert "page_render_probe" in page.text
        assert page.context["logs"]["source"] == "buffer"

    def test_the_level_selector_offers_debug_but_not_critical(
        self, tmp_path: Path
    ) -> None:
        """`CRITICAL` would be a control that can only produce an empty page.

        Nothing in this application logs at that level. `DEBUG` is offered
        because a deployment started with `LOG_LEVEL=DEBUG` has debug lines worth
        reading, and a `CRITICAL` line from a dependency still appears under
        every floor -- the floor is 「这个级别及以上」.
        """
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            _authenticate(client, settings)
            page = client.get("/logs")

        codes = [choice["code"] for choice in page.context["logs"]["levels"]]
        assert codes == ["DEBUG", "INFO", "WARNING", "ERROR"]

    def test_no_chinese_severity_name_is_written_in_the_template(
        self, tmp_path: Path
    ) -> None:
        """Labels come from `log_level_view`, like every other bit of vocabulary.

        Asserted by checking the label is present *and* that it arrived through
        the payload: a template that spelled 「警告」 itself would pass the first
        half and fail this.
        """
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            _authenticate(client, settings)
            page = client.get("/logs")

        labels = {choice["label"] for choice in page.context["logs"]["levels"]}
        assert {"调试", "信息", "警告", "错误"} == labels
        for label in labels:
            assert label in page.text

    def test_the_page_says_which_level_the_process_is_emitting(
        self, tmp_path: Path
    ) -> None:
        """A floor below the configured level cannot reveal anything.

        Without this the operator who selects 调试 on an INFO deployment sees no
        change and has no way to tell whether the filter is broken.
        """
        settings = _settings(tmp_path, log_level="WARNING")
        with TestClient(create_app(settings)) as client:
            _authenticate(client, settings)
            page = client.get("/logs")

        assert page.context["logs"]["configured_level"] == "WARNING"
        assert page.context["logs"]["configured_level_label"] == "警告"
        assert "WARNING" in page.text

    def test_the_page_offers_no_way_to_clear_or_download(self, tmp_path: Path) -> None:
        """The buffer is evidence, and the file belongs to whoever runs the host.

        A clear button would delete what the page exists to show; a download link
        would turn a viewer into a file export.
        """
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            _authenticate(client, settings)
            page = client.get("/logs")

        assert "清空" not in page.text
        assert "下载日志" not in page.text
        # Scoped to the panel: the shell's 退出登录 is a POST form on every page,
        # so asserting over the whole document would only be testing base.html.
        panel = page.text[page.text.index("data-log-panel") :]
        assert 'method="post"' not in panel.lower()


# ---------------------------------------------------------------------------
#  The level floor
# ---------------------------------------------------------------------------


class TestTheLevelFloor:
    def test_a_warning_floor_still_shows_errors(self, tmp_path: Path) -> None:
        """The whole reason the floor is not an equality test.

        A viewer whose 「警告」 hid the errors would be a filter that loses
        evidence, on the page that exists for the moment evidence matters.
        """
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            _authenticate(client, settings)
            _log(logging.INFO, "floor_info")
            _log(logging.WARNING, "floor_warning")
            _log(logging.ERROR, "floor_error")
            payload = client.get("/api/v1/logs?level=WARNING").json()

        events = [entry["event"] for entry in payload["entries"]]
        assert "floor_error" in events
        assert "floor_warning" in events
        assert "floor_info" not in events

    def test_the_default_floor_is_info(self, tmp_path: Path) -> None:
        """INFO rather than 全部: DEBUG is a firehose nobody asked for."""
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            _authenticate(client, settings)
            payload = client.get("/api/v1/logs").json()

        assert payload["level"] == "INFO"
        assert DEFAULT_VIEW_LEVEL == "INFO"

    def test_an_unknown_level_falls_back_instead_of_failing(self) -> None:
        """This arrives from a select element and from stale bookmarks.

        Answering a whole page with an error because a query parameter aged badly
        is worse than showing the default view.
        """
        assert resolve_view_level("bananas") == DEFAULT_VIEW_LEVEL
        assert resolve_view_level("") == DEFAULT_VIEW_LEVEL
        assert resolve_view_level(None) == DEFAULT_VIEW_LEVEL
        assert resolve_view_level("warning") == "WARNING"


# ---------------------------------------------------------------------------
#  Buffer and file
# ---------------------------------------------------------------------------


class TestBufferAndFile:
    def test_the_buffer_is_preferred_over_the_file(self, tmp_path: Path) -> None:
        """It needs no file, which is the case the 系统 tab cannot serve.

        `LOG_TO_FILE=false`, or a data directory that turned read-only, is
        exactly the deployment where an operator most needs to see why.
        """
        settings = _settings(tmp_path)
        _write_log_file(
            settings,
            [{"timestamp": "t", "level": "ERROR", "logger": "old", "event": "from_the_file"}],
        )
        with TestClient(create_app(settings)) as client:
            _authenticate(client, settings)
            _log(logging.ERROR, "from_the_buffer")
            payload = client.get("/api/v1/logs?level=ERROR").json()

        assert payload["source"] == "buffer"
        events = [entry["event"] for entry in payload["entries"]]
        assert "from_the_buffer" in events
        assert "from_the_file" not in events

    def test_the_file_is_read_when_the_buffer_has_nothing_at_this_level(
        self, tmp_path: Path
    ) -> None:
        """A process that just started has an empty buffer and a full file."""
        settings = _settings(tmp_path)
        _write_log_file(
            settings,
            [
                {
                    "timestamp": "2026-09-04T01:00:00+00:00",
                    "level": "ERROR",
                    "logger": "app.previous",
                    "event": "from_a_previous_process",
                    "error_code": "OLD",
                }
            ],
        )
        with TestClient(create_app(settings)) as client:
            _authenticate(client, settings)
            # Nothing is logged at ERROR in this process, so the floor leaves the
            # buffer empty and the reader is the only remaining source.
            payload = client.get("/api/v1/logs?level=ERROR").json()

        assert payload["source"] == "file"
        assert payload["entries"][0]["event"] == "from_a_previous_process"

    def test_a_missing_file_is_reported_rather_than_failing(
        self, tmp_path: Path
    ) -> None:
        """「没有日志文件」 is a configuration answer, not an empty result."""
        settings = _settings(tmp_path, log_to_file=False)
        with TestClient(create_app(settings)) as client:
            _authenticate(client, settings)
            payload = client.get("/api/v1/logs?level=ERROR").json()

        assert payload["file_present"] is False
        assert payload["file_enabled"] is False
        assert payload["entries"] == []

    def test_the_buffer_capacity_is_reported(self, tmp_path: Path) -> None:
        """So 「缓冲已满」 explains a missing line instead of looking like loss."""
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            _authenticate(client, settings)
            payload = client.get("/api/v1/logs").json()

        assert payload["buffer_capacity"] > 0
        assert payload["buffered"] >= 1


# ---------------------------------------------------------------------------
#  Redaction and parity
# ---------------------------------------------------------------------------


class TestRedactionAndParity:
    def test_a_credential_never_reaches_the_page_or_the_api(
        self, tmp_path: Path
    ) -> None:
        """Redaction happens in `JsonFormatter`, and the buffer holds its output.

        This asserts the property rather than the mechanism: a second formatting
        path added later would fail here even if it looked correct.
        """
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            _authenticate(client, settings)
            _log(logging.ERROR, "provider refused token=super-secret-value")
            page = client.get("/logs")
            payload = client.get("/api/v1/logs?level=ERROR").json()

        assert "super-secret-value" not in page.text
        assert "super-secret-value" not in json.dumps(payload, ensure_ascii=False)
        assert any("<redacted>" in entry["event"] for entry in payload["entries"])

    def test_the_page_and_the_json_endpoint_cannot_disagree(
        self, tmp_path: Path
    ) -> None:
        """One snapshot builder, the rule every other section follows."""
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            _authenticate(client, settings)
            _log(logging.WARNING, "parity_probe")
            page = client.get("/logs")
            payload = client.get("/api/v1/logs").json()

        assert set(payload).issubset(set(page.context["logs"]))
        assert page.context["logs"]["levels"] == payload["levels"]

    def test_a_context_field_reaches_the_entry(self, tmp_path: Path) -> None:
        """`candidate_id` is what makes a line link to the work it is about.

        It travels through the same `_CONTEXT_FIELDS` whitelist as everywhere
        else, which is the mechanism `error_message` was silently missing from
        for two releases.
        """
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            _authenticate(client, settings)
            _log(
                logging.ERROR,
                "job_failed_with_context",
                candidate_id=42,
                job_id=7,
                error_code="PROBE",
                error_message="上游说不行",
            )
            payload = client.get("/api/v1/logs?level=ERROR").json()

        entry = next(
            item
            for item in payload["entries"]
            if item["event"] == "job_failed_with_context"
        )
        assert entry["candidate_id"] == 42
        assert entry["job_id"] == 7
        assert entry["error_code"] == "PROBE"
        assert entry["error_message"] == "上游说不行"


# ---------------------------------------------------------------------------
#  The stream endpoint
# ---------------------------------------------------------------------------


class TestTheStreamEndpoint:
    """Driven directly, because the stream is endless and `TestClient` drains."""

    def test_the_stream_requires_authentication(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        with TestClient(create_app(settings)) as client:
            response = client.get("/api/v1/logs/stream", follow_redirects=False)

        assert response.status_code == 401

    @staticmethod
    def _request(app):
        class FakeRequest:
            def __init__(self) -> None:
                self.app = app
                self.session = {"authenticated": True, "csrf_token": "t"}

        return FakeRequest()

    def test_the_headers_defeat_proxy_buffering(self, tmp_path: Path) -> None:
        """nginx would otherwise hold frames until its buffer filled."""
        settings = _settings(tmp_path)
        app = create_app(settings)

        async def scenario():
            response = await stream_logs(self._request(app))
            await response.body_iterator.aclose()
            return response

        response = asyncio.run(scenario())

        assert response.media_type == "text/event-stream"
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["x-accel-buffering"] == "no"

    def test_a_logged_record_reaches_a_connected_stream(
        self, tmp_path: Path
    ) -> None:
        """End to end: `logging` -> handler -> broker -> SSE frame."""
        settings = _settings(tmp_path)
        app = create_app(settings)

        async def scenario() -> str:
            response = await stream_logs(self._request(app))
            frames = response.body_iterator
            # The route replays the buffer before going live, and `create_app`
            # has already logged. Reading a fixed number of frames would assert
            # against whichever replayed record happened to land there, so the
            # replay is drained up to the comment that terminates it.
            while not (await anext(frames)).startswith(": connected"):
                continue
            _log(logging.ERROR, "stream_probe")
            frame = await asyncio.wait_for(anext(frames), timeout=2.0)
            await frames.aclose()
            return frame

        frame = asyncio.run(scenario())

        assert "event: log" in frame
        assert "stream_probe" in frame
