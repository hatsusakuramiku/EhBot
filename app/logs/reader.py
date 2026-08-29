"""Read the tail of the application's own log file.

Why a reader at all: this is a single-operator service with a web interface, and
until now the only way to see why a pack failed was to reach the host and run
`docker compose logs`. The dashboard shows startup errors and nothing else.

Three constraints shape the whole module:

**It takes no path.** The file is derived from `Settings.log_dir`, the same value
the handler writes to. A caller-supplied path would turn a page behind a session
into a file-disclosure primitive for the container's whole filesystem, which is
the same reasoning that keeps a URL parameter off the thumbnail proxy.

**It is bounded.** Only the last `_READ_BYTES` are read, and only `limit` lines
are returned. A log file is allowed to reach ten megabytes, and rendering that
into a page would be a denial of service an operator inflicts on themselves.

**It never raises for an unreadable line.** Records are JSON because this
application wrote them, but the file may also hold a line from a crash before
the formatter was installed, or a half-written tail. Such a line is surfaced as
a `LOG_OTHER` entry carrying the raw text rather than being dropped, because a
viewer that silently hides what it cannot parse is worse than useless during an
incident.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

#: How much of the file's tail to read. Comfortably more than `MAX_LIMIT` lines
#: of JSON, so the newest lines are always present, while bounding the read on a
#: file that rotation allows to be large.
_READ_BYTES = 1 * 1024 * 1024

#: Hard ceiling on returned lines, independent of what the caller asks for. The
#: query string is operator input and a hand-typed `?limit=100000` must not be
#: able to render a page that never finishes.
MAX_LIMIT = 500

DEFAULT_LIMIT = 100


@dataclass(frozen=True, slots=True)
class LogEntry:
    """One rendered log line.

    `raw` is kept for a line that did not parse as the expected JSON; the page
    shows it verbatim so nothing in the file is invisible.
    """

    level: str
    timestamp: str
    logger: str
    event: str
    request_id: str | None = None
    job_id: int | None = None
    candidate_id: int | None = None
    error_code: str | None = None
    exception: str | None = None
    raw: str | None = None


def _coerce_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _parse_line(line: str) -> LogEntry:
    try:
        payload = json.loads(line)
    except ValueError:
        return LogEntry(
            level="LOG_OTHER", timestamp="", logger="", event="", raw=line
        )
    if not isinstance(payload, dict):
        return LogEntry(
            level="LOG_OTHER", timestamp="", logger="", event="", raw=line
        )
    return LogEntry(
        level=str(payload.get("level") or ""),
        timestamp=str(payload.get("timestamp") or ""),
        logger=str(payload.get("logger") or ""),
        event=str(payload.get("event") or ""),
        request_id=(
            str(payload["request_id"])
            if payload.get("request_id") is not None
            else None
        ),
        job_id=_coerce_int(payload.get("job_id")),
        candidate_id=_coerce_int(payload.get("candidate_id")),
        error_code=(
            str(payload["error_code"])
            if payload.get("error_code") is not None
            else None
        ),
        exception=(
            str(payload["exception"])
            if payload.get("exception") is not None
            else None
        ),
    )


def clamp_limit(raw: str | int | None) -> int:
    """Resolve a requested line count into the allowed range."""
    if raw is None or raw == "":
        return DEFAULT_LIMIT
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    if parsed < 1:
        return DEFAULT_LIMIT
    return min(parsed, MAX_LIMIT)


def read_log_tail(
    log_dir: Path,
    *,
    limit: int = DEFAULT_LIMIT,
    level: str | None = None,
) -> tuple[list[LogEntry], bool]:
    """Return the newest entries first, plus whether the file exists.

    The boolean is not redundant with an empty list: 「no log file」 means file
    logging is off or has not written yet, while 「no matching lines」 means the
    filter excluded everything. The page says something different for each,
    because the first is a configuration answer and the second is not.
    """
    log_path = log_dir / "ehbot.log"
    try:
        size = log_path.stat().st_size
        with log_path.open("rb") as handle:
            if size > _READ_BYTES:
                handle.seek(size - _READ_BYTES)
                # The seek probably landed mid-line; that partial line is not a
                # record and would render as unparsable noise.
                handle.readline()
            blob = handle.read()
    except OSError:
        return [], False

    wanted = (level or "").strip().upper() or None
    entries: list[LogEntry] = []
    # Reversed so the read stops as soon as `limit` matches are found: with a
    # level filter on a large file, the alternative parses every line to discard
    # almost all of them.
    for line in reversed(blob.decode("utf-8", errors="replace").splitlines()):
        text = line.strip()
        if not text:
            continue
        entry = _parse_line(text)
        if wanted is not None and entry.level.upper() != wanted:
            continue
        entries.append(entry)
        if len(entries) >= limit:
            break
    return entries, True