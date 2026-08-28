"""The tab and facet plumbing behind the R5 candidate list.

These are the two places where a number or a URL becomes a query, so they are
tested away from the database: a tab badge that disagrees with the list under it
and a facet value that reaches SQL unchecked are both defects the integration
tests would only catch by accident.
"""

from __future__ import annotations

import pytest

from app.api.candidates import (
    CANDIDATE_TABS,
    MAX_FACET_VALUES,
    candidate_facet_selection,
    candidate_tab_counts,
)
from app.api.contracts import ApiError
from app.api.status import CANDIDATE_TAB_STATUS, candidate_tab_view
from app.db.database import CANDIDATE_COUNT_KEYS, CANDIDATE_FACETS


class TestTabs:
    def test_every_tab_has_a_label(self) -> None:
        """The tab bar's words and the tab bar's queries come in pairs.

        A tab present in one table and missing from the other is either a link
        with no text or a heading that selects nothing.
        """
        assert set(CANDIDATE_TABS) == set(CANDIDATE_TAB_STATUS)

    def test_every_tab_status_is_a_countable_status(self) -> None:
        """`candidate_tab_counts` can only add up statuses the count query emits."""
        for statuses in CANDIDATE_TABS.values():
            for status in statuses:
                assert status in CANDIDATE_COUNT_KEYS, status

    def test_counts_add_up_the_statuses_the_tab_selects(self) -> None:
        counts = {
            "total": 9,
            "pending_review": 2,
            "needs_info": 3,
            "needs_revision": 1,
            "approved": 1,
            "processing": 1,
            "downloaded": 0,
            "rejected": 1,
            "failed": 0,
        }
        tallies = candidate_tab_counts(counts)
        # 「待补充」 covers both revision states, 「已通过」 everything already let
        # through: the badge has to match the list, which selects the same sets.
        assert tallies["needs_info"] == 4
        assert tallies["approved"] == 2
        assert tallies["pending"] == 2
        assert tallies["failed"] == 0

    def test_all_uses_the_table_total_rather_than_the_union(self) -> None:
        """A state with no tab still has to be counted somewhere.

        DISCOVERED belongs to no tab, so summing the other five would report a
        smaller 「全部」 than the list it opens.
        """
        counts = {"total": 5, "discovered": 5}
        assert candidate_tab_counts(counts)["all"] == 5

    def test_a_missing_count_reads_as_zero(self) -> None:
        """The dashboard renders before any candidate exists."""
        assert candidate_tab_counts({}) == dict.fromkeys(CANDIDATE_TABS, 0)

    def test_an_unknown_tab_falls_back_instead_of_raising(self) -> None:
        """A hand-edited URL must not take the page down.

        The view layer asks for a label before the route has rejected anything,
        so `candidate_tab_view` answers for every string.
        """
        assert candidate_tab_view("pending").label == "待审核"
        assert candidate_tab_view("nonsense").label
        assert candidate_tab_view(None).label


class TestFacetSelection:
    def test_blank_and_repeated_values_are_dropped(self) -> None:
        selection = candidate_facet_selection(
            {"tags": ["巨乳", " 巨乳 ", "", "  ", "汉语"], "artist": None}
        )
        assert selection == {"tags": ("巨乳", "汉语")}

    def test_unknown_names_are_ignored(self) -> None:
        """Only `CANDIDATE_FACETS` names reach the database layer's whitelist."""
        selection = candidate_facet_selection(
            {"status": ["FAILED"], "language": ["Chinese"]}
        )
        assert set(selection) <= set(CANDIDATE_FACETS)
        assert selection == {"language": ("Chinese",)}

    def test_too_many_values_in_one_group_is_refused(self) -> None:
        with pytest.raises(ApiError) as raised:
            candidate_facet_selection(
                {"tags": [f"tag-{index}" for index in range(MAX_FACET_VALUES + 1)]}
            )
        assert raised.value.code == "FACET_TOO_MANY"
        assert raised.value.details == {
            "facet": "tags",
            "limit": MAX_FACET_VALUES,
        }

    def test_the_cap_counts_distinct_values(self) -> None:
        """Repeats collapse before the cap, so a re-submitted URL still works.

        A checkbox form that posts the same tag twice is not an operator trying
        to build a fifty-clause query.
        """
        repeated = ["巨乳"] * (MAX_FACET_VALUES + 4)
        assert candidate_facet_selection({"tags": repeated}) == {"tags": ("巨乳",)}
