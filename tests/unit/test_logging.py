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
