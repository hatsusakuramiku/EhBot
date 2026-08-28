"""The three system preferences: what a save accepts, what a read guarantees.

Reads and writes are deliberately asymmetric and both halves are tested here.
`snapshot` never raises -- a value stored by an older version or edited into the
database by hand falls back, because a bad row must not take every page down with
it -- while `save` refuses what it cannot store and says which bound was broken,
because that is the one moment an operator can fix it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.db.database import Database
from app.settings.service import (
    DEFAULT_IDLE_POLL_INTERVAL_MS,
    DEFAULT_POLL_INTERVAL_MS,
    DEFAULT_TIMEZONE,
    MAX_POLL_INTERVAL_MS,
    MAX_SOURCE_CONCURRENCY,
    MIN_POLL_INTERVAL_MS,
    MIN_SOURCE_CONCURRENCY,
    SETTING_POLL_INTERVAL_MS,
    SETTING_SOURCE_CONCURRENCY,
    SETTING_TIMEZONE,
    SystemSettingsError,
    SystemSettingsService,
)


async def service(tmp_path: Path, **kwargs) -> SystemSettingsService:
    database = Database(tmp_path / "ehbot.db")
    await database.initialize()
    return SystemSettingsService(database, **kwargs)


class TestDefaults:
    @pytest.mark.asyncio
    async def test_an_empty_store_reads_as_the_defaults(
        self, tmp_path: Path
    ) -> None:
        """A missing row means 「默认值」, never 「没有设置」."""
        snapshot = await (await service(tmp_path)).snapshot()

        assert snapshot["poll_interval_ms"] == DEFAULT_POLL_INTERVAL_MS
        assert snapshot["source_concurrency"] == 3
        assert snapshot["timezone"] == DEFAULT_TIMEZONE
        assert snapshot["poll_interval_overridden"] is False
        assert snapshot["source_concurrency_overridden"] is False
        assert snapshot["timezone_overridden"] is False

    @pytest.mark.asyncio
    async def test_the_environment_supplies_the_starting_concurrency(
        self, tmp_path: Path
    ) -> None:
        """A deployment that tuned `TELEGRAPH_CONCURRENCY` keeps its number."""
        settings = await service(tmp_path, default_source_concurrency=8)

        assert (await settings.snapshot())["source_concurrency"] == 8

    @pytest.mark.asyncio
    async def test_the_idle_cadence_never_polls_faster_than_the_visible_one(
        self, tmp_path: Path
    ) -> None:
        """A background tab hammering the server harder than a watched one is
        the defect a second stored field would allow."""
        settings = await service(tmp_path)
        await settings.save({SETTING_POLL_INTERVAL_MS: "45000"})

        snapshot = await settings.snapshot()
        assert snapshot["idle_poll_interval_ms"] == 45_000

        await settings.save({SETTING_POLL_INTERVAL_MS: "1000"})
        assert (await settings.snapshot())["idle_poll_interval_ms"] == (
            DEFAULT_IDLE_POLL_INTERVAL_MS
        )


class TestReadsNeverRaise:
    @pytest.mark.asyncio
    async def test_an_unparsable_stored_value_reads_as_the_default(
        self, tmp_path: Path
    ) -> None:
        settings = await service(tmp_path)
        await settings._database.save_system_settings(  # noqa: SLF001
            {
                SETTING_POLL_INTERVAL_MS: "soon",
                SETTING_SOURCE_CONCURRENCY: "",
                SETTING_TIMEZONE: "Mars/Olympus Mons",
            }
        )

        snapshot = await settings.snapshot()
        assert snapshot["poll_interval_ms"] == DEFAULT_POLL_INTERVAL_MS
        assert snapshot["source_concurrency"] == 3
        # A name that is not a zone name is not passed to the browser to guess
        # at: the display falls back to UTC, which is at least unambiguous.
        assert snapshot["timezone"] == DEFAULT_TIMEZONE

    @pytest.mark.asyncio
    async def test_a_stored_value_outside_the_bounds_is_clamped(
        self, tmp_path: Path
    ) -> None:
        """`save` cannot produce these, an older version or a hand edit can."""
        settings = await service(tmp_path)
        await settings._database.save_system_settings(  # noqa: SLF001
            {
                SETTING_POLL_INTERVAL_MS: "1",
                SETTING_SOURCE_CONCURRENCY: "9000",
            }
        )

        snapshot = await settings.snapshot()
        assert snapshot["poll_interval_ms"] == MIN_POLL_INTERVAL_MS
        assert snapshot["source_concurrency"] == MAX_SOURCE_CONCURRENCY


class TestSaving:
    @pytest.mark.asyncio
    async def test_a_save_returns_the_snapshot_it_produced(
        self, tmp_path: Path
    ) -> None:
        settings = await service(tmp_path)
        saved = await settings.save(
            {
                SETTING_POLL_INTERVAL_MS: "5000",
                SETTING_SOURCE_CONCURRENCY: "6",
                SETTING_TIMEZONE: "Asia/Shanghai",
            }
        )

        assert saved == await settings.snapshot()
        assert saved["poll_interval_ms"] == 5000
        assert saved["source_concurrency"] == 6
        assert saved["timezone"] == "Asia/Shanghai"
        assert saved["poll_interval_overridden"] is True

    @pytest.mark.asyncio
    async def test_a_field_the_form_left_out_is_not_touched(
        self, tmp_path: Path
    ) -> None:
        """The 系统 tab submits one form, but a partial request must not clear
        what it does not carry."""
        settings = await service(tmp_path)
        await settings.save({SETTING_TIMEZONE: "Asia/Tokyo"})
        await settings.save({SETTING_SOURCE_CONCURRENCY: "4"})

        snapshot = await settings.snapshot()
        assert snapshot["timezone"] == "Asia/Tokyo"
        assert snapshot["source_concurrency"] == 4

    @pytest.mark.asyncio
    async def test_an_empty_submission_clears_the_override(
        self, tmp_path: Path
    ) -> None:
        """Same contract as the archive path overrides: blank means default."""
        settings = await service(tmp_path)
        await settings.save({SETTING_POLL_INTERVAL_MS: "9000"})
        await settings.save({SETTING_POLL_INTERVAL_MS: "  "})

        snapshot = await settings.snapshot()
        assert snapshot["poll_interval_ms"] == DEFAULT_POLL_INTERVAL_MS
        assert snapshot["poll_interval_overridden"] is False

    @pytest.mark.parametrize(
        ("values", "code"),
        [
            ({SETTING_POLL_INTERVAL_MS: "later"}, "POLL_INTERVAL_INVALID"),
            (
                {SETTING_POLL_INTERVAL_MS: str(MIN_POLL_INTERVAL_MS - 1)},
                "POLL_INTERVAL_INVALID",
            ),
            (
                {SETTING_POLL_INTERVAL_MS: str(MAX_POLL_INTERVAL_MS + 1)},
                "POLL_INTERVAL_INVALID",
            ),
            ({SETTING_SOURCE_CONCURRENCY: "many"}, "CONCURRENCY_INVALID"),
            (
                {SETTING_SOURCE_CONCURRENCY: str(MIN_SOURCE_CONCURRENCY - 1)},
                "CONCURRENCY_INVALID",
            ),
            (
                {SETTING_SOURCE_CONCURRENCY: str(MAX_SOURCE_CONCURRENCY + 1)},
                "CONCURRENCY_INVALID",
            ),
            ({SETTING_TIMEZONE: "not a zone"}, "TIMEZONE_INVALID"),
            ({SETTING_TIMEZONE: "../etc/localtime"}, "TIMEZONE_INVALID"),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_value_that_cannot_be_stored_is_refused_with_its_bound(
        self, tmp_path: Path, values: dict, code: str
    ) -> None:
        settings = await service(tmp_path)

        with pytest.raises(SystemSettingsError) as raised:
            await settings.save(values)

        assert raised.value.code == code
        assert raised.value.public_message

    @pytest.mark.asyncio
    async def test_a_refused_save_stores_nothing_at_all(
        self, tmp_path: Path
    ) -> None:
        """One bad field in a three-field form must not half-apply the save."""
        settings = await service(tmp_path)

        with pytest.raises(SystemSettingsError):
            await settings.save(
                {
                    SETTING_SOURCE_CONCURRENCY: "6",
                    SETTING_TIMEZONE: "not a zone",
                }
            )

        snapshot = await settings.snapshot()
        assert snapshot["source_concurrency"] == 3
        assert snapshot["timezone"] == DEFAULT_TIMEZONE

    @pytest.mark.asyncio
    async def test_a_bare_zone_name_is_accepted(self, tmp_path: Path) -> None:
        """`UTC` has no `Area/Location`, and it is the default."""
        settings = await service(tmp_path)
        assert (await settings.save({SETTING_TIMEZONE: "UTC"}))["timezone"] == (
            "UTC"
        )

    @pytest.mark.asyncio
    async def test_a_three_level_zone_name_is_accepted(
        self, tmp_path: Path
    ) -> None:
        """`America/Argentina/Salta` is a real zone, and the shape check has to
        allow it or an operator there cannot save their own timezone."""
        settings = await service(tmp_path)
        saved = await settings.save(
            {SETTING_TIMEZONE: "America/Argentina/Salta"}
        )

        assert saved["timezone"] == "America/Argentina/Salta"
