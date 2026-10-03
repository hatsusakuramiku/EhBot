"""Operator-editable system preferences.

What belongs here and what does not
-----------------------------------
Five preferences are stored: how often the interface polls, how many preview
images are fetched at once, which timezone timestamps are read in, how often
the automatic-approval sweep runs, and the logging floor. Theme and density are deliberately absent --
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

from app.ai.models import (
    MODEL_SOURCE_DEFAULT,
    MODEL_SOURCES,
)
from app.ai.prompt import DEFAULT_CANDIDATE_PROMPT
from app.candidates.parse_rules import (
    PARSE_RULES_KEY,
    ParseRulesError,
    default_parse_rules,
    dump_parse_rules,
    parse_rules_view,
    validate_parse_rules,
)
from app.config import LOG_LEVEL_CHOICES
from app.db.database import Database
from app.downloads.models import AUTO_DOWNLOAD_PROVIDERS


SETTING_POLL_INTERVAL_MS = "poll_interval_ms"
SETTING_SOURCE_CONCURRENCY = "source_concurrency"
SETTING_TIMEZONE = "timezone"
SETTING_AUTO_APPROVAL_INTERVAL_MINUTES = "auto_approval_interval_minutes"
SETTING_LOG_LEVEL = "log_level"
SETTING_DOWNLOAD_SOURCE_PRIORITY = "download_source_priority"

#: The AI candidate-admission switches. All four are one decision -- 「要不要让
#: 模型先看一遍消息」 -- so they are read together and saved from one page.
SETTING_AI_CANDIDATE_ENABLED = "ai_candidate_enabled"
SETTING_AI_CANDIDATE_PROMPT = "ai_candidate_prompt"
SETTING_AI_CANDIDATE_OVERRIDE = "ai_candidate_override_parse_rules"
SETTING_AI_CANDIDATE_FALLBACK = "ai_candidate_fallback"
#: Which models the gate asks: the global default chain, or a list of its own.
#: Same vocabulary and semantics as the archive-path switch (`ai_model_source`).
SETTING_AI_CANDIDATE_MODEL_SOURCE = "ai_candidate_model_source"

#: The master switch over every AI feature. `1` (on) by default so an upgrade
#: changes nothing; off means 「不产生任何 AI 调用」 while every per-feature
#: switch and stored chain stays exactly where the operator left it. 生效 =
#: 总开关 AND 本功能开关.
SETTING_AI_ENABLED = "ai_enabled"
DEFAULT_AI_ENABLED = True

#: What to do when the gate is on but the chain cannot answer. `reject` keeps
#: the gate's intent (keep things out) on error; `accept` is for an operator who
#: would rather review a stray message than lose a book to a timeout.
AI_CANDIDATE_FALLBACKS: tuple[str, ...] = ("reject", "accept")
DEFAULT_AI_CANDIDATE_FALLBACK = "reject"

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
DEFAULT_LOG_LEVEL = "INFO"

#: The order the router tries download sources in. Stored as the codes joined
#: by commas, because it is one decision -- 「谁先谁后」 -- and a table for four
#: values would make the page read as four unrelated switches. EXHENTAI is not
#: offered: it spends GP and stays a per-work decision.
DEFAULT_DOWNLOAD_SOURCE_PRIORITY: tuple[str, ...] = AUTO_DOWNLOAD_PROVIDERS

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
    """Read and write the operator-editable system preferences."""

    def __init__(
        self,
        database: Database,
        *,
        default_source_concurrency: int = 3,
        default_log_level: str = DEFAULT_LOG_LEVEL,
    ) -> None:
        self._database = database
        # The environment still supplies the starting value, so a deployment
        # that tuned `TELEGRAPH_CONCURRENCY` keeps its number until an operator
        # overrides it here.
        self._default_source_concurrency = default_source_concurrency
        candidate_level = default_log_level.strip().upper()
        self._default_log_level = (
            candidate_level
            if candidate_level in LOG_LEVEL_CHOICES
            else DEFAULT_LOG_LEVEL
        )

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
        download_source_priority = _read_priority(
            stored.get(SETTING_DOWNLOAD_SOURCE_PRIORITY, "")
        )
        timezone = stored.get(SETTING_TIMEZONE, "").strip() or DEFAULT_TIMEZONE
        if not _TIMEZONE_PATTERN.match(timezone):
            timezone = DEFAULT_TIMEZONE
        log_level = stored.get(SETTING_LOG_LEVEL, "").strip().upper()
        if log_level not in LOG_LEVEL_CHOICES:
            log_level = self._default_log_level
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
            "ai_enabled": _read_bool(
                stored, SETTING_AI_ENABLED, DEFAULT_AI_ENABLED
            ),
            "poll_interval_ms": poll_interval_ms,
            # A background tab must never poll faster than a foreground one, so
            # the floor is the active interval rather than the constant.
            "idle_poll_interval_ms": max(
                poll_interval_ms, DEFAULT_IDLE_POLL_INTERVAL_MS
            ),
            "source_concurrency": concurrency,
            "timezone": timezone,
            "log_level": log_level,
            "log_access": log_level == "DEBUG",
            "auto_approval_interval_minutes": auto_approval_interval_minutes,
            "download_source_priority": list(download_source_priority),
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
            "log_level_overridden": bool(
                stored.get(SETTING_LOG_LEVEL, "").strip()
            ),
            "auto_approval_interval_overridden": bool(
                stored.get(SETTING_AUTO_APPROVAL_INTERVAL_MINUTES, "").strip()
            ),
            "download_source_priority_overridden": bool(
                stored.get(SETTING_DOWNLOAD_SOURCE_PRIORITY, "").strip()
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

    async def log_level(self) -> str:
        return str((await self.snapshot())["log_level"])

    async def auto_approval_interval_minutes(self) -> int:
        return int((await self.snapshot())["auto_approval_interval_minutes"])

    async def download_source_priority(self) -> tuple[str, ...]:
        return tuple(
            (await self.snapshot())["download_source_priority"]
        )

    async def ai_enabled(self) -> bool:
        """Whether any AI feature may run at all (master switch)."""
        return bool((await self.snapshot())["ai_enabled"])

    async def save_ai_enabled(self, enabled: bool) -> dict[str, object]:
        await self._database.save_system_settings(
            {SETTING_AI_ENABLED: "1" if enabled else "0"}
        )
        return await self.snapshot()

    async def parse_rules(self) -> dict[str, object]:
        """The candidate-admission parse scheme, always in the full shape."""
        stored = await self._database.system_settings()
        return parse_rules_view(stored.get(PARSE_RULES_KEY))

    async def save_parse_rules(self, raw: object) -> dict[str, object]:
        """Validate and store a submitted scheme, or refuse with the reason."""
        try:
            rules = validate_parse_rules(raw)  # type: ignore[arg-type]
        except ParseRulesError as exc:
            raise SystemSettingsError(exc.code, exc.public_message) from exc
        await self._database.save_system_settings(
            {PARSE_RULES_KEY: dump_parse_rules(rules)}
        )
        return await self.parse_rules()

    async def reset_parse_rules(self) -> dict[str, object]:
        """Drop the stored scheme, returning the shipped default."""
        await self._database.save_system_settings(
            {PARSE_RULES_KEY: dump_parse_rules(default_parse_rules())}
        )
        return await self.parse_rules()

    async def candidate_admission(self) -> dict[str, object]:
        """The AI candidate gate, read leniently and always complete.

        An empty prompt reads back as the shipped default rather than as 「ask
        nothing」: a blank system message would be a request the model cannot
        answer, and the page shows the default in the box so the operator can
        see what a cleared field means.
        """
        stored = await self._database.system_settings()
        fallback = stored.get(SETTING_AI_CANDIDATE_FALLBACK, "").strip().lower()
        if fallback not in AI_CANDIDATE_FALLBACKS:
            fallback = DEFAULT_AI_CANDIDATE_FALLBACK
        prompt = stored.get(SETTING_AI_CANDIDATE_PROMPT, "").strip()
        source = (
            stored.get(SETTING_AI_CANDIDATE_MODEL_SOURCE, "").strip().lower()
        )
        if source not in MODEL_SOURCES:
            source = MODEL_SOURCE_DEFAULT
        return {
            "enabled": _read_bool(stored, SETTING_AI_CANDIDATE_ENABLED),
            "prompt": prompt or DEFAULT_CANDIDATE_PROMPT,
            "override_parse_rules": _read_bool(
                stored, SETTING_AI_CANDIDATE_OVERRIDE
            ),
            "fallback": fallback,
            "model_source": source,
            "prompt_overridden": bool(prompt),
            "enabled_overridden": bool(
                stored.get(SETTING_AI_CANDIDATE_ENABLED, "").strip()
            ),
            "override_overridden": bool(
                stored.get(SETTING_AI_CANDIDATE_OVERRIDE, "").strip()
            ),
            "model_source_overridden": bool(
                stored.get(SETTING_AI_CANDIDATE_MODEL_SOURCE, "").strip()
            ),
        }

    async def ai_candidate_model_source(self) -> str:
        """Where the gate's models come from (read by `AiProviderService`).

        A reader method rather than only a field of `candidate_admission()` so
        the provider-service registry can ask this one question without loading
        the prompt and the switches on every call.
        """
        return str((await self.candidate_admission())["model_source"])

    async def save_ai_candidate_model_source(
        self, raw: str
    ) -> dict[str, object]:
        value = (raw or "").strip().lower()
        if value not in MODEL_SOURCES:
            raise SystemSettingsError(
                "AI_MODEL_SOURCE_INVALID", "模型来源取值无效"
            )
        await self._database.save_system_settings(
            {SETTING_AI_CANDIDATE_MODEL_SOURCE: value}
        )
        return await self.candidate_admission()

    async def save_candidate_admission(
        self, values: dict[str, object]
    ) -> dict[str, object]:
        """Store whichever admission switches the form submitted.

        A key the form left out is untouched, so the two halves of the page (the
        parse scheme and the AI gate) can be saved independently without one
        clearing the other.
        """
        cleaned: dict[str, str] = {}
        if SETTING_AI_CANDIDATE_ENABLED in values:
            cleaned[SETTING_AI_CANDIDATE_ENABLED] = (
                "1" if _truthy(values[SETTING_AI_CANDIDATE_ENABLED]) else "0"
            )
        if SETTING_AI_CANDIDATE_OVERRIDE in values:
            cleaned[SETTING_AI_CANDIDATE_OVERRIDE] = (
                "1" if _truthy(values[SETTING_AI_CANDIDATE_OVERRIDE]) else "0"
            )
        if SETTING_AI_CANDIDATE_PROMPT in values:
            cleaned[SETTING_AI_CANDIDATE_PROMPT] = str(
                values[SETTING_AI_CANDIDATE_PROMPT] or ""
            ).strip()
        if SETTING_AI_CANDIDATE_FALLBACK in values:
            fallback = str(values[SETTING_AI_CANDIDATE_FALLBACK] or "").strip().lower()
            if fallback not in AI_CANDIDATE_FALLBACKS:
                raise SystemSettingsError(
                    "AI_CANDIDATE_FALLBACK_INVALID",
                    "兜底动作必须是 reject 或 accept",
                )
            cleaned[SETTING_AI_CANDIDATE_FALLBACK] = fallback
        if cleaned:
            await self._database.save_system_settings(cleaned)
        return await self.candidate_admission()

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
        if SETTING_DOWNLOAD_SOURCE_PRIORITY in values:
            cleaned[SETTING_DOWNLOAD_SOURCE_PRIORITY] = ",".join(
                _validate_priority(
                    values[SETTING_DOWNLOAD_SOURCE_PRIORITY]
                )
            )
        if SETTING_LOG_LEVEL in values:
            level = str(values[SETTING_LOG_LEVEL] or "").strip().upper()
            if level and level not in LOG_LEVEL_CHOICES:
                raise SystemSettingsError(
                    "LOG_LEVEL_INVALID", "日志等级必须是 DEBUG、INFO、WARNING 或 ERROR"
                )
            cleaned[SETTING_LOG_LEVEL] = level
        if cleaned:
            await self._database.save_system_settings(cleaned)
        return await self.snapshot()


def _read_bool(stored: dict[str, str], key: str, default: bool = False) -> bool:
    """A stored on/off value, read leniently.

    Absent means the default; anything the form (or an older build) might have
    written as "on" counts as on, and everything else is off. Reads never raise
    for the same reason every other preference read does not.
    """
    raw = str(stored.get(key, "")).strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "on", "yes"}


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "on", "yes"}


def _read_priority(raw: object) -> tuple[str, ...]:
    """The stored order, with anything unrecognised dropped and the rest appended.

    Read leniently for the same reason every other stored preference is: a value
    written by a newer version, or edited by hand, must not take the page or the
    router down. A code the router does not know is dropped; a code it does know
    but that the row omits (an older row, a hand-edited one) keeps its default
    place at the end rather than disappearing from routing altogether.
    """
    submitted: list[str] = []
    for item in str(raw or "").split(","):
        code = item.strip().upper()
        if code in AUTO_DOWNLOAD_PROVIDERS and code not in submitted:
            submitted.append(code)
    return tuple(
        submitted
        + [code for code in DEFAULT_DOWNLOAD_SOURCE_PRIORITY if code not in submitted]
    )


def _validate_priority(raw: object) -> tuple[str, ...]:
    """Normalise a submitted order, refusing a code the router cannot use.

    Unknown codes are an error rather than something to drop silently: the form
    renders a checkbox per known source, so an unknown one means the request was
    not built by this page, and quietly ignoring it would leave the operator
    believing a source had been positioned when it had not.
    """
    codes: list[str] = []
    for item in str(raw or "").split(","):
        code = item.strip().upper()
        if not code:
            continue
        if code not in AUTO_DOWNLOAD_PROVIDERS:
            raise SystemSettingsError(
                "DOWNLOAD_SOURCE_PRIORITY_INVALID",
                f"未知的下载来源：{code}",
            )
        if code in codes:
            raise SystemSettingsError(
                "DOWNLOAD_SOURCE_PRIORITY_INVALID",
                f"下载来源重复：{code}",
            )
        codes.append(code)
    if not codes:
        raise SystemSettingsError(
            "DOWNLOAD_SOURCE_PRIORITY_INVALID",
            "下载来源优先级至少要保留一个来源",
        )
    return tuple(
        codes
        + [code for code in DEFAULT_DOWNLOAD_SOURCE_PRIORITY if code not in codes]
    )


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
    "DEFAULT_LOG_LEVEL",
    "DEFAULT_POLL_INTERVAL_MS",
    "DEFAULT_TIMEZONE",
    "MAX_AUTO_APPROVAL_INTERVAL_MINUTES",
    "MAX_POLL_INTERVAL_MS",
    "MAX_SOURCE_CONCURRENCY",
    "MIN_AUTO_APPROVAL_INTERVAL_MINUTES",
    "MIN_POLL_INTERVAL_MS",
    "MIN_SOURCE_CONCURRENCY",
    "SETTING_AUTO_APPROVAL_INTERVAL_MINUTES",
    "SETTING_LOG_LEVEL",
    "SETTING_POLL_INTERVAL_MS",
    "SETTING_SOURCE_CONCURRENCY",
    "SETTING_TIMEZONE",
    "SystemSettingsError",
    "SystemSettingsService",
]
