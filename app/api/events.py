"""In-process event bus backing the SSE endpoint.

Why this exists: the download page refreshes the whole document on a timer, so
a finished job is noticed up to N seconds late and the operator loses scroll
position every tick. Publishing a state transition here lets the interface
refresh exactly the affected row the moment it happens.

Design constraints that shaped it:

* **Never block a worker.** `publish` is synchronous and drops into a bounded
  queue. A slow or dead browser must not be able to stall the download loop, so
  a full queue discards the oldest event rather than awaiting room.
* **Events are hints, not state.** A payload carries identifiers, not a
  snapshot. The browser re-reads the authoritative row from the REST endpoint,
  which means a dropped event degrades to「刷新晚一点」rather than showing
  something wrong.
* **No external broker.** Single process by project constraint, so this is a
  set of per-subscriber `asyncio.Queue` objects and nothing more.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
import json
import logging
import time
from typing import Any


#: Per-subscriber backlog. Small on purpose: the browser refetches state, so an
#: old event has no value and holding hundreds would only delay the useful one.
QUEUE_MAXSIZE = 64

#: Comment line sent when idle so proxies and load balancers do not treat a
#: quiet stream as dead.
KEEPALIVE_INTERVAL_SECONDS = 15.0

#: Event names. The interface subscribes by name, so these are part of the
#: contract with `base.html`. Every name here has a publisher; `EVENT_LIBRARY`
#: was removed with the library-shelf phase rather than left as a name a client
#: could subscribe to and wait on forever.
EVENT_CANDIDATE = "candidate"
EVENT_DOWNLOAD = "download"
EVENT_CONVERSION = "conversion"
EVENT_CONNECTION = "connection"

KNOWN_EVENTS: frozenset[str] = frozenset(
    {
        EVENT_CANDIDATE,
        EVENT_DOWNLOAD,
        EVENT_CONVERSION,
        EVENT_CONNECTION,
    }
)


@dataclass(frozen=True, slots=True)
class Event:
    """One state transition worth telling the interface about."""

    name: str
    data: dict[str, Any] = field(default_factory=dict)
    #: Server-side sequence number. Lets a client tell「没有新事件」from
    #: 「错过了事件」after a reconnect.
    sequence: int = 0

    def encode(self) -> str:
        """Render as an SSE frame.

        `ensure_ascii=False` keeps Chinese readable in a network trace, and the
        blank terminating line is what makes the frame complete.
        """
        payload = json.dumps(
            {**self.data, "sequence": self.sequence},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return f"event: {self.name}\nid: {self.sequence}\ndata: {payload}\n\n"


class EventBus:
    """Fan-out of state transitions to connected browsers."""

    def __init__(self, queue_maxsize: int = QUEUE_MAXSIZE) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._queue_maxsize = queue_maxsize
        self._sequence = 0
        self._dropped = 0

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def dropped_count(self) -> int:
        """Events discarded because a subscriber was not keeping up."""
        return self._dropped

    def publish(self, name: str, **data: Any) -> Event | None:
        """Queue an event for every subscriber. Safe to call from any task.

        Returns the event, or None when nobody is listening -- the common case
        with no browser open, and the reason this costs almost nothing to call
        from the worker loops.
        """
        if name not in KNOWN_EVENTS:
            # A typo would otherwise produce an event no client subscribes to,
            # which is invisible in production. Fail loudly in development.
            raise ValueError(f"unknown event name: {name!r}")
        if not self._subscribers:
            return None
        self._sequence += 1
        event = Event(name=name, data=dict(data), sequence=self._sequence)
        for queue in tuple(self._subscribers):
            self._offer(queue, event)
        return event

    def _offer(self, queue: asyncio.Queue[Event], event: Event) -> None:
        """Enqueue, evicting the oldest entry when the subscriber is behind."""
        try:
            queue.put_nowait(event)
            return
        except asyncio.QueueFull:
            pass
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:  # pragma: no cover - drained concurrently
            pass
        self._dropped += 1
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:  # pragma: no cover - refilled concurrently
            logging.getLogger(__name__).warning(
                "event_dropped", extra={"error_code": "EVENT_QUEUE_FULL"}
            )

    async def stream(self) -> AsyncIterator[str]:
        """Yield SSE frames for one subscriber until the client disconnects.

        Always emits a `retry` directive and an initial comment so the browser
        learns the reconnect delay and the connection is not held open by a
        proxy waiting for first output.
        """
        queue: asyncio.Queue[Event] = asyncio.Queue(
            maxsize=self._queue_maxsize
        )
        self._subscribers.add(queue)
        try:
            yield "retry: 3000\n\n"
            yield ": connected\n\n"
            last_activity = time.monotonic()
            while True:
                timeout = max(
                    0.5,
                    KEEPALIVE_INTERVAL_SECONDS
                    - (time.monotonic() - last_activity),
                )
                try:
                    event = await asyncio.wait_for(
                        queue.get(), timeout=timeout
                    )
                except TimeoutError:
                    # Idle: a comment frame keeps intermediaries from closing
                    # the stream, and costs one line.
                    yield ": keepalive\n\n"
                    last_activity = time.monotonic()
                    continue
                yield event.encode()
                last_activity = time.monotonic()
        finally:
            # Runs on client disconnect too, because the generator is closed;
            # without this the set would grow for the life of the process.
            self._subscribers.discard(queue)


__all__ = [
    "EVENT_CANDIDATE",
    "EVENT_CONNECTION",
    "EVENT_CONVERSION",
    "EVENT_DOWNLOAD",
    "KEEPALIVE_INTERVAL_SECONDS",
    "KNOWN_EVENTS",
    "QUEUE_MAXSIZE",
    "Event",
    "EventBus",
]