"""The in-memory log buffer and its live stream (R16).

The bugs these guard against are the ones the first version of this module
actually had. The worst by a distance: `publish` ran on whatever thread logged,
and `asyncio.Queue.put_nowait` from another thread appends without waking the
coroutine waiting in `get()`. Every frame therefore sat in the queue until the
next keepalive timeout expired -- a 「实时」 view that was up to fifteen seconds
late, and only ever arrived by accident. Almost every record in this application
is logged from a worker thread, because every database read runs under
`asyncio.to_thread`, so that path is the normal one and not an edge case.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading

from app.logs.broker import (
    EVENT_LOG,
    BufferHandler,
    LogBroker,
    parse_buffered_line,
)


def _line(event: str, level: str = "INFO") -> str:
    return json.dumps({"level": level, "event": event, "logger": "app.test"})


# ---------------------------------------------------------------------------
#  Buffer
# ---------------------------------------------------------------------------


def test_the_buffer_returns_newest_first() -> None:
    """The order the page renders, and the order `read_log_tail` returns.

    A viewer whose two sources disagreed about direction would be unreadable.
    """
    broker = LogBroker()
    for name in ("first", "second", "third"):
        broker.publish(_line(name))

    assert [
        json.loads(record.line)["event"] for record in broker.snapshot()
    ] == ["third", "second", "first"]


def test_the_buffer_evicts_the_oldest_at_capacity() -> None:
    """Bounded memory is the point: a log storm must cost a fixed amount."""
    broker = LogBroker(buffer_size=3)
    for index in range(5):
        broker.publish(_line(str(index)))

    events = [json.loads(record.line)["event"] for record in broker.snapshot()]
    assert events == ["4", "3", "2"]
    assert broker.capacity == 3


def test_sequence_numbers_increase_and_survive_eviction() -> None:
    """The id is the page's de-duplication key across a reconnect.

    Counting positions in the deque instead would restart at 1 after an
    eviction, and the browser would discard a new record as one it had seen.
    """
    broker = LogBroker(buffer_size=2)
    sequences = [broker.publish(_line(str(index))).sequence for index in range(4)]

    assert sequences == [1, 2, 3, 4]
    assert [record.sequence for record in broker.snapshot()] == [4, 3]


def test_a_snapshot_limit_takes_the_newest() -> None:
    broker = LogBroker()
    for index in range(10):
        broker.publish(_line(str(index)))

    assert [
        json.loads(record.line)["event"]
        for record in broker.snapshot(limit=2)
    ] == ["9", "8"]


# ---------------------------------------------------------------------------
#  Stream
# ---------------------------------------------------------------------------


def test_a_subscriber_receives_a_record_published_on_the_loop() -> None:
    async def scenario() -> str:
        broker = LogBroker()
        stream = broker.stream()
        preamble = await anext(stream)
        assert preamble.startswith("retry:")
        await anext(stream)  # ": connected"

        broker.publish(_line("live"))
        frame = await anext(stream)
        await stream.aclose()
        return frame

    frame = asyncio.run(scenario())
    assert f"event: {EVENT_LOG}" in frame
    assert '"event": "live"' in frame


def test_a_record_published_from_another_thread_arrives_immediately() -> None:
    """The defect this module exists to avoid.

    `asyncio.Queue` is not thread safe: a bare `put_nowait` from a worker thread
    appends without waking the coroutine in `get()`, so the frame waits for the
    keepalive timeout -- fifteen seconds, on a page whose entire purpose is 「实
    时」. The timeout here is far below that interval, so a regression fails
    rather than passing slowly.
    """

    async def scenario() -> str:
        broker = LogBroker()
        stream = broker.stream()
        await anext(stream)
        await anext(stream)

        done = threading.Event()

        def worker() -> None:
            broker.publish(_line("from-a-thread"))
            done.set()

        threading.Thread(target=worker).start()
        frame = await asyncio.wait_for(anext(stream), timeout=2.0)
        done.wait(timeout=2.0)
        await stream.aclose()
        return frame

    assert '"event": "from-a-thread"' in asyncio.run(scenario())


def test_the_stream_replays_the_buffer_before_going_live() -> None:
    """A browser that subscribes must not stare at a blank page.

    Replayed oldest-first, because the page prepends: newest-first here would
    render the history upside down.
    """

    async def scenario() -> list[str]:
        broker = LogBroker()
        broker.publish(_line("older"))
        broker.publish(_line("newer"))

        stream = broker.stream(replay=5)
        await anext(stream)  # retry
        frames = [await anext(stream), await anext(stream)]
        await stream.aclose()
        return frames

    frames = asyncio.run(scenario())
    assert '"event": "older"' in frames[0]
    assert '"event": "newer"' in frames[1]


def test_a_replayed_record_is_not_delivered_twice() -> None:
    """The subscription is registered before the replay is taken.

    That ordering is what stops a record logged mid-replay from falling into the
    gap -- but it also means such a record is in both the replay and the queue,
    so the live loop has to skip what it already sent. Without the skip the page
    shows one line twice, which during a retry loop is indistinguishable from
    the bug being investigated.
    """

    async def scenario() -> list[str]:
        broker = LogBroker()
        broker.publish(_line("buffered"))

        stream = broker.stream(replay=5)
        await anext(stream)  # retry
        replayed = await anext(stream)
        await anext(stream)  # ": connected"

        broker.publish(_line("fresh"))
        live = await asyncio.wait_for(anext(stream), timeout=2.0)
        await stream.aclose()
        return [replayed, live]

    replayed, live = asyncio.run(scenario())
    assert '"event": "buffered"' in replayed
    # Not the buffered one a second time.
    assert '"event": "fresh"' in live


def test_a_slow_subscriber_drops_the_oldest_instead_of_blocking() -> None:
    """A browser that stopped reading must not stall the thread that logged.

    The oldest is evicted rather than the newest: during an incident the line an
    operator is waiting for is the one that just arrived.
    """

    async def scenario() -> tuple[int, str]:
        broker = LogBroker(queue_maxsize=2)
        stream = broker.stream()
        await anext(stream)
        await anext(stream)

        for index in range(5):
            broker.publish(_line(str(index)))

        frame = await anext(stream)
        await stream.aclose()
        return broker.dropped_count, frame

    dropped, frame = asyncio.run(scenario())
    assert dropped == 3
    assert '"event": "3"' in frame


def test_a_disconnect_removes_the_subscriber() -> None:
    """Otherwise the set grows for the life of the process."""

    async def scenario() -> int:
        broker = LogBroker()
        stream = broker.stream()
        await anext(stream)
        assert broker.subscriber_count == 1
        await stream.aclose()
        return broker.subscriber_count

    assert asyncio.run(scenario()) == 0


def test_publishing_with_nobody_listening_still_buffers() -> None:
    """Unlike the event bus, a record with no subscriber is not a no-op.

    The event bus drops an event nobody is waiting for because a client refetches
    state anyway. A log line has no authoritative source to refetch from once it
    has scrolled out of the file, so it is kept.
    """
    broker = LogBroker()
    broker.publish(_line("nobody-listening"))

    assert broker.subscriber_count == 0
    assert len(broker.snapshot()) == 1


# ---------------------------------------------------------------------------
#  Handler
# ---------------------------------------------------------------------------


def test_the_handler_buffers_the_formatted_line() -> None:
    """The buffer holds what the formatter produced, redaction included.

    Asserted through the handler rather than by calling `publish` directly,
    because the property that matters is that there is no second formatting path
    -- a credential is redacted in `JsonFormatter` and nowhere else.
    """
    from app.logging import JsonFormatter

    broker = LogBroker()
    handler = BufferHandler(broker)
    handler.setFormatter(JsonFormatter())
    handler.emit(
        logging.LogRecord(
            name="app.test",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg="failed for token=super-secret",
            args=(),
            exc_info=None,
        )
    )

    line = broker.snapshot()[0].line
    assert "super-secret" not in line
    assert json.loads(line)["level"] == "WARNING"


def test_a_handler_failure_never_reaches_the_caller() -> None:
    """A logging call is often already reporting a failure of its own.

    An exception escaping here would replace the error being logged with an
    error about logging it.
    """

    class Exploding(LogBroker):
        def publish(self, line: str):  # type: ignore[override]
            raise RuntimeError("buffer is broken")

    handler = BufferHandler(Exploding())
    handler.setFormatter(logging.Formatter("%(message)s"))
    record = logging.LogRecord(
        name="app.test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="the real problem",
        args=(),
        exc_info=None,
    )

    raise_exceptions = logging.raiseExceptions
    logging.raiseExceptions = False
    try:
        handler.emit(record)  # must not raise
    finally:
        logging.raiseExceptions = raise_exceptions


# ---------------------------------------------------------------------------
#  Parsing
# ---------------------------------------------------------------------------


def test_an_unparseable_buffered_line_becomes_a_log_other_envelope() -> None:
    """Same contract `app/logs/reader.py` gives an unreadable file line.

    One entry shape from both sources, because the page renders one template for
    both and a missing key there is a 500 during an incident.
    """
    payload = parse_buffered_line("<<< not json >>>")

    assert payload["level"] == "LOG_OTHER"
    assert payload["raw"] == "<<< not json >>>"


def test_a_non_dict_json_line_is_also_an_envelope() -> None:
    payload = parse_buffered_line("[1, 2, 3]")

    assert payload["level"] == "LOG_OTHER"
    assert payload["raw"] == "[1, 2, 3]"
