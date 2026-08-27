"""Navigation model tests (R3).

The bugs these guard against are the ones the old two-navigation `base.html`
actually had: a link present in one rendering and missing from the other, and a
parent highlighted as the current page while its child was also current. Both
are invisible in a screenshot and obvious to a screen reader.
"""

from app.web.routes import NAV_ITEMS, NavItem, active_domain
from app.web.routes.shell import DENSITIES, THEMES


def _walk(items: tuple[NavItem, ...]):
    for item in items:
        yield item
        yield from _walk(item.children)


def test_dashboard_prefix_does_not_swallow_every_path() -> None:
    # `/` as a prefix would `startswith`-match every URL in the application, so
    # it is special-cased. If that case is ever dropped, the dashboard is the
    # current page on all 14 pages at once.
    dashboard = NAV_ITEMS[0]
    assert dashboard.matches("/")
    assert not dashboard.matches("/candidates")
    assert not dashboard.matches("/activity/history")


def test_prefix_matches_on_segment_boundaries_only() -> None:
    candidates = NAV_ITEMS[1]
    assert candidates.matches("/candidates")
    assert candidates.matches("/candidates/42")
    # `/candidates-archive` shares a string prefix but is a different page.
    assert not candidates.matches("/candidates-archive")


def test_parent_is_active_but_only_the_child_is_current() -> None:
    activity = NAV_ITEMS[2]
    history = "/activity/history"
    # The parent is highlighted...
    assert activity.is_active(history)
    # ...but does not claim `aria-current`. `/activity` is a prefix of
    # `/activity/history`, so the parent does `matches()` it -- which is why
    # the template must ask `is_current()` instead. Two current pages in one
    # document is a defect a screen reader reads out loud.
    assert activity.matches(history)
    assert not activity.is_current(history)
    child = next(c for c in activity.children if c.key == "history")
    assert child.is_current(history)


def test_exactly_one_item_in_the_whole_tree_is_ever_current() -> None:
    # The same prefix trap exists between siblings: `/candidates` is a prefix of
    # `/candidates/needs-info`, so without `exact=True` on the index child both
    # 全部候选 and 待补充 would announce themselves as the current page.
    for path in (
        "/",
        "/candidates",
        "/candidates/needs-info",
        "/candidates/processing",
        "/candidates/failed",
        "/manual-add",
        "/activity",
        "/activity/packing",
        "/activity/history",
        "/connections",
        "/sources",
        "/auto-approval-rules",
        "/archive-settings",
        "/change-password",
    ):
        current = [i.key for i in _walk(NAV_ITEMS) if i.is_current(path)]
        assert len(current) == 1, f"{path} produced {current}"


def test_a_detail_page_highlights_its_section_and_no_leaf() -> None:
    # `/candidates/42` is not in the tree. The section must still read as
    # active, and no child may claim to be the page being shown.
    detail = "/candidates/42"
    candidates = NAV_ITEMS[1]
    assert candidates.is_active(detail)
    assert not any(child.is_current(detail) for child in candidates.children)


def test_every_live_page_resolves_to_exactly_one_domain() -> None:
    # A page reachable in the application but absent from the tree renders with
    # no navigation highlighted, which is how the old mobile bar drifted.
    for path in (
        "/",
        "/candidates",
        "/candidates/needs-info",
        "/candidates/processing",
        "/candidates/failed",
        "/manual-add",
        "/activity",
        "/activity/packing",
        "/activity/history",
        "/connections",
        "/sources",
        "/auto-approval-rules",
        "/archive-settings",
        "/change-password",
    ):
        matched = [item for item in NAV_ITEMS if item.is_active(path)]
        assert len(matched) == 1, f"{path} matched {[i.key for i in matched]}"


def test_active_domain_returns_none_off_the_tree() -> None:
    # `/login` renders before a session exists and deliberately has no nav.
    assert active_domain("/login") is None


def test_nav_keys_are_unique() -> None:
    keys = [item.key for item in _walk(NAV_ITEMS)]
    assert len(keys) == len(set(keys))


def test_nav_items_are_immutable() -> None:
    # Frozen dataclasses because the tree is a module-level constant shared by
    # every request; a mutable one would let a handler leak state into the next.
    try:
        NAV_ITEMS[0].label = "changed"  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("NavItem should be frozen")


def test_theme_and_density_vocabularies_are_closed() -> None:
    # `ui.js` validates against these before writing localStorage, and the
    # stylesheet has a selector for each. Adding a value in one place only is
    # how an unstyled theme ships.
    assert THEMES == ("auto", "light", "dark")
    assert DENSITIES == ("comfortable", "compact")
