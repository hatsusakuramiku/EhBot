"""Unit tests for the JSON API contracts, status registry and event bus."""

from __future__ import annotations

import asyncio
import json

import pytest

from app.api.contracts import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    ApiError,
    Page,
    PageParams,
)
from app.api.events import (
    EVENT_DOWNLOAD,
    Event,
    EventBus,
)
from app.api.status import (
    CANDIDATE_STATUS,
    DOWNLOAD_STATUS,
    TONE_DANGER,
    TONE_NEUTRAL,
    TONE_SUCCESS,
    connection_view,
    is_live,
    provider_label,
    status_label,
    status_tone,
    status_view,
)


class TestApiError:
    def test_payload_carries_code_message_and_details(self) -> None:
        error = ApiError(
            "CANDIDATE_NOT_FOUND",
            "候选不存在",
            status_code=404,
            details={"candidate_id": 7},
        )

        assert error.status_code == 404
        assert error.to_payload() == {
            "error": {
                "code": "CANDIDATE_NOT_FOUND",
                "message": "候选不存在",
                "details": {"candidate_id": 7},
            }
        }

    def test_details_default_to_an_empty_dict(self) -> None:
        # The interface always reads `details`, so it must never be null.
        assert ApiError("X", "y").to_payload()["error"]["details"] == {}


class TestPageParams:
    def test_defaults_when_query_string_is_absent(self) -> None:
        params = PageParams.clamp(None, None)

        assert params.page == 1
        assert params.page_size == DEFAULT_PAGE_SIZE
        assert params.offset == 0

    @pytest.mark.parametrize("page", [0, -1, -999])
    def test_non_positive_page_falls_back_to_the_first(self, page: int) -> None:
        # A negative offset would make SQLite raise rather than return rows.
        assert PageParams.clamp(page, 10).page == 1
        assert PageParams.clamp(page, 10).offset == 0

    def test_page_size_is_capped_rather_than_rejected(self) -> None:
        params = PageParams.clamp(1, 100_000)

        assert params.page_size == MAX_PAGE_SIZE

    @pytest.mark.parametrize("size", [0, -5])
    def test_non_positive_page_size_uses_the_default(self, size: int) -> None:
        assert PageParams.clamp(1, size).page_size == DEFAULT_PAGE_SIZE

    def test_offset_follows_the_requested_window(self) -> None:
        params = PageParams.clamp(4, 25)

        assert params.offset == 75
        assert params.limit == 25


class TestPage:
    def test_pages_is_derived_from_total_and_size(self) -> None:
        page = Page.of([1, 2, 3], total=101, params=PageParams.clamp(1, 25))

        payload = page.to_payload()
        assert payload["total"] == 101
        assert payload["page_size"] == 25
        # 101 items at 25 per page is 5 pages, the last one partial.
        assert payload["pages"] == 5

    def test_empty_result_reports_zero_pages(self) -> None:
        page = Page.of([], total=0, params=PageParams.clamp(1, 50))

        assert page.to_payload()["pages"] == 0
        assert page.to_payload()["items"] == []


class TestStatusRegistry:
    def test_known_download_state_resolves_to_chinese(self) -> None:
        assert status_label("WAITING_TORRENT") == "等待做种"
        assert status_label("CONVERSION_WAITING_PASSWORD") == "待补密码"

    def test_unknown_code_is_shown_verbatim_not_raised(self) -> None:
        # A newly added backend state must never blank out a page.
        view = status_view("SOME_FUTURE_STATE")

        assert view.label == "SOME_FUTURE_STATE"
        assert view.tone == TONE_NEUTRAL

    def test_empty_code_renders_a_dash(self) -> None:
        assert status_label(None) == "—"
        assert status_label("") == "—"

    def test_tones_separate_success_from_failure(self) -> None:
        assert status_tone("COMPLETED") == TONE_SUCCESS
        assert status_tone("FAILED") == TONE_DANGER

    def test_live_states_are_the_ones_that_advance_alone(self) -> None:
        # These drive whether the interface keeps polling.
        assert is_live("DOWNLOADING") is True
        assert is_live("WAITING_TORRENT") is True
        assert is_live("CONVERSION_RUNNING") is True
        assert is_live("COMPLETED") is False
        assert is_live("PAUSED") is False

    def test_candidate_failed_wins_over_download_failed(self) -> None:
        # Both registries define FAILED; the candidate meaning is the one an
        # operator sees most, so it must be the resolved one.
        assert status_view("FAILED").label == CANDIDATE_STATUS["FAILED"].label

    def test_provider_labels_replace_raw_enums(self) -> None:
        assert provider_label("EH_TORRENT") == "EH 种子"
        assert provider_label("TELEGRAPH") == "预览页图源"
        assert provider_label(None) == "—"

    def test_connection_view_defaults_to_unconfigured(self) -> None:
        assert connection_view(None).label == "尚未配置"
        assert connection_view("connected").label == "已连接"

    def test_every_registry_entry_uses_a_defined_tone(self) -> None:
        allowed = {
            "neutral",
            "active",
            "waiting",
            "success",
            "danger",
            "muted",
        }
        for registry in (CANDIDATE_STATUS, DOWNLOAD_STATUS):
            for code, view in registry.items():
                assert view.tone in allowed, code
                # A stale copy/paste would otherwise show the wrong state name.
                assert view.code == code


class TestEvent:
    def test_encode_produces_a_complete_sse_frame(self) -> None:
        frame = Event(
            name=EVENT_DOWNLOAD, data={"job_id": 3}, sequence=7
        ).encode()

        assert frame.startswith("event: download\n")
        assert "id: 7\n" in frame
        # A frame is only complete when terminated by a blank line.
        assert frame.endswith("\n\n")

        data_line = next(
            line for line in frame.splitlines() if line.startswith("data: ")
        )
        assert json.loads(data_line[len("data: "):]) == {
            "job_id": 3,
            "sequence": 7,
        }

    def test_chinese_payloads_are_not_escaped(self) -> None:
        frame = Event(
            name=EVENT_DOWNLOAD, data={"reason": "等待做种"}, sequence=1
        ).encode()

        assert "等待做种" in frame


class TestEventBus:
    def test_publish_without_subscribers_is_a_noop(self) -> None:
        bus = EventBus()

        # This is the common case with no browser open, and the reason a
        # worker can publish unconditionally.
        assert bus.publish(EVENT_DOWNLOAD, job_id=1) is None
        assert bus.subscriber_count == 0

    def test_unknown_event_name_fails_loudly(self) -> None:
        bus = EventBus()

        with pytest.raises(ValueError, match="unknown event name"):
            bus.publish("not-an-event", job_id=1)

    def test_subscriber_receives_published_events(self) -> None:
        async def scenario() -> list[str]:
            bus = EventBus()
            stream = bus.stream()
            preamble = [await anext(stream), await anext(stream)]
            assert preamble[0].startswith("retry:")

            bus.publish(EVENT_DOWNLOAD, job_id=42, state="COMPLETED")
            frame = await anext(stream)
            await stream.aclose()
            return [frame]

        frames = asyncio.run(scenario())
        assert "event: download" in frames[0]
        assert '"job_id":42' in frames[0]

    def test_disconnect_removes_the_subscriber(self) -> None:
        async def scenario() -> int:
            bus = EventBus()
            stream = bus.stream()
            await anext(stream)
            assert bus.subscriber_count == 1
            # Closing the generator must run the cleanup in `finally`;
            # otherwise the set grows for the life of the process.
            await stream.aclose()
            return bus.subscriber_count

        assert asyncio.run(scenario()) == 0

    def test_slow_subscriber_drops_oldest_instead_of_blocking(self) -> None:
        async def scenario() -> tuple[int, str]:
            bus = EventBus(queue_maxsize=2)
            stream = bus.stream()
            await anext(stream)
            await anext(stream)

            # Publish more than the queue holds without ever reading. A worker
            # must not be stalled by a browser that stopped consuming.
            for index in range(5):
                bus.publish(EVENT_DOWNLOAD, job_id=index)

            frame = await anext(stream)
            await stream.aclose()
            return bus.dropped_count, frame

        dropped, frame = asyncio.run(scenario())
        assert dropped == 3
        # The oldest were evicted, so the reader sees a recent event.
        assert '"job_id":3' in frame

    def test_sequence_numbers_increase_monotonically(self) -> None:
        async def scenario() -> list[int]:
            bus = EventBus()
            stream = bus.stream()
            await anext(stream)
            await anext(stream)

            sequences = []
            for _ in range(3):
                event = bus.publish(EVENT_DOWNLOAD, job_id=1)
                assert event is not None
                sequences.append(event.sequence)
            await stream.aclose()
            return sequences

        assert asyncio.run(scenario()) == [1, 2, 3]