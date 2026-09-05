"""Context every page shell needs.

The navigation is defined once here, as data. `base.html` used to hardcode the
same links twice -- once for the sidebar and once for the mobile header -- which
is why the two drifted apart: the mobile bar had a 历史 entry the sidebar never
gained. One list rendered by both means adding a section is one edit, and the
mobile view can never disagree with the desktop one.

Why the tree has two levels
---------------------------
The target information architecture is four flat domains, but three of them own
several pages an operator switches between constantly -- the six candidate tabs,
the three activity queues, the seven settings sections -- and a flat nav would
put those behind a page they had to load first. So a domain carries its sections
as ``children``, the sidebar renders both levels, and only a leaf ever claims
``aria-current="page"``.

R8 collapsed the settings domain into `/settings/{section}`: 来源规则, 自动审批,
归档设置 and 外部连接 used to be four top-level pages listed here by hand, each
with its own URL. They are now tabs named from `settings_section_view`, so this
file no longer contains a settings label at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import Request

from app.api.status import (
    SETTINGS_ARCHIVE,
    SETTINGS_AUTO_APPROVAL,
    SETTINGS_CONNECTIONS,
    SETTINGS_PASSWORDS,
    SETTINGS_PATHS,
    SETTINGS_SOURCES,
    SETTINGS_SYSTEM,
    candidate_tab_view,
    downloaded_tab_view,
    settings_section_view,
)

#: The zone timestamps are rendered in when nothing has been stored yet. Kept
#: here as a literal rather than imported from `app.settings.service` so the
#: shell has no dependency on the database layer: the real value arrives on
#: `app.state`, and this is only what a page shows before the store is read.
FALLBACK_TIMEZONE = "UTC"

#: Themes the shell will apply. `auto` follows the operating system.
THEMES: tuple[str, ...] = ("auto", "light", "dark")

#: Row density. `comfortable` is the default; `compact` is for scanning a long
#: queue. Only shared measurements change, so no component has its own variant.
DENSITIES: tuple[str, ...] = ("comfortable", "compact")


@dataclass(frozen=True, slots=True)
class NavItem:
    """One destination in the navigation tree.

    ``prefix`` is what marks the item active, so a detail page under a section
    keeps its parent highlighted. ``short`` is the compact label for the mobile
    bar, where the full name does not fit.
    """

    key: str
    label: str
    short: str
    path: str
    prefix: str
    #: A single glyph for the collapsed rail and the mobile tab bar. It is
    #: decorative -- the label stays in the DOM and is only visually hidden --
    #: so it is never the sole cue for a destination.
    icon: str = "•"
    #: Match the path exactly instead of by prefix. Needed by the "index" child
    #: of a section, whose path is its parent's: `/candidates` is a prefix of
    #: `/candidates/needs-info`, so a prefix test would make 全部候选 and 待补充
    #: both the current page. The index child is the narrower claim, so it gives
    #: up prefix matching and the parent keeps it.
    exact: bool = False
    children: tuple[NavItem, ...] = field(default=())

    def matches(self, current_path: str) -> bool:
        """Whether this item's own route is the current one.

        The dashboard is special-cased: its prefix is ``/``, which every path
        starts with, so a prefix test would light it up on every page.
        """
        if self.prefix == "/":
            return current_path == "/"
        if self.exact:
            return current_path == self.prefix
        return current_path == self.prefix or current_path.startswith(
            self.prefix + "/"
        )

    def is_active(self, current_path: str) -> bool:
        """Whether this item or any of its children is the current route.

        A parent whose child is open must read as active, otherwise the mobile
        bar shows nothing selected while the operator is plainly inside a
        section.
        """
        if self.matches(current_path):
            return True
        return any(child.matches(current_path) for child in self.children)

    def is_current(self, current_path: str) -> bool:
        """Whether this item is the page the document is showing.

        Only this earns ``aria-current="page"``; ``is_active`` earns a class.
        A parent never claims it, because its prefix is by construction a
        prefix of its children's paths -- ``/activity`` also `matches()`
        ``/activity/history``, and 活动 plus 历史 both announcing themselves as
        the current page is a defect a screen reader reads out and a screenshot
        hides. The child is the more specific answer, so the child wins.
        """
        return not self.children and self.matches(current_path)


#: The six domains, each carrying the pages it absorbed. 已下载 sits between
#: 活动 and 设置: it was cut on 2026-08-26 as「书库纳管」and reinstated on
#: 2026-08-28 by operator instruction, scoped to what §1.3.1 of the requirements
#: document actually asks for -- listing what has been downloaded and acting on
#: it (pack, re-pack, rename, relocate, remove, re-download). It is deliberately
#: *not* a reader or a catalogue: no shelves, no reading progress, no import
#: scan. Those remain out of scope.
NAV_ITEMS: tuple[NavItem, ...] = (
    NavItem("dashboard", "工作台", "工作台", "/", "/", icon="◉"),
    NavItem(
        "candidates",
        "候选",
        "候选",
        "/candidates",
        "/candidates",
        icon="▤",
        children=(
            #: The six tabs of the candidate domain, named by
            #: `candidate_tab_view` rather than by a string typed here: a tab
            #: called 待审核 in the sidebar, 待审核 in the tab strip and
            #: `pending` in JSON is one vocabulary in `app/api/status.py`, and a
            #: literal here would be the second copy that drifts.
            #:
            #: 待审核 is the index child because `/candidates` renders it -- the
            #: domain's front door is the queue an operator opens it to work --
            #: so it is the one that gives up prefix matching.
            NavItem(
                "candidates_pending",
                candidate_tab_view("pending").label,
                candidate_tab_view("pending").label,
                "/candidates",
                "/candidates",
                exact=True,
            ),
            NavItem(
                "candidates_all",
                candidate_tab_view("all").label,
                candidate_tab_view("all").label,
                "/candidates/all",
                "/candidates/all",
            ),
            NavItem(
                "candidates_needs_info",
                candidate_tab_view("needs_info").label,
                candidate_tab_view("needs_info").label,
                "/candidates/needs-info",
                "/candidates/needs-info",
            ),
            NavItem(
                "candidates_approved",
                candidate_tab_view("approved").label,
                candidate_tab_view("approved").label,
                "/candidates/approved",
                "/candidates/approved",
            ),
            NavItem(
                "candidates_rejected",
                candidate_tab_view("rejected").label,
                candidate_tab_view("rejected").label,
                "/candidates/rejected",
                "/candidates/rejected",
            ),
            NavItem(
                "candidates_failed",
                candidate_tab_view("failed").label,
                candidate_tab_view("failed").label,
                "/candidates/failed",
                "/candidates/failed",
            ),
            NavItem(
                "manual_add", "手动添加", "手动", "/manual-add", "/manual-add",
            ),
        ),
    ),
    NavItem(
        "activity",
        "活动",
        "活动",
        "/activity",
        "/activity",
        icon="⇄",
        children=(
            #: 队列 and 打包 are two tabs rather than one list because they are
            #: two queues: a packaging job carries `provider='CONVERSION'` and
            #: never competes for a download slot, and mixing them was what made
            #: the old page's counts unreadable.
            NavItem(
                "queue", "队列", "队列", "/activity", "/activity",
                exact=True,
            ),
            NavItem(
                "packing", "打包", "打包",
                "/activity/packing", "/activity/packing",
            ),
            NavItem(
                "history", "历史", "历史",
                "/activity/history", "/activity/history",
            ),
        ),
    ),
    NavItem(
        "downloaded",
        "已下载",
        "已下载",
        "/downloaded",
        "/downloaded",
        icon="▣",
        children=(
            #: The five tabs, named by `downloaded_tab_view` rather than by a
            #: string typed here, for the reason the candidate tabs are: the
            #: sidebar entry, the tab strip and the JSON payload must be one
            #: vocabulary.
            #:
            #: 全部 is the index child because `/downloaded` renders it, so it is
            #: the one that gives up prefix matching -- otherwise it would
            #: prefix-match every sibling.
            NavItem(
                "downloaded_all",
                downloaded_tab_view("all").label,
                downloaded_tab_view("all").label,
                "/downloaded",
                "/downloaded",
                exact=True,
            ),
            NavItem(
                "downloaded_unpacked",
                downloaded_tab_view("unpacked").label,
                downloaded_tab_view("unpacked").label,
                "/downloaded/unpacked",
                "/downloaded/unpacked",
            ),
            NavItem(
                "downloaded_packed",
                downloaded_tab_view("packed").label,
                downloaded_tab_view("packed").label,
                "/downloaded/packed",
                "/downloaded/packed",
            ),
            NavItem(
                "downloaded_attention",
                downloaded_tab_view("attention").label,
                downloaded_tab_view("attention").label,
                "/downloaded/attention",
                "/downloaded/attention",
            ),
            NavItem(
                "downloaded_failed",
                downloaded_tab_view("failed").label,
                downloaded_tab_view("failed").label,
                "/downloaded/failed",
                "/downloaded/failed",
            ),
        ),
    ),
    #: 运行日志 is a leaf: it has no sections, because a level floor is a filter
    #: on one view and not a second page. It sits before 设置 because it is an
    #: observation surface and 设置 is a configuration one -- an operator watching
    #: a job fail is not in the middle of changing a setting.
    NavItem("logs", "运行日志", "日志", "/logs", "/logs", icon="≡"),
    NavItem(
        "settings",
        "设置",
        "设置",
        "/settings",
        "/settings",
        icon="⚙",
        children=(
            #: The seven tabs of the settings domain, named by
            #: `settings_section_view` rather than by a string typed here, for
            #: the same reason the candidate tabs are: the tab, the URL segment
            #: and the JSON payload must be one vocabulary. 外部连接 is the index
            #: child because `/settings` renders it -- a deployment is not usable
            #: until Telegram is connected, so it is the first thing an operator
            #: needs -- and so it is the one that gives up prefix matching.
            NavItem(
                "connections",
                settings_section_view(SETTINGS_CONNECTIONS).label,
                "连接",
                f"/settings/{SETTINGS_CONNECTIONS}",
                f"/settings/{SETTINGS_CONNECTIONS}",
            ),
            NavItem(
                "sources",
                settings_section_view(SETTINGS_SOURCES).label,
                "来源",
                f"/settings/{SETTINGS_SOURCES}",
                f"/settings/{SETTINGS_SOURCES}",
            ),
            NavItem(
                "auto_approval",
                settings_section_view(SETTINGS_AUTO_APPROVAL).label,
                "审批",
                f"/settings/{SETTINGS_AUTO_APPROVAL}",
                f"/settings/{SETTINGS_AUTO_APPROVAL}",
            ),
            NavItem(
                "archive",
                settings_section_view(SETTINGS_ARCHIVE).label,
                "归档",
                f"/settings/{SETTINGS_ARCHIVE}",
                f"/settings/{SETTINGS_ARCHIVE}",
            ),
            NavItem(
                "paths",
                settings_section_view(SETTINGS_PATHS).label,
                "路径",
                f"/settings/{SETTINGS_PATHS}",
                f"/settings/{SETTINGS_PATHS}",
            ),
            NavItem(
                "passwords",
                settings_section_view(SETTINGS_PASSWORDS).label,
                "密码",
                f"/settings/{SETTINGS_PASSWORDS}",
                f"/settings/{SETTINGS_PASSWORDS}",
            ),
            NavItem(
                "system",
                settings_section_view(SETTINGS_SYSTEM).label,
                "系统",
                f"/settings/{SETTINGS_SYSTEM}",
                f"/settings/{SETTINGS_SYSTEM}",
            ),
        ),
    ),
)


def active_domain(current_path: str) -> NavItem | None:
    """The top-level item the current path belongs to, if any."""
    for item in NAV_ITEMS:
        if item.is_active(current_path):
            return item
    return None


def shell_context(request: Request) -> dict:
    """Build the template context shared by every page.

    The CSRF token is included because HTMX reads it from a meta tag and
    attaches it to every state-changing request, which removes the per-form
    hidden input the old templates each had to remember.

    The timezone is read off `app.state` rather than from the database, because
    this runs for every rendered page and is synchronous. The route that saves it
    refreshes the cached value, so a change applies on the next page without a
    restart -- and a deployment that never opened the settings page renders the
    fallback rather than paying for a query per page.
    """
    current_path = request.url.path
    domain = active_domain(current_path)
    return {
        "nav_items": NAV_ITEMS,
        "current_path": current_path,
        "active_domain": domain,
        "active_children": domain.children if domain is not None else (),
        "csrf_token": request.session.get("csrf_token", ""),
        "display_timezone": getattr(
            request.app.state, "display_timezone", FALLBACK_TIMEZONE
        ),
    }


__all__ = [
    "DENSITIES",
    "FALLBACK_TIMEZONE",
    "NAV_ITEMS",
    "THEMES",
    "NavItem",
    "active_domain",
    "shell_context",
]
