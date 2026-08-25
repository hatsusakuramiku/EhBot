"""Context every page shell needs.

The navigation is defined once here, as data. `base.html` currently hardcodes
the same nine links twice -- once for the sidebar and once for the mobile
header -- which is why the two drifted apart. A single list rendered by both
means adding a section is one edit, and the mobile view can never disagree with
the desktop one.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request


@dataclass(frozen=True, slots=True)
class NavItem:
    """One top-level destination.

    ``prefix`` is what marks the item active, so a detail page under a section
    keeps its parent highlighted. ``short`` is the compact label for the
    mobile bar, where the full name does not fit.
    """

    key: str
    label: str
    short: str
    path: str
    prefix: str

    def is_active(self, current_path: str) -> bool:
        if self.prefix == "/":
            return current_path == "/"
        return current_path == self.prefix or current_path.startswith(
            self.prefix + "/"
        )


#: The five domains from the refactor spec. The legacy pages remain reachable
#: at their existing URLs during the transition; this list is what the new
#: shell renders, and each entry is pointed at its rebuilt page as that page
#: lands.
NAV_ITEMS: tuple[NavItem, ...] = (
    NavItem("dashboard", "工作台", "工作台", "/", "/"),
    NavItem("candidates", "候选", "候选", "/candidates", "/candidates"),
    NavItem("activity", "活动", "活动", "/downloads", "/downloads"),
    NavItem("library", "书库", "书库", "/library", "/library"),
    NavItem("settings", "设置", "设置", "/settings", "/settings"),
)


def shell_context(request: Request) -> dict:
    """Build the template context shared by every page.

    The CSRF token is included because HTMX reads it from a meta tag and
    attaches it to every state-changing request, which removes the per-form
    hidden input the old templates each had to remember.
    """
    current_path = request.url.path
    return {
        "nav_items": NAV_ITEMS,
        "current_path": current_path,
        "csrf_token": request.session.get("csrf_token", ""),
    }


__all__ = ["NAV_ITEMS", "NavItem", "shell_context"]