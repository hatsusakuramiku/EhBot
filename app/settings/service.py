"""Operator-editable system preferences.

What belongs here and what does not
-----------------------------------
Four preferences are stored: how often the interface polls, how many preview
images are fetched at once, which timezone timestamps are read in, and how often
the automatic-approval sweep runs. Theme and density are deliberately absent --
they live in `localStorage`, per browser, because they answer 「这块屏幕看起来怎
样」 rather than 「这个部署怎么运行」, and a server-stored theme would follow an
operator onto a screen where it is wrong.

Every value has a default in this module, so a missing row means "the default"
rather than "unset": there is no state in which the interface has no polling
cadence at all. That is also why reads never raise -- a value stored by an older
version, or edited in the database by hand, falls back rather than taking the
page down with it. Writes are the strict half: `save_*` rejects what it cannot
store and says why, because that is the moment an operator can fix it.
"""

from __future__ import annotations

import re

from app.db.database import Database


SETTING_POLL_INTERVAL_MS = "poll_interval_ms"
SETTING_SOURCE_CONCURRENCY = "source_concurrency"
SETTING_TIMEZONE = "timezone"
SETTING_AUTO_APPROVAL_INTERVAL_MINUTES = "auto_approval_interval_minutes"

#: Visible-tab polling cadence. 2s matches what `/api/v1/meta` served as a
#: constant before this was editable, so an operator who never opens the
#: settings page sees no change.
DEFAULT_POLL_INTERVAL_MS = 2000

#: Floor and ceiling. Below 500ms the interface would hammer the server for no
#: gain -- the event stream is the primary signal and polling is only the
#: fallback for a proxy that buffers it -- and above a minute the fallback is
#: slow enough that an operator would call the page broken.
MIN_POLL_INTERVAL_MS = 500
MAX_POLL_INTERVAL_MS = 60_000

#: Idle-tab cadence, applied when the polling client is in a background tab.
#: Derived rather than stored: it is the same decision as the visible interval
#: seen from further away, and a second field would let an operator set an idle
#: cadence faster than the active one.
DEFAULT_IDLE_POLL_INTERVAL_MS = 15_000

#: How many preview-page images are fetched at once. This is the only genuine
#: concurrency ceiling in the process -- both job workers claim one job at a
#: time -- so it is what the 并发上限 control sets, named for the source it
#: actually bounds rather than pretending to be a global limit.
MIN_SOURCE_CONCURRENCY = 1
MAX_SOURCE_CONCURRENCY = 16

#: How often the automatic-approval sweep re-reads the pending queue, in
#: minutes. It exists because a rule used to fire only while somebody had the
#: 待审核 page open: approval was a side effect of rendering, so a deployment
#: nobody was watching approved nothing. The sweep is the unattended path and
#: the page render is now only an optimisation on top of it.
DEFAULT_AUTO_APPROVAL_INTERVAL_MINUTES = 30

#: Zero is a real value and means 「不要自动跑」 -- an operator who wants rules to
#: fire only when they are looking has to be able to say so, and deleting every
#: rule is not the same statement. The ceiling is a day because an interval
#: measured in weeks is indistinguishable from off, and 「off」 already has a
#: value.
MIN_AUTO_APPROVAL_INTERVAL_MINUTES = 0
MAX_AUTO_APPROVAL_INTERVAL_MINUTES = 1440

DEFAULT_TIMEZONE = "UTC"

#: An IANA zone name: `UTC`, or `Area/Location` with at most one further level
#: (`America/Argentina/Salta`). The name is validated by shape rather than
#: against `zoneinfo.available_timezones()` because a slim container may carry no
#: tz database at all, and the rendering that uses this happens in the browser,
#: which always has the full list. Shape is what keeps the value from being
#: something other than a zone name.
_TIMEZONE_PATTERN = re.compile(
    r"^[A-Za-z][A-Za-z0-9+_-]*(?:/[A-Za-z0-9+_.-]+){0,2}$"
)


class SystemSettingsError(ValueError):
    """A system preference an operator may not save."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message


def _read_int(stored: dict[str, str], key: str, default: int) -> int:
    raw = stored.get(key, "")
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value


class SystemSettingsService:
    """Read and write the three system preferences."""

    def __init__(
        self, database: Database, *, default_source_concurrency: int = 3
    ) -> None:
        self._database = database
        # The environment still supplies the starting value, so a deployment
        # that tuned `TELEGRAPH_CONCURRENCY` keeps its number until an operator
        # overrides it here.
        self._default_source_concurrency = default_source_concurrency

    async def snapshot(self) -> dict[str, object]:
        """Every preference, clamped, plus the derived idle cadence."""
        stored = await self._database.system_settings()
        poll_interval_ms = min(
            max(
                _read_int(
                    stored,
                    SETTING_POLL_INTERVAL_MS,
                    DEFAULT_POLL_INTERVAL_MS,
                ),
                MIN_POLL_INTERVAL_MS,
            ),
            MAX_POLL_INTERVAL_MS,
        )
        concurrency = min(
            max(
                _read_int(
                    stored,
                    SETTING_SOURCE_CONCURRENCY,
                    self._default_source_concurrency,
                ),
                MIN_SOURCE_CONCURRENCY,
            ),
            MAX_SOURCE_CONCURRENCY,
        )
        timezone = stored.get(SETTING_TIMEZONE, "").strip() or DEFAULT_TIMEZONE
        if not _TIMEZONE_PATTERN.match(timezone):
            timezone = DEFAULT_TIMEZONE
        auto_approval_interval_minutes = min(
            max(
                _read_int(
                    stored,
                    SETTING_AUTO_APPROVAL_INTERVAL_MINUTES,
                    DEFAULT_AUTO_APPROVAL_INTERVAL_MINUTES,
                ),
                MIN_AUTO_APPROVAL_INTERVAL_MINUTES,
            ),
            MAX_AUTO_APPROVAL_INTERVAL_MINUTES,
        )
        return {
            "poll_interval_ms": poll_interval_ms,
            # A background tab must never poll faster than a foreground one, so
            # the floor is the active interval rather than the constant.
            "idle_poll_interval_ms": max(
                poll_interval_ms, DEFAULT_IDLE_POLL_INTERVAL_MS
            ),
            "source_concurrency": concurrency,
            "timezone": timezone,
            "auto_approval_interval_minutes": auto_approval_interval_minutes,
            # Whether the operator has moved this off the default. A row holding
            # an empty string is not an override -- that is how a cleared field is
            # stored, and `_read_int` reads it back as the default.
            "poll_interval_overridden": bool(
                stored.get(SETTING_POLL_INTERVAL_MS, "").strip()
            ),
            "source_concurrency_overridden": bool(
                stored.get(SETTING_SOURCE_CONCURRENCY, "").strip()
            ),
            "timezone_overridden": bool(
                stored.get(SETTING_TIMEZONE, "").strip()
            ),
            "auto_approval_interval_overridden": bool(
                stored.get(SETTING_AUTO_APPROVAL_INTERVAL_MINUTES, "").strip()
            ),
        }

    async def poll_interval_ms(self) -> int:
        return int((await self.snapshot())["poll_interval_ms"])

    async def idle_poll_interval_ms(self) -> int:
        return int((await self.snapshot())["idle_poll_interval_ms"])

    async def source_concurrency(self) -> int:
        return int((await self.snapshot())["source_concurrency"])

    async def timezone(self) -> str:
        return str((await self.snapshot())["timezone"])

    async def auto_approval_interval_minutes(self) -> int:
        return int((await self.snapshot())["auto_approval_interval_minutes"])

    async def save(self, values: dict[str, str]) -> dict[str, object]:
        """Validate and store whichever preferences the form submitted.

        A field the form left out is not touched, and a field submitted empty
        clears the override back to the default -- the same contract the archive
        path overrides use, so the two settings pages behave alike.
        """
        cleaned: dict[str, str] = {}
        if SETTING_POLL_INTERVAL_MS in values:
            cleaned[SETTING_POLL_INTERVAL_MS] = _validate_bounded_int(
                values[SETTING_POLL_INTERVAL_MS],
                minimum=MIN_POLL_INTERVAL_MS,
                maximum=MAX_POLL_INTERVAL_MS,
                code="POLL_INTERVAL_INVALID",
                label="轮询间隔",
                unit="毫秒",
            )
        if SETTING_SOURCE_CONCURRENCY in values:
            cleaned[SETTING_SOURCE_CONCURRENCY] = _validate_bounded_int(
                values[SETTING_SOURCE_CONCURRENCY],
                minimum=MIN_SOURCE_CONCURRENCY,
                maximum=MAX_SOURCE_CONCURRENCY,
                code="CONCURRENCY_INVALID",
                label="并发上限",
                unit="",
            )
        if SETTING_AUTO_APPROVAL_INTERVAL_MINUTES in values:
            cleaned[SETTING_AUTO_APPROVAL_INTERVAL_MINUTES] = (
                _validate_bounded_int(
                    values[SETTING_AUTO_APPROVAL_INTERVAL_MINUTES],
                    minimum=MIN_AUTO_APPROVAL_INTERVAL_MINUTES,
                    maximum=MAX_AUTO_APPROVAL_INTERVAL_MINUTES,
                    code="AUTO_APPROVAL_INTERVAL_INVALID",
                    label="自动审批间隔",
                    unit="分钟",
                )
            )
        if SETTING_TIMEZONE in values:
            raw = str(values[SETTING_TIMEZONE] or "").strip()
            if raw and not _TIMEZONE_PATTERN.match(raw):
                raise SystemSettingsError(
                    "TIMEZONE_INVALID",
                    "时区必须是 IANA 名称，例如 Asia/Shanghai",
                )
            cleaned[SETTING_TIMEZONE] = raw
        if cleaned:
            await self._database.save_system_settings(cleaned)
        return await self.snapshot()


def _validate_bounded_int(
    raw: object,
    *,
    minimum: int,
    maximum: int,
    code: str,
    label: str,
    unit: str,
) -> str:
    """Parse one integer preference, or refuse it with the bound it broke.

    An empty submission is stored as an empty string rather than rejected: that
    is how a form clears an override, and `snapshot` reads an unparsable value
    as the default.
    """
    text = str(raw or "").strip()
    if text == "":
        return ""
    try:
        value = int(text)
    except ValueError as exc:
        raise SystemSettingsError(code, f"{label}必须是整数") from exc
    if value < minimum or value > maximum:
        raise SystemSettingsError(
            code, f"{label}必须在 {minimum} 到 {maximum}{unit} 之间"
        )
    return str(value)


__all__ = [
    "DEFAULT_AUTO_APPROVAL_INTERVAL_MINUTES",
    "DEFAULT_IDLE_POLL_INTERVAL_MS",
    "DEFAULT_POLL_INTERVAL_MS",
    "DEFAULT_TIMEZONE",
    "MAX_AUTO_APPROVAL_INTERVAL_MINUTES",
    "MAX_POLL_INTERVAL_MS",
    "MAX_SOURCE_CONCURRENCY",
    "MIN_AUTO_APPROVAL_INTERVAL_MINUTES",
    "MIN_POLL_INTERVAL_MS",
    "MIN_SOURCE_CONCURRENCY",
    "SETTING_AUTO_APPROVAL_INTERVAL_MINUTES",
    "SETTING_POLL_INTERVAL_MS",
    "SETTING_SOURCE_CONCURRENCY",
    "SETTING_TIMEZONE",
    "SystemSettingsError",
    "SystemSettingsService",
]
