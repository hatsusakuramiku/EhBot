"""Logging setup: one JSON line per record, on every output path.

Three properties this module is responsible for, each of which was a real
defect before:

**A traceback reaches the log.** `JsonFormatter` reads `record.exc_info` and
`record.stack_info`. It did not, and the four `.exception()` call sites in the
defensive worker loops were therefore reporting an event name and nothing else
-- a download worker that says it failed without saying where.

**Every logger goes through this formatter.** `uvicorn` and `uvicorn.access`
ship with their own handlers and `propagate = False`, so clearing the root's
handlers is not enough: the access log would keep its own plain-text format and,
because redaction lives in this formatter, would never be redacted. Both are
collected in `configure_logging`, which is a security fix and not a cosmetic
one.

**Credentials never appear.** `redact_sensitive_values` runs over the message,
the exception text and the stack, because a URL carrying a token is as likely to
surface in a traceback as in a message.

Redaction is deliberately over-eager: an entire URL query string is replaced
rather than matched key by key, since the next credential to travel in a query
is one nobody added a pattern for yet.
"""

from __future__ import annotations

import contextvars
import json
import logging
import logging.handlers
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.logs.broker import BufferHandler, LogBroker


_AUTHORIZATION = re.compile(
    r"(?i)(\bauthorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+"
)
_SENSITIVE_VALUE = re.compile(
    r"(?i)(\b(?:token|api[_-]?hash|ipb_member_id|ipb_pass_hash|igneous)"
    r"\s*[:=]\s*)[^\s,;&]+"
)
#: No `\b` before the alternation. A word boundary cannot match between a
#: space and a slash, so the anchored form missed exactly the shape an access
#: log line has -- `GET /candidates?search=... HTTP/1.1` went through
#: unredacted, while `/library/a/b.cbz?x=1` was caught only because the slash
#: before `b.cbz` follows a letter. Collecting uvicorn's access logger made
#: that gap reachable, so the anchor comes off.
_URL_QUERY = re.compile(r"(?i)((?:https?://|/)[^\s?]*)\?[^\s]+")
_COOKIE_HEADER = re.compile(r"(?i)(\bcookie\s*[:=]\s*)[^\r\n]+")
_TELEGRAM_BOT_PATH = re.compile(r"(?i)(/bot)[^/\s]+(/(?:getMe|getUpdates)\b)")

#: The current request's correlation id. A `contextvars.ContextVar` rather than
#: a parameter because the point is to reach log calls that never took one --
#: every existing `logging.getLogger(__name__)` site inside a request gains the
#: id without being edited. Tasks started with `asyncio.create_task` inherit a
#: copy of the context, so a job enqueued by a request keeps its id, while the
#: two long-lived worker loops start from the default and report `None`.
request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ehbot_request_id", default=None
)

#: Fields a call site may attach through `extra=`. Listed rather than accepting
#: everything on the record, because `LogRecord` carries two dozen attributes of
#: its own and a payload that grew them all would be unreadable. Adding a field
#: here is the deliberate act of admitting it to the log contract.
#:
#: `error_message` is on the list because R13 made every failing worker attach
#: it, and a whitelist that omits it drops exactly the half of a failure an
#: operator reads: `error_code` says which branch failed, the message says what
#: the archive or the provider actually reported. It was silently discarded
#: until now, so the in-app tail showed a bare code for every failed pack.
_CONTEXT_FIELDS: tuple[str, ...] = (
    "request_id",
    "candidate_id",
    "work_id",
    "job_id",
    "source_type",
    "provider",
    "status",
    "attempt",
    "duration_ms",
    "error_code",
    "error_message",
)

_UVICORN_LOGGERS: tuple[str, ...] = ("uvicorn", "uvicorn.error", "uvicorn.access")

#: Set once `configure_logging` has run. Guards against the reconfiguration that
#: used to happen on every `create_app()`: a test session building several
#: applications would clear the root handlers repeatedly, so whether a record
#: was captured depended on construction order.
_CONFIGURED = False

#: The in-memory tail every application in this process shares.
#:
#: Module level, not per application, because the thing it buffers is process
#: wide: the root logger is a singleton, so a second broker would either receive
#: nothing or double every record. `create_app` publishes this object on
#: `app.state.log_broker`, which is how a route reaches it without importing
#: from here.
#:
#: It is created eagerly and outlives `configure_logging`, so the records logged
#: between interpreter start and the first configure call are not lost -- and so
#: a test that reconfigures logging does not invalidate a reference a running
#: application already holds.
_BROKER = LogBroker()


def log_broker() -> LogBroker:
    """The process's log buffer."""
    return _BROKER


def new_request_id() -> str:
    """A short correlation id.

    Twelve hex characters: enough to disambiguate the requests alive at one
    moment on a single-operator deployment, short enough to read off a log line
    and grep for. It is not a security token and never authorises anything.
    """
    return uuid.uuid4().hex[:12]


def redact_sensitive_values(message: str) -> str:
    message = _TELEGRAM_BOT_PATH.sub(r"\1<redacted>\2", message)
    message = _URL_QUERY.sub(r"\1?<redacted>", message)
    message = _AUTHORIZATION.sub(r"\1<redacted>", message)
    message = _SENSITIVE_VALUE.sub(r"\1<redacted>", message)
    return _COOKIE_HEADER.sub(r"\1<redacted>", message)


class RequestIdFilter(logging.Filter):
    """Stamp the ambient request id onto every record that lacks one.

    A filter rather than formatter code so an explicit `extra={"request_id":
    ...}` still wins: a background task that adopts a request's id on purpose
    should not have it overwritten by whatever context it happens to run in.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "request_id", None) is None:
            record.request_id = request_id_var.get()
        return True


class DropAllFilter(logging.Filter):
    """Silence one logger without detaching it from the pipeline.

    Used for `uvicorn.access` when access logging is switched off. Uvicorn's own
    `access_log=False` sets `handlers = []` *and* `propagate = False`, and the
    second half would quietly undo the collection done in `configure_logging`.
    Dropping records at the filter keeps the wiring intact.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return False


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": redact_sensitive_values(record.getMessage()),
        }
        # Where it was logged from. The cheapest possible triage: a warning
        # whose text is ambiguous is still attributable to a subsystem.
        payload["source"] = f"{record.module}:{record.lineno}"
        for field in _CONTEXT_FIELDS:
            value = getattr(record, field, None)
            if value is None:
                continue
            # Redacted like the message. `error_message` is provider text --
            # a qBittorrent refusal carries the URL it tried, and that URL may
            # carry a token -- so a field is no safer than the message it was
            # split out of. The numeric ids pass through untouched.
            payload[field] = (
                redact_sensitive_values(value)
                if isinstance(value, str)
                else value
            )
        if record.exc_info:
            # Redacted like everything else: an exception's message routinely
            # carries the URL that failed, and that URL may carry a token.
            payload["exception"] = redact_sensitive_values(
                self.formatException(record.exc_info)
            )
        if record.stack_info:
            payload["stack"] = redact_sensitive_values(
                self.formatStack(record.stack_info)
            )
        return json.dumps(payload, ensure_ascii=False)


def _build_file_handler(
    log_path: Path, *, max_bytes: int, backups: int
) -> logging.Handler | None:
    """A rotating file handler, or `None` when the directory is unusable.

    Retention must not be able to stop the service. A deployment whose data
    directory is read-only has a problem worth reporting, but refusing to start
    over it would turn a logging preference into an outage, so the caller falls
    back to stdout alone and says so.
    """
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=max_bytes,
            backupCount=backups,
            encoding="utf-8",
        )
    except OSError:
        return None
    handler.setFormatter(JsonFormatter())
    return handler


def configure_logging(
    *,
    level: str = "INFO",
    access_log: bool = True,
    log_dir: Path | None = None,
    file_max_bytes: int = 10 * 1024 * 1024,
    file_backups: int = 5,
    force: bool = False,
) -> None:
    """Install the JSON pipeline on the root logger and collect uvicorn's.

    Idempotent: calling it a second time is a no-op unless `force` is set. It
    used to run inside `create_app`, so every application built in a test
    session reset the root handlers.
    """
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    resolved = logging.getLevelNamesMapping().get(level.strip().upper())
    if resolved is None:
        resolved = logging.INFO

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(JsonFormatter())

    # Fed the formatter's own output, so the in-app tail, the file and stdout
    # carry byte-identical records -- redaction included, because it happens in
    # the formatter and there is only one of those.
    buffer_handler = BufferHandler(_BROKER)
    buffer_handler.setFormatter(JsonFormatter())

    root_logger = logging.getLogger()
    for existing in list(root_logger.handlers):
        root_logger.removeHandler(existing)
    root_logger.addHandler(stream_handler)
    root_logger.addHandler(buffer_handler)

    failed_log_dir: Path | None = None
    if log_dir is not None:
        file_handler = _build_file_handler(
            log_dir / "ehbot.log",
            max_bytes=file_max_bytes,
            backups=file_backups,
        )
        if file_handler is None:
            failed_log_dir = log_dir
        else:
            root_logger.addHandler(file_handler)

    for existing_filter in list(root_logger.filters):
        root_logger.removeFilter(existing_filter)
    root_logger.addFilter(RequestIdFilter())
    root_logger.setLevel(resolved)

    # Uvicorn installs its own handlers and turns propagation off, so without
    # this its output keeps a second format and skips redaction entirely.
    for name in _UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        for existing in list(uvicorn_logger.handlers):
            uvicorn_logger.removeHandler(existing)
        for existing_filter in list(uvicorn_logger.filters):
            uvicorn_logger.removeFilter(existing_filter)
        uvicorn_logger.propagate = True
        uvicorn_logger.setLevel(resolved)

    if not access_log:
        logging.getLogger("uvicorn.access").addFilter(DropAllFilter())

    _CONFIGURED = True

    if failed_log_dir is not None:
        logging.getLogger(__name__).warning(
            "log_file_unavailable_stdout_only",
            extra={"error_code": "LOG_FILE_UNAVAILABLE"},
        )
