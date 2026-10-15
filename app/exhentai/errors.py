"""Safe EH failures and HTTP Retry-After parsing; never retain response bodies."""
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
import math
import re


DEFAULT_RETRY_AFTER = 60


def retry_after_seconds(value: str | None, *, now: datetime | None = None) -> int:
    """Return a positive delay; missing/invalid values use sixty seconds."""
    text = (value or "").strip()
    if text.isascii() and text.isdecimal() and len(text) <= 9:
        return max(1, int(text))
    try:
        target = parsedate_to_datetime(text)
        if target.tzinfo is None:
            target = target.replace(tzinfo=UTC)
        return max(1, math.ceil((target - (now or datetime.now(UTC))).total_seconds()))
    except (ValueError, TypeError, OverflowError):
        return DEFAULT_RETRY_AFTER


class ExHentaiDownloadError(ValueError):
    def __init__(self, code: str, message: str, *, failure_scope: str = "operation", retry_after: int | None = None):
        super().__init__(message)
        self.code = code
        self.public_message = message
        self.failure_scope = failure_scope
        self.retry_after = retry_after


class GdataError(ExHentaiDownloadError):
    pass


def has_gallery_heading(document: str) -> bool:
    return re.search(r'''<h1\b[^>]*\bid\s*=\s*["']g[nj]["']''', document, re.IGNORECASE) is not None


def credential_rejected(response) -> bool:
    if response.status_code == 401:
        return True
    return not has_gallery_heading(response.text) and any(marker in response.text.lower() for marker in (
        "you must be logged in", "login required", "not logged in"))


def response_limit(response, *, scope="site") -> ExHentaiDownloadError | None:
    """Recognize shared throttling; a generic rejection is not a bad Cookie."""
    text = response.text.lower()
    if (response.status_code == 429
            or (not has_gallery_heading(response.text) and (
                "your ip address has been temporarily banned" in text or "excessive pageloads" in text))
            or (response.status_code == 503 and response.headers.get("Retry-After"))):
        return ExHentaiDownloadError(
            "EXHENTAI_RATE_LIMITED", "EH 请求受到限制，请等待共享冷却结束后重试",
            failure_scope=scope, retry_after=retry_after_seconds(response.headers.get("Retry-After")),
        )
    return None
