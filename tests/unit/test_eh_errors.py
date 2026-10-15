from datetime import UTC, datetime

import httpx
import pytest

from app.exhentai.errors import retry_after_seconds, credential_rejected, response_limit


@pytest.mark.parametrize("value,expected", [(None, 60), ("invalid", 60), ("-2", 60), ("0", 1), ("120", 120),
    ("Thu, 15 Oct 2026 12:01:30 GMT", 90), ("Thu, 15 Oct 2026 11:00:00 GMT", 1)])
def test_retry_after_delta_and_http_date(value, expected):
    assert retry_after_seconds(value, now=datetime(2026, 10, 15, 12, tzinfo=UTC)) == expected


def test_a_gallery_title_containing_login_or_limit_words_is_not_an_error_page():
    response = httpx.Response(200, text='<html>ExHentai<h1 id="gn">Login required: excessive pageloads</h1></html>')
    assert not credential_rejected(response)
    assert response_limit(response) is None
