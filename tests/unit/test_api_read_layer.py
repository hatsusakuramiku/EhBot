"""Unit tests for the R1 read layer: paging, filtering and orchestration."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from app.api.candidates import CANDIDATE_SORTS, CANDIDATE_TABS
from app.db.database import CANDIDATE_COUNT_KEYS, Database, _escape_like
from app.review.orchestration import (
    ReviewOrchestrator,
    RoutedSource,
    TELEGRAM_FILE_LIMIT,
)


def build_database(tmp_path: Path, rows: list[tuple[int, str, str]]) -> Database:
    """Create a database seeded with `(id, status, title)` candidates."""
    database = Database(tmp_path / "test.db")
    asyncio.run(database.initialize())
    with sqlite3.connect(database.path) as connection:
        for candidate_id, status, title in rows:
            connection.execute(
                "INSERT INTO candidates (id, status, filter_result, "
                "filter_reason, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                (candidate_id, status, "ACCEPT", "", "2026-01-01", "2026-01-01"),
            )
            connection.execute(
                "INSERT INTO metadata_values (candidate_id, field_name, "
                "field_value, value_source, confidence, is_manual, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (candidate_id, "Title", title, "EX", 1.0, 0, "2026-01-01"),
            )
    return database


class TestLikeEscaping:
    def test_wildcards_are_neutralised(self) -> None:
        assert _escape_like("100%") == "100\\%"
        assert _escape_like("a_b") == "a\\_b"

    def test_backslash_is_escaped_before_the_wildcards(self) -> None:
        # Escaping the escape character last would double-escape the ones it
        # just inserted and turn a search into a literal backslash hunt.
        assert _escape_like("a\\b") == "a\\\\b"


class TestCandidatePaging:
    def test_total_is_the_unpaged_count(self, tmp_path: Path) -> None:
        database = build_database(
            tmp_path,
            [(i, "PENDING_REVIEW", f"Book {i}") for i in range(1, 8)],
        )
        items, total = asyncio.run(database.list_candidates_page(limit=3))

        assert len(items) == 3
        # The pager needs the full count, not the size of the page it got.
        assert total == 7

    def test_offset_walks_without_repeating_a_row(self, tmp_path: Path) -> None:
        database = build_database(
            tmp_path,
            [(i, "PENDING_REVIEW", f"Book {i}") for i in range(1, 8)],
        )
        first, _ = asyncio.run(database.list_candidates_page(limit=3))
        second, _ = asyncio.run(
            database.list_candidates_page(limit=3, offset=3)
        )

        assert not {item.candidate_id for item in first} & {
            item.candidate_id for item in second
        }

    def test_status_filter_accepts_several_states(self, tmp_path: Path) -> None:
        database = build_database(
            tmp_path,
            [
                (1, "PENDING_REVIEW", "A"),
                (2, "NEEDS_INFO", "B"),
                (3, "REJECTED", "C"),
            ],
        )
        items, total = asyncio.run(
            database.list_candidates_page(
                statuses=("PENDING_REVIEW", "NEEDS_INFO")
            )
        )

        assert total == 2
        assert {item.candidate_id for item in items} == {1, 2}

    def test_no_status_filter_returns_every_state(self, tmp_path: Path) -> None:
        database = build_database(
            tmp_path,
            [(1, "PENDING_REVIEW", "A"), (2, "REJECTED", "B")],
        )
        _, total = asyncio.run(database.list_candidates_page(statuses=()))

        assert total == 2

    def test_unknown_sort_falls_back_instead_of_raising(
        self, tmp_path: Path
    ) -> None:
        database = build_database(
            tmp_path, [(1, "PENDING_REVIEW", "A"), (2, "PENDING_REVIEW", "B")]
        )
        items, _ = asyncio.run(
            database.list_candidates_page(sort="'; DROP TABLE candidates;--")
        )

        # A stale bookmark must still render, and the value must never reach
        # the SQL text.
        assert [item.candidate_id for item in items] == [2, 1]

    def test_title_sort_orders_alphabetically(self, tmp_path: Path) -> None:
        database = build_database(
            tmp_path,
            [(1, "PENDING_REVIEW", "Zebra"), (2, "PENDING_REVIEW", "Apple")],
        )
        items, _ = asyncio.run(database.list_candidates_page(sort="title"))

        assert [item.title for item in items] == ["Apple", "Zebra"]

    def test_search_matches_a_title_substring(self, tmp_path: Path) -> None:
        database = build_database(
            tmp_path,
            [(1, "PENDING_REVIEW", "Summer Book"), (2, "PENDING_REVIEW", "Winter")],
        )
        items, total = asyncio.run(
            database.list_candidates_page(search="Summer")
        )

        assert total == 1
        assert items[0].candidate_id == 1

    def test_search_treats_a_wildcard_as_a_literal(self, tmp_path: Path) -> None:
        database = build_database(
            tmp_path, [(1, "PENDING_REVIEW", "Plain"), (2, "PENDING_REVIEW", "Also")]
        )
        _, total = asyncio.run(database.list_candidates_page(search="%"))

        # An unescaped `%` would match everything and read as a broken filter.
        assert total == 0


class TestCandidateCounts:
    def test_every_status_is_present_even_at_zero(self, tmp_path: Path) -> None:
        database = build_database(tmp_path, [(1, "PENDING_REVIEW", "A")])
        counts = asyncio.run(database.candidate_counts())

        # A badge should read「0」rather than vanish.
        for key in CANDIDATE_COUNT_KEYS.values():
            assert key in counts
        assert counts["pending_review"] == 1
        assert counts["rejected"] == 0

    def test_total_counts_all_rows(self, tmp_path: Path) -> None:
        database = build_database(
            tmp_path,
            [(1, "PENDING_REVIEW", "A"), (2, "REJECTED", "B"), (3, "FAILED", "C")],
        )
        counts = asyncio.run(database.candidate_counts())

        assert counts["total"] == 3


class TestTabVocabulary:
    def test_every_tab_maps_to_known_statuses(self) -> None:
        known = set(CANDIDATE_COUNT_KEYS)
        for statuses in CANDIDATE_TABS.values():
            assert set(statuses) <= known

    def test_all_tab_is_unfiltered(self) -> None:
        # Mapping「全部」to a union would hide any state lacking a tab.
        assert CANDIDATE_TABS["all"] == ()

    def test_sorts_match_the_database_whitelist(self) -> None:
        from app.db.database import _CANDIDATE_SORTS

        # Drift here means the API advertises an ordering the database silently
        # ignores.
        assert CANDIDATE_SORTS == frozenset(_CANDIDATE_SORTS)


class FakeCandidate:
    def __init__(self, messages=(), torrent_hash=None, preview_url=None):
        self.messages = messages
        self.torrent_hash = torrent_hash
        self.preview_url = preview_url


class FakeMessage:
    def __init__(self, attachments):
        self.attachments = attachments


def make_orchestrator(
    *, torrent=True, telegraph=True, telegram_user=False
) -> ReviewOrchestrator:
    return ReviewOrchestrator(
        database=None,
        download_service=lambda: None,
        torrent_available=lambda: torrent,
        telegraph_available=lambda: telegraph,
        telegram_user_available=lambda: telegram_user,
    )


class TestSourceRouting:
    def test_a_fitting_telegram_archive_wins(self) -> None:
        candidate = FakeCandidate(
            messages=(
                FakeMessage([{"type": "archive", "size_bytes": 1024}]),
            ),
            torrent_hash="abc",
        )
        routed = make_orchestrator().route_source(candidate)

        # Original quality and free, so it outranks the torrent.
        assert routed.provider == "TELEGRAM"
        assert routed.attachment is not None

    def test_an_oversized_attachment_falls_through_to_the_torrent(self) -> None:
        candidate = FakeCandidate(
            messages=(
                FakeMessage(
                    [{"type": "archive", "size_bytes": TELEGRAM_FILE_LIMIT + 1}]
                ),
            ),
            torrent_hash="abc",
        )
        routed = make_orchestrator().route_source(candidate)

        # getFile would refuse it permanently, so routing must not pick it.
        assert routed.provider == "EH_TORRENT"

    def test_an_oversized_attachment_prefers_the_user_account_over_the_torrent(
        self,
    ) -> None:
        candidate = FakeCandidate(
            messages=(
                FakeMessage(
                    [{"type": "archive", "size_bytes": TELEGRAM_FILE_LIMIT + 1}]
                ),
            ),
            torrent_hash="abc",
            preview_url="http://x",
        )
        routed = make_orchestrator(telegram_user=True).route_source(candidate)

        # The bytes were always in the channel; only the Bot API protocol could
        # not carry them. Fetching them directly beats a swarm that may have no
        # seeders and a preview page that is a 1280 px re-encode.
        assert routed.provider == "TELEGRAM_USER"
        assert routed.attachment is not None

    def test_the_user_account_is_not_used_when_it_is_not_logged_in(self) -> None:
        candidate = FakeCandidate(
            messages=(
                FakeMessage(
                    [{"type": "archive", "size_bytes": TELEGRAM_FILE_LIMIT + 1}]
                ),
            ),
            torrent_hash="abc",
        )
        routed = make_orchestrator(telegram_user=False).route_source(candidate)

        # Availability is asked per routing decision, so this is the same
        # deployment before the operator logs in.
        assert routed.provider == "EH_TORRENT"

    def test_a_fitting_attachment_still_uses_the_bot(self) -> None:
        candidate = FakeCandidate(
            messages=(FakeMessage([{"type": "archive", "size_bytes": 1024}]),)
        )
        routed = make_orchestrator(telegram_user=True).route_source(candidate)

        # A logged-in user account must not take over the small-file path: the
        # bot needs no extra credential and is already receiving the message.
        assert routed.provider == "TELEGRAM"

    def test_a_file_exactly_at_the_limit_still_uses_telegram(self) -> None:
        candidate = FakeCandidate(
            messages=(
                FakeMessage(
                    [{"type": "archive", "size_bytes": TELEGRAM_FILE_LIMIT}]
                ),
            )
        )
        routed = make_orchestrator().route_source(candidate)

        assert routed.provider == "TELEGRAM"

    def test_the_torrent_is_skipped_when_no_client_is_configured(self) -> None:
        candidate = FakeCandidate(torrent_hash="abc", preview_url="http://x")
        routed = make_orchestrator(torrent=False).route_source(candidate)

        assert routed.provider == "TELEGRAPH"

    def test_nothing_available_is_reported_as_not_downloadable(self) -> None:
        routed = make_orchestrator().route_source(FakeCandidate())

        assert routed.provider is None
        assert not routed.is_downloadable

    def test_the_user_account_needs_an_attachment_to_be_routed(self) -> None:
        # A logged-in account cannot help a candidate that arrived as a bare
        # gallery link: there is no message media to fetch.
        candidate = FakeCandidate(torrent_hash="abc")
        routed = make_orchestrator(telegram_user=True).route_source(candidate)

        assert routed.provider == "EH_TORRENT"

    def test_exhentai_is_never_routed_automatically(self) -> None:
        # Archive Download spends GP, so it stays an explicit operator choice.
        candidates = [
            FakeCandidate(),
            FakeCandidate(torrent_hash="abc"),
            FakeCandidate(preview_url="http://x"),
        ]
        providers = {
            make_orchestrator().route_source(c).provider for c in candidates
        }

        assert "EXHENTAI" not in providers


class TestRoutedSource:
    def test_is_downloadable_reflects_the_provider(self) -> None:
        assert RoutedSource("TELEGRAM").is_downloadable
        assert not RoutedSource(None).is_downloadable
