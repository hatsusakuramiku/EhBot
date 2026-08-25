"""Page-shell routes.

A module here renders a template and nothing else: no domain orchestration, no
enqueueing, no provider routing. The page fetches its own data from
`/api/v1/*`, which is what lets a row update without reloading the document.

R0 provides the package and the shared shell context. The existing pages stay
in `app/main.py` until each is moved across in R4–R8, so this package grows one
domain at a time instead of in a single risky commit.
"""

from app.web.routes.shell import shell_context

__all__ = ["shell_context"]