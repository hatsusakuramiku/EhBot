"""The settings domain's vocabulary, tab strip, and two shared badge resolvers.

Seven sections have to agree in four places: the URL segment, the tab label, the
sidebar leaf and the JSON payload. Any two of those drifting apart shows up as a
tab that navigates nowhere or a nav item whose label differs from the heading it
lands on, so the agreement is asserted here rather than left to a page render.
"""

from __future__ import annotations

import pytest

from app.api.settings import section_tabs
from app.api.status import (
    DEPENDENCY_STATUS,
    SETTINGS_CONNECTIONS,
    SETTINGS_SECTION_STATUS,
    SETTINGS_SECTIONS,
    TOGGLE_STATUS,
    dependency_view,
    settings_section_view,
    toggle_view,
)
from app.web.routes.shell import NAV_ITEMS


def settings_domain():
    """The 设置 item, found by key rather than by index."""
    return next(item for item in NAV_ITEMS if item.key == "settings")


class TestVocabulary:
    def test_there_are_seven_sections_and_each_has_words(self) -> None:
        assert len(SETTINGS_SECTIONS) == 7
        assert set(SETTINGS_SECTIONS) == set(SETTINGS_SECTION_STATUS)
        for code in SETTINGS_SECTIONS:
            assert settings_section_view(code).label

    def test_a_section_code_is_url_safe(self) -> None:
        """The code is the path segment, so it may not need escaping."""
        for code in SETTINGS_SECTIONS:
            assert code == code.lower()
            assert set(code) <= set("abcdefghijklmnopqrstuvwxyz-")

    def test_a_section_is_a_place_rather_than_a_state(self) -> None:
        """A tab strip that coloured one tab would claim a state it has not got."""
        for code in SETTINGS_SECTIONS:
            assert settings_section_view(code).tone == "neutral"
            assert settings_section_view(code).live is False

    def test_an_unknown_section_does_not_fall_back(self) -> None:
        """`/settings/nonsense` has to be a 404, and this is what makes it one.

        A resolver that returned 外部连接 for an unknown code would invent a tab
        and answer a mistyped URL with a page.
        """
        with pytest.raises(KeyError):
            settings_section_view("nonsense")
        with pytest.raises(KeyError):
            settings_section_view(None)


class TestTabStrip:
    def test_the_strip_lists_every_section_in_order(self) -> None:
        tabs = section_tabs(SETTINGS_CONNECTIONS)

        assert [tab["key"] for tab in tabs] == list(SETTINGS_SECTIONS)

    def test_each_tab_links_to_its_own_section(self) -> None:
        for tab in section_tabs(SETTINGS_CONNECTIONS):
            assert tab["href"] == f"/settings/{tab['key']}"
            assert tab["label"] == settings_section_view(tab["key"]).label

    def test_exactly_one_tab_is_current(self) -> None:
        for code in SETTINGS_SECTIONS:
            current = [tab["key"] for tab in section_tabs(code) if tab["current"]]
            assert current == [code]

    def test_no_tab_is_current_off_the_strip(self) -> None:
        """`GET /api/v1/settings` lists the sections without opening one."""
        assert not any(tab["current"] for tab in section_tabs(""))


class TestNavigation:
    def test_the_domain_carries_the_seven_sections_as_children(self) -> None:
        children = settings_domain().children

        assert [child.path for child in children] == [
            f"/settings/{code}" for code in SETTINGS_SECTIONS
        ]

    def test_a_nav_leaf_reads_the_same_words_as_its_tab(self) -> None:
        """Two labels for one destination is the drift this vocabulary exists
        to prevent -- the sidebar and the tab strip must say the same thing."""
        for child in settings_domain().children:
            code = child.path.rsplit("/", 1)[-1]
            assert child.label == settings_section_view(code).label

    def test_every_section_resolves_to_exactly_one_leaf(self) -> None:
        for code in SETTINGS_SECTIONS:
            path = f"/settings/{code}"
            current = [
                child.key
                for child in settings_domain().children
                if child.is_current(path)
            ]
            assert len(current) == 1, f"{path} produced {current}"


class TestSharedBadges:
    @pytest.mark.parametrize("enabled", [True, 1, "yes"])
    def test_anything_truthy_reads_as_enabled(self, enabled: object) -> None:
        """The callers hold an SQLite integer, not a bool."""
        assert toggle_view(enabled).code == "ENABLED"
        assert toggle_view(enabled).label == "已启用"

    @pytest.mark.parametrize("disabled", [False, 0, None, ""])
    def test_anything_falsy_reads_as_disabled(self, disabled: object) -> None:
        assert toggle_view(disabled).code == "DISABLED"
        assert toggle_view(disabled).label == "已停用"

    def test_a_disabled_row_is_a_decision_rather_than_a_fault(self) -> None:
        """`danger` on a switch the operator turned off would read as an error."""
        assert toggle_view(False).tone == "muted"
        assert toggle_view(True).tone == "active"

    def test_a_dependency_is_ready_or_not_ready(self) -> None:
        assert dependency_view(True).label == "已就绪"
        assert dependency_view(False).label == "未就绪"
        assert dependency_view(None).label == "未就绪"

    def test_a_missing_dependency_is_muted_rather_than_dangerous(self) -> None:
        """Neither 7-Zip nor a torrent client is required to run: an install
        with neither still ingests from Telegram."""
        assert dependency_view(False).tone == "muted"
        assert dependency_view(True).tone == "success"

    def test_the_two_vocabularies_stay_separate(self) -> None:
        """「已停用」 and 「未就绪」 are different facts: one is a decision the
        operator made, the other is something to go and fix."""
        assert set(TOGGLE_STATUS) & set(DEPENDENCY_STATUS) == set()
