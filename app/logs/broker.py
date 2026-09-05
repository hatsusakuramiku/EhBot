"""In-process log buffer and live fan-out for the 运行日志 page.

Why this exists alongside `app/logs/reader.py`: the reader answers 「what is on
disk」 and needs the file to exist, which it does not when `LOG_TO_FILE=false`
or when the data directory is read-only -- exactly the deployments where an
operator most needs to see why. This holds the newest records in memory, so the
page has content the moment the process has logged anything, and it can push a
record to a connected browser as it happens instead of on the next reload.

The shape is the one `app/api/events.py` already established for state
transitions, and the two constraints are the same:

* **Never block the logging call.** `emit` runs inside `logging`, which runs
  inside a worker loop, so it appends to a `deque` and offers to each
  subscriber's bounded queue without ever awaiting. A browser that stopped
  reading loses the oldest records it had not fetched; nothing upstream stalls.
* **Bounded, always.** The buffer is a `deque(maxlen=...)` and each subscriber
  queue has a `maxsize`. A log storm must cost a fixed amount of memory,
  because the alternative is that a debug-level deployment with a stuck stream
  is an OOM.

Two things it deliberately does **not** do:

* **It does not format.** `LogBroker.publish` takes the already-formatted JSON
  line, which is what `JsonFormatter` produced -- so the buffer, the file and
  stdout carry byte-identical records, redaction included. A second formatting
  path is a second place for a credential to escape.
* **It does not filter by level.** The handler is attached at the root's level
  and the *page* filters, because a filter applied on the way into the buffer
  would mean switching the page to 「警告」 shows nothing until new records
  arrive. Buffer everything the pipeline emits; decide what to show on read.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
import threading
import time


#: Records kept in memory. 1000 JSON lines is roughly 300-600 KB, which is
#: affordable on the 512 MB target and comfortably more than `MAX_LIMIT` so a
#: level filter still has something to select from after the newest page.
BUFFER_SIZE = 1000

#: Per-subscriber backlog. Larger than the event bus's 64 because these are not
#: hints a client can recover by refetching -- a dropped log line is gone from
#: that browser's view -- but still bounded, because a stuck stream must not
#: grow without limit.
QUEUE_MAXSIZE = 256

#: Comment frame sent when nothing has been logged, so a proxy does not treat a
#: quiet stream as dead. Matches the event bus's cadence.
KEEPALIVE_INTERVAL_SECONDS = 15.0

#: SSE event name the page subscribes to.
EVENT_LOG = "log"


@dataclass(frozen=True, slots=True)
class BufferedRecord:
    """One formatted record, plus the sequence number it was buffered under.

    The sequence is assigned by the broker rather than read from the payload:
    two records can share a timestamp to the microsecond, and the page needs a
    stable key for 「已经显示过这条了吗」 after a reconnect.
    """

    sequence: int
    line: str

    def encode(self) -> str:
        """Render as an SSE frame carrying the formatted line as-is.

        The line is already JSON, so it is placed in `data:` untouched rather
        than re-encoded. `id:` lets the browser resume, and the blank line
        terminates the frame.
        """
        return f"event: {EVENT_LOG}\nid: {self.sequence}\ndata: {self.line}\n\n"


@dataclass(frozen=True, slots=True, eq=False)
class _Subscriber:
    """One connected stream: its queue and the loop that queue belongs to.

    The loop is carried because `publish` runs on whichever thread logged, and
    an `asyncio.Queue` may only be touched from its own loop. `eq=False` keeps
    identity hashing, so two subscribers with structurally equal fields are
    still two entries in the set.
    """

    queue: asyncio.Queue[BufferedRecord]
    loop: asyncio.AbstractEventLoop


class LogBroker:
    """Newest records in memory, fanned out to connected browsers."""

    def __init__(
        self,
        *,
        buffer_size: int = BUFFER_SIZE,
        queue_maxsize: int = QUEUE_MAXSIZE,
    ) -> None:
        self._records: deque[BufferedRecord] = deque(maxlen=buffer_size)
        self._subscribers: set[_Subscriber] = set()
        self._queue_maxsize = queue_maxsize
        self._sequence = 0
        self._dropped = 0
        # `publish` is called from whatever thread logged, including the ones
        # `asyncio.to_thread` runs every database read on, so the buffer and the
        # sequence counter are shared mutable state across threads.
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        """How many records fit before the oldest is evicted.

        Reported to the page so 「缓冲 1000/1000」 explains why a line an operator
        remembers seeing is no longer there, instead of it looking like loss.
        """
        return self._records.maxlen or 0

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    @property
    def dropped_count(self) -> int:
        """Records a subscriber never received because it fell behind."""
        return self._dropped

    def publish(self, line: str) -> BufferedRecord:
        """Buffer a formatted record and offer it to every subscriber.

        Synchronous and never raising, because the caller is a logging handler:
        an exception here would surface as a logging error on a code path that
        is often already handling a failure, and awaiting would need an event
        loop that the thread doing the logging usually does not have.
        """
        with self._lock:
            self._sequence += 1
            record = BufferedRecord(sequence=self._sequence, line=line)
            self._records.append(record)
            subscribers = tuple(self._subscribers)
        for subscriber in subscribers:
            self._deliver(subscriber, record)
        return record

    def _deliver(self, subscriber: _Subscriber, record: BufferedRecord) -> None:
        """Hand a record to one subscriber, on the loop that owns its queue.

        This is the crux of the whole module. Most log records are emitted from
        a worker thread -- every database read runs under `asyncio.to_thread`,
        and both worker loops log from there -- and `asyncio.Queue` is **not**
        thread safe. A bare `put_nowait` from another thread appends to the
        deque without ever waking the coroutine waiting in `get()`, so the frame
        sat in the queue until the next keepalive timeout happened to expire: a
        「实时」 view that was up to fifteen seconds late, and only by accident.

        `call_soon_threadsafe` is what wakes the loop. It raises once the loop
        has closed, which is a subscriber whose request ended between the
        snapshot above and this call, so that is discarded rather than treated
        as an error.
        """
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is subscriber.loop:
            self._offer(subscriber.queue, record)
            return
        try:
            subscriber.loop.call_soon_threadsafe(
                self._offer, subscriber.queue, record
            )
        except RuntimeError:
            # The loop is closed: its `stream` generator is finished and will
            # remove itself. Nothing to report -- the client is gone.
            self._subscribers.discard(subscriber)

    def _offer(
        self, queue: asyncio.Queue[BufferedRecord], record: BufferedRecord
    ) -> None:
        """Enqueue, evicting the oldest entry when the subscriber is behind.

        Dropping the oldest rather than the newest: during an incident the line
        an operator is waiting for is the one that just arrived. Always runs on
        the queue's own loop, which is what makes the eviction safe.
        """
        try:
            queue.put_nowait(record)
            return
        except asyncio.QueueFull:
            pass
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:  # pragma: no cover - drained concurrently
            pass
        self._dropped += 1
        try:
            queue.put_nowait(record)
        except asyncio.QueueFull:  # pragma: no cover - refilled concurrently
            pass

    def snapshot(self, *, limit: int | None = None) -> tuple[BufferedRecord, ...]:
        """The buffered records, newest first.

        Newest first because that is the order the page renders and the order
        `read_log_tail` already returns; a viewer whose two sources disagreed
        about direction would be unreadable.
        """
        with self._lock:
            records = tuple(reversed(self._records))
        if limit is None:
            return records
        return records[:limit]

    async def stream(
        self, *, replay: int = 0
    ) -> AsyncIterator[str]:
        """Yield SSE frames for one subscriber until the client disconnects.

        `replay` is served from the buffer before the live queue is drained, so
        a browser that connects to the stream alone -- or reconnects after a
        proxy dropped it -- is not left with a blank page until the next record
        happens to be logged. The subscription is registered *before* the replay
        is taken, so a record logged during the replay is queued rather than
        lost in the gap between the two.
        """
        queue: asyncio.Queue[BufferedRecord] = asyncio.Queue(
            maxsize=self._queue_maxsize
        )
        subscriber = _Subscriber(
            queue=queue, loop=asyncio.get_running_loop()
        )
        with self._lock:
            self._subscribers.add(subscriber)
        try:
            yield "retry: 3000\n\n"
            replayed = self.snapshot(limit=replay) if replay > 0 else ()
            # Reversed back to oldest-first: the stream appends, so a client
            # receiving newest-first would render the replay upside down.
            for record in reversed(replayed):
                yield record.encode()
            highest_replayed = replayed[0].sequence if replayed else 0
            yield ": connected\n\n"
            last_activity = time.monotonic()
            while True:
                timeout = max(
                    0.5,
                    KEEPALIVE_INTERVAL_SECONDS
                    - (time.monotonic() - last_activity),
                )
                try:
                    record = await asyncio.wait_for(queue.get(), timeout=timeout)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    last_activity = time.monotonic()
                    continue
                # A record that arrived while the replay was being written is
                # in both places; skipping it here is what keeps the page from
                # showing one line twice.
                if record.sequence <= highest_replayed:
                    continue
                yield record.encode()
                last_activity = time.monotonic()
        finally:
            # Also runs when the client disconnects, because the generator is
            # closed; without it the set grows for the life of the process.
            with self._lock:
                self._subscribers.discard(subscriber)


class BufferHandler(logging.Handler):
    """Feed the broker with the same formatted line the file handler writes.

    Attached in `configure_logging` with `JsonFormatter`, so the buffer holds
    redacted JSON identical to what reaches disk. It carries no level of its
    own: the root's level decides what exists at all, and the page filters what
    it shows.
    """

    def __init__(self, broker: LogBroker) -> None:
        super().__init__()
        self._broker = broker

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._broker.publish(self.format(record))
        except Exception:  # pragma: no cover - handler of last resort
            # `handleError` respects `logging.raiseExceptions`, so a broken
            # buffer degrades to a message on stderr in development and to
            # silence in production. It must never propagate into the caller,
            # which is usually already reporting a failure of its own.
            self.handleError(record)


def parse_buffered_line(line: str) -> dict[str, object]:
    """Decode a buffered record for a JSON caller.

    Returns the parsed object, or a `LOG_OTHER`-shaped envelope carrying the raw
    text -- the same contract `app/logs/reader.py` gives a line it could not
    parse, so the page's two sources produce one entry shape.
    """
    try:
        payload = json.loads(line)
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        return {"level": "LOG_OTHER", "timestamp": "", "logger": "", "event": "", "raw": line}
    return payload


__all__ = [
    "BUFFER_SIZE",
    "EVENT_LOG",
    "KEEPALIVE_INTERVAL_SECONDS",
    "QUEUE_MAXSIZE",
    "BufferHandler",
    "BufferedRecord",
    "LogBroker",
    "parse_buffered_line",
]
