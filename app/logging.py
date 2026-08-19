from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime


_AUTHORIZATION = re.compile(
    r"(?i)(\bauthorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+"
)
_SENSITIVE_VALUE = re.compile(
    r"(?i)(\b(?:cookie|token|api[_-]?hash|ipb_member_id|ipb_pass_hash|igneous)"
    r"\s*[:=]\s*)[^\s,;&]+"
)


def redact_sensitive_values(message: str) -> str:
    message = _AUTHORIZATION.sub(r"\1<redacted>", message)
    return _SENSITIVE_VALUE.sub(r"\1<redacted>", message)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "event": redact_sensitive_values(record.getMessage()),
        }
        for field in (
            "candidate_id",
            "job_id",
            "source_type",
            "duration_ms",
            "error_code",
        ):
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)
