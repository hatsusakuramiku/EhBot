"""Page-shell routes.

A module here renders a template and nothing else: no domain orchestration, no
enqueueing, no provider routing. The page fetches its own data from
`/api/v1/*`, which is what lets a row update without reloading the document.

R0 provided the package and the shared shell context; R3 added the navigation
tree it renders from; R9 moved the last handler out of `app/main.py`, so every
page route now lives in the module named after its domain. Import order in
`app/wiring.py` is load-bearing: Starlette matches in declaration order, so a
router with a typed path parameter has to be included after its literal
siblings.
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