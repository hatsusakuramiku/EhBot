import logging

from app.logging import JsonFormatter


def test_json_formatter_redacts_sensitive_values() -> None:
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=(
            "Authorization=Bearer-secret Cookie:session-secret "
            "https://example.test/path?token=query-secret&safe=value"
        ),
        args=(),
        exc_info=None,
    )

    output = JsonFormatter().format(record)

    assert "Bearer-secret" not in output
    assert "session-secret" not in output
    assert "query-secret" not in output
    assert "value" in output
