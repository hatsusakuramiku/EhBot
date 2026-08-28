"""Context every page shell needs.

The navigation is defined once here, as data. `base.html` used to hardcode the
same links twice -- once for the sidebar and once for the mobile header -- which
is why the two drifted apart: the mobile bar had a 历史 entry the sidebar never
gained. One list rendered by both means adding a section is one edit, and the
mobile view can never disagree with the desktop one.

Why the tree has two levels
---------------------------
The target information architecture is four flat domains. But 设置 does not
exist yet -- it arrives in R8 -- and 来源规则 / 自动审批 / 归档设置 / 外部连接
are live pages an operator uses today. A flat four-item nav would make them
unreachable, trading a real regression for a cosmetic match with the plan. So a
domain carries the pages it will eventually absorb as ``children``, and when R8
lands `/settings` those children become tabs inside it and this file shrinks by
one edit. The nav never points at a route that does not exist.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import Request

from app.api.status import candidate_tab_view

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


#: The four domains from the refactor spec, each carrying the legacy pages it
#: will absorb. The 书库 domain that once sat between 活动 and 设置 was deleted
#: on 2026-08-26: this project's scope ends at the archive.
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
        "settings",
        "设置",
        "设置",
        "/connections",
        "/connections",
        icon="⚙",
        children=(
            NavItem(
                "connections", "外部连接", "连接", "/connections",
                "/connections", exact=True,
            ),
            NavItem("sources", "来源规则", "来源", "/sources", "/sources"),
            NavItem(
                "auto_approval", "自动审批", "审批",
                "/auto-approval-rules", "/auto-approval-rules",
            ),
            NavItem(
                "archive", "归档设置", "归档",
                "/archive-settings", "/archive-settings",
            ),
            NavItem(
                "change_password", "修改密码", "密码",
                "/change-password", "/change-password",
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
    """
    current_path = request.url.path
    domain = active_domain(current_path)
    return {
        "nav_items": NAV_ITEMS,
        "current_path": current_path,
        "active_domain": domain,
        "active_children": domain.children if domain is not None else (),
        "csrf_token": request.session.get("csrf_token", ""),
    }


__all__ = [
    "DENSITIES",
    "NAV_ITEMS",
    "THEMES",
    "NavItem",
    "active_domain",
    "shell_context",
]
