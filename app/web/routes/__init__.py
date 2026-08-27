"""Page-shell routes.

A module here renders a template and nothing else: no domain orchestration, no
enqueueing, no provider routing. The page fetches its own data from
`/api/v1/*`, which is what lets a row update without reloading the document.

R0 provides the package and the shared shell context; R3 adds the navigation
tree it renders from. The existing pages stay in `app/main.py` until each is
moved across in R4–R8, so this package grows one domain at a time instead of in
a single risky commit.
"""

from app.web.routes.shell import (
    DENSITIES,
    NAV_ITEMS,
    THEMES,
    NavItem,
    active_domain,
    shell_context,
)
from app.web.routes.ui_kit import ui_kit_context

__all__ = [
    "DENSITIES",
    "NAV_ITEMS",
    "THEMES",
    "NavItem",
    "active_domain",
    "shell_context",
    "ui_kit_context",
]