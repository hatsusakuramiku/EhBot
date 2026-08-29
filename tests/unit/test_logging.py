import logging

from app.logging import JsonFormatter


def test_json_formatter_redacts_sensitive_values() -> None:
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=(
            "Authorization=Bearer-secret "
            "https://example.test/path?authkey=query-secret&safe=value "
            "Cookie: sid=session-secret; theme=dark"
        ),
        args=(),
        exc_info=None,
    )

    output = JsonFormatter().format(record)

    assert "Bearer-secret" not in output
    assert "session-secret" not in output
    assert "query-secret" not in output
    assert "value" not in output
    assert "theme" not in output


def test_json_formatter_redacts_telegram_bot_token_in_url_path() -> None:
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="HTTP Request: GET https://api.telegram.org/bot123:SECRET/getMe",
        args=(),
        exc_info=None,
    )

    output = JsonFormatter().format(record)

    assert "123:SECRET" not in output
    assert "/bot<redacted>/getMe" in output


import json
import logging
import sys
from pathlib import Path

import pytest

from app.logging import (
    DropAllFilter,
    JsonFormatter,
    RequestIdFilter,
    configure_logging,
    redact_sensitive_values,
    request_id_var,
)


@pytest.fixture
def restore_logging():
    """Snapshot root + uvicorn loggers so a test can reconfigure freely."""
    snapshot = {}
    for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        snapshot[name] = (
            list(lg.handlers),
            list(lg.filters),
            lg.propagate,
            lg.level,
            lg.disabled,
        )
    yield
    for name, (handlers, filters, propagate, level, disabled) in snapshot.items():
        lg = logging.getLogger(name)
        for h in list(lg.handlers):
            lg.removeHandler(h)
        for h in handlers:
            lg.addHandler(h)
        for fl in list(lg.filters):
            lg.removeFilter(fl)
        for fl in filters:
            lg.addFilter(fl)
        lg.propagate = propagate
        lg.level = level
        lg.disabled = disabled


def _make_record(name, level, msg, *, args=(), exc_info=None, stack_info=False, extra=None):
    record = logging.LogRecord(
        name=name, level=level, pathname="/p/m.py", lineno=42,
        msg=msg, args=args, exc_info=exc_info,
    )
    if stack_info:
        record.stack_info = "Stack (most recent call last):\n  File \"x\""
    if extra:
        for k, v in extra.items():
            setattr(record, k, v)
    return record


def test_json_formatter_includes_logger_and_source():
    payload = json.loads(JsonFormatter().format(
        _make_record("app.conversion.service", logging.WARNING, "stuck")
    ))
    assert payload["logger"] == "app.conversion.service"
    assert payload["source"] == "m:42"
    assert payload["level"] == "WARNING"
    assert payload["event"] == "stuck"


def test_json_formatter_carries_traceback_for_exception():
    try:
        1 / 0
    except ZeroDivisionError:
        record = _make_record(
            "app.downloads.service", logging.ERROR,
            "pack failed for /library/a.cbz?token=SECRET",
            exc_info=sys.exc_info(),
        )
    payload = json.loads(JsonFormatter().format(record))
    assert "ZeroDivisionError" in payload["exception"]
    assert "division by zero" in payload["exception"]
    assert "SECRET" not in payload["exception"]
    assert "token=SECRET" not in payload["event"]
    assert "?<redacted>" in payload["event"]


def test_json_formatter_includes_stack_info_when_requested():
    record = _make_record(
        "app.db", logging.INFO, "saved",
        stack_info=True,
    )
    payload = json.loads(JsonFormatter().format(record))
    assert "Stack" in payload["stack"]


def test_json_formatter_round_trips_context_fields():
    record = _make_record(
        "app.conversion.service", logging.INFO, "packed",
        extra={
            "request_id": "abc123",
            "candidate_id": 7,
            "work_id": 12,
            "job_id": 99,
            "source_type": "TELEGRAPH",
            "provider": "EH_TORRENT",
            "status": "COMPLETED",
            "attempt": 3,
            "duration_ms": 1450,
            "error_code": "OK",
        },
    )
    payload = json.loads(JsonFormatter().format(record))
    assert payload["request_id"] == "abc123"
    assert payload["candidate_id"] == 7
    assert payload["work_id"] == 12
    assert payload["job_id"] == 99
    assert payload["source_type"] == "TELEGRAPH"
    assert payload["provider"] == "EH_TORRENT"
    assert payload["status"] == "COMPLETED"
    assert payload["attempt"] == 3
    assert payload["duration_ms"] == 1450
    assert payload["error_code"] == "OK"


def test_json_formatter_omits_unset_context_fields():
    payload = json.loads(JsonFormatter().format(
        _make_record("app.x", logging.INFO, "ok")
    ))
    for field in (
        "request_id", "candidate_id", "work_id", "job_id", "source_type",
        "provider", "status", "attempt", "duration_ms", "error_code",
    ):
        assert field not in payload


def test_request_id_filter_stamps_context_var_onto_records():
    token = request_id_var.set("rid-deadbeef")
    try:
        record = logging.LogRecord("m", logging.INFO, "x", 1, "ok", (), None)
        RequestIdFilter().filter(record)
        assert record.request_id == "rid-deadbeef"
    finally:
        request_id_var.reset(token)


def test_request_id_filter_keeps_explicit_extra_winning():
    token = request_id_var.set("rid-from-context")
    try:
        record = logging.LogRecord("m", logging.INFO, "x", 1, "ok", (), None)
        record.request_id = "rid-from-extra"
        RequestIdFilter().filter(record)
        assert record.request_id == "rid-from-extra"
    finally:
        request_id_var.reset(token)


def test_drop_all_filter_rejects_every_record():
    assert DropAllFilter().filter(
        logging.LogRecord("m", logging.INFO, "x", 1, "ok", (), None)
    ) is False


def test_redact_sensitive_values_handles_empty_string():
    assert redact_sensitive_values("") == ""


def test_redact_sensitive_values_strips_bare_query_path():
    # The leading `/` (no protocol) must be picked up: an access log line
    # starts with `GET /candidates?...`, and only the unanchored form catches
    # it. The prior pattern missed this entirely.
    out = redact_sensitive_values("GET /candidates?search=x&token=SECRET HTTP/1.1")
    assert "SECRET" not in out
    assert "/candidates?<redacted>" in out


def test_redact_sensitive_values_redacts_token_in_exception_text():
    out = redact_sensitive_values(
        "Traceback ...\nrequests.exceptions.HTTPError: 401 Client Error: "
        "https://api.telegram.org/bot999:SECRET/getUpdates"
    )
    assert "999:SECRET" not in out
    assert "/bot<redacted>/getUpdates" in out


def test_configure_logging_is_idempotent(restore_logging):
    configure_logging(level="INFO", force=True)
    first = list(logging.getLogger().handlers)
    configure_logging(level="INFO")
    second = list(logging.getLogger().handlers)
    assert len(first) == len(second) == 1
    assert first[0] is second[0]


def test_configure_logging_takes_over_uvicorn_loggers(restore_logging):
    # Plant a fake handler that would survive a naive `handlers.clear()` on
    # the root but not on the per-logger takeover. If the takeover is skipped
    # the access log keeps its plain-text format and skips redaction.
    access = logging.getLogger("uvicorn.access")
    fake = logging.StreamHandler()
    access.addHandler(fake)
    access.propagate = False

    configure_logging(level="INFO", force=True)

    assert fake not in access.handlers
    assert access.propagate is True


def test_configure_logging_drops_access_when_disabled(restore_logging):
    configure_logging(level="INFO", access_log=False, force=True)
    access = logging.getLogger("uvicorn.access")
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, "x", 1, "GET /", (), None,
    )
    droppers = [fl for fl in access.filters if isinstance(fl, DropAllFilter)]
    assert droppers, "LOG_ACCESS=false should attach a DropAllFilter to uvicorn.access"
    assert all(not fl.filter(record) for fl in droppers)


def test_configure_logging_sets_root_level(restore_logging):
    configure_logging(level="DEBUG", force=True)
    assert logging.getLogger().level == logging.DEBUG


def test_configure_logging_falls_back_to_info_for_unknown_level(restore_logging):
    configure_logging(level="BANANAS", force=True)
    assert logging.getLogger().level == logging.INFO


def test_configure_logging_installs_file_handler_when_dir_writable(restore_logging, tmp_path):
    log_dir = tmp_path / "logs"
    configure_logging(level="INFO", log_dir=log_dir, force=True)
    file_handlers = [
        h for h in logging.getLogger().handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert len(file_handlers) == 1
    assert file_handlers[0].baseFilename.endswith("ehbot.log")


def _unwritable_path() -> Path:
    # A path that is *itself* an existing file cannot have child directories.
    # On both Windows and POSIX mkdir(<file>/x) raises NotADirectoryError /
    # FileExistsError; that is the failure mode the code must survive.
    import tempfile
    f = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
    f.close()
    return Path(f.name)


def test_configure_logging_falls_back_to_stdout_when_dir_unwritable(restore_logging):
    blocker = _unwritable_path()
    configure_logging(level="INFO", log_dir=blocker, force=True)
    assert not any(
        isinstance(h, logging.handlers.RotatingFileHandler)
        for h in logging.getLogger().handlers
    )
    blocker.unlink()


def test_configure_logging_no_log_dir_means_stdout_only(restore_logging):
    configure_logging(level="INFO", log_dir=None, force=True)
    assert not any(
        isinstance(h, logging.handlers.RotatingFileHandler)
        for h in logging.getLogger().handlers
    )


from app.logs.reader import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    LogEntry,
    _parse_line,
    clamp_limit,
    read_log_tail,
)


def _write_log(tmp_path: Path, lines: list[str]) -> Path:
    log = tmp_path / "ehbot.log"
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tmp_path


def test_parse_line_parses_json_record():
    line = json.dumps({
        "timestamp": "2026-08-29T01:00:00+00:00",
        "level": "INFO",
        "logger": "app.x",
        "event": "ok",
        "request_id": "rid",
        "job_id": 7,
        "candidate_id": 42,
        "error_code": "OK",
        "exception": "Traceback (most recent call last):\n  ...",
    })
    entry = _parse_line(line)
    assert entry.level == "INFO"
    assert entry.logger == "app.x"
    assert entry.event == "ok"
    assert entry.request_id == "rid"
    assert entry.job_id == 7
    assert entry.candidate_id == 42
    assert entry.error_code == "OK"
    assert entry.exception and "Traceback" in entry.exception
    assert entry.raw is None


def test_parse_line_keeps_unparseable_text_as_raw():
    entry = _parse_line("not json at all")
    assert entry.level == "LOG_OTHER"
    assert entry.raw == "not json at all"
    assert entry.event == ""


def test_parse_line_tolerates_non_dict_json():
    entry = _parse_line(json.dumps([1, 2, 3]))
    assert entry.level == "LOG_OTHER"
    assert entry.raw == "[1, 2, 3]"


def test_parse_line_coerces_bad_int_fields_to_none():
    entry = _parse_line(json.dumps({
        "timestamp": "", "level": "INFO", "logger": "x", "event": "e",
        "job_id": "not-a-number", "candidate_id": None,
    }))
    assert entry.job_id is None
    assert entry.candidate_id is None


def test_read_log_tail_returns_newest_first(tmp_path):
    log_dir = _write_log(tmp_path, [
        json.dumps({"timestamp": "t1", "level": "INFO", "logger": "a", "event": "first"}),
        json.dumps({"timestamp": "t2", "level": "INFO", "logger": "a", "event": "second"}),
        json.dumps({"timestamp": "t3", "level": "INFO", "logger": "a", "event": "third"}),
    ])
    entries, present = read_log_tail(log_dir, limit=10)
    assert present is True
    assert [e.event for e in entries] == ["third", "second", "first"]


def test_read_log_tail_reports_missing_file(tmp_path):
    entries, present = read_log_tail(tmp_path)
    assert entries == []
    assert present is False


def test_read_log_tail_caps_at_max_limit_when_requested(tmp_path):
    # Caller is expected to pass a clamped limit; a page that bypasses clamp
    # would let a hand-typed ?limit=100000 render an unbounded page. The
    # clamp itself lives in `clamp_limit`, exercised separately below.
    lines = [
        json.dumps({"timestamp": f"t{i}", "level": "INFO", "logger": "x", "event": str(i)})
        for i in range(MAX_LIMIT + 50)
    ]
    log_dir = _write_log(tmp_path, lines)
    entries, _ = read_log_tail(log_dir, limit=MAX_LIMIT)
    assert len(entries) == MAX_LIMIT


def test_clamp_limit_caps_requested_count_to_max():
    assert clamp_limit(10_000) == MAX_LIMIT


def test_clamp_limit_falls_back_to_default_for_missing_or_bad_input():
    assert clamp_limit(None) == DEFAULT_LIMIT
    assert clamp_limit("") == DEFAULT_LIMIT
    assert clamp_limit("not-a-number") == DEFAULT_LIMIT
    assert clamp_limit(0) == DEFAULT_LIMIT
    assert clamp_limit(-5) == DEFAULT_LIMIT


def test_clamp_limit_passes_through_small_values():
    assert clamp_limit(7) == 7
    assert clamp_limit("42") == 42


def test_read_log_tail_caps_at_requested_limit(tmp_path):
    lines = [
        json.dumps({"timestamp": f"t{i}", "level": "INFO", "logger": "x", "event": str(i)})
        for i in range(20)
    ]
    log_dir = _write_log(tmp_path, lines)
    entries, _ = read_log_tail(log_dir, limit=5)
    assert len(entries) == 5


def test_read_log_tail_filters_by_level(tmp_path):
    log_dir = _write_log(tmp_path, [
        json.dumps({"timestamp": "t1", "level": "INFO", "logger": "x", "event": "i1"}),
        json.dumps({"timestamp": "t2", "level": "WARNING", "logger": "x", "event": "w1"}),
        json.dumps({"timestamp": "t3", "level": "WARNING", "logger": "x", "event": "w2"}),
        json.dumps({"timestamp": "t4", "level": "ERROR", "logger": "x", "event": "e1"}),
    ])
    entries, _ = read_log_tail(log_dir, level="WARNING")
    assert [e.event for e in entries] == ["w2", "w1"]


def test_read_log_tail_default_limit_is_hundred(tmp_path):
    assert DEFAULT_LIMIT == 100


def test_read_log_tail_skips_unparseable_lines_without_dropping_others(tmp_path):
    log_dir = _write_log(tmp_path, [
        json.dumps({"timestamp": "t1", "level": "INFO", "logger": "x", "event": "good"}),
        "<<< partial write before crash >>>",
        json.dumps({"timestamp": "t2", "level": "ERROR", "logger": "x", "event": "bad"}),
    ])
    entries, _ = read_log_tail(log_dir, limit=10)
    # newest first, all three present, raw text kept verbatim
    assert entries[0].event == "bad"
    assert entries[1].raw and "partial write" in entries[1].raw
    assert entries[2].event == "good"

