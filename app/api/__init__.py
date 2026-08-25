"""JSON API package.

The API layer is deliberately separate from the page layer: `app/api` returns
data with a stable envelope and never renders a template or issues a redirect,
while `app/web/routes` renders page shells and lets the browser fetch its own
data. Keeping the two apart is what allows the interface to update a single row
instead of reloading the whole document.
"""

from app.api.contracts import (
    ApiError,
    Page,
    PageParams,
    api_error_handler,
)

__all__ = [
    "ApiError",
    "Page",
    "PageParams",
    "api_error_handler",
]