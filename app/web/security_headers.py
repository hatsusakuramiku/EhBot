"""Response headers the browser enforces on our behalf.

None of these were set before, so every protection they describe was left to
the browser's defaults -- which is to say, absent. They are added in one
middleware rather than per route because a header that depends on a handler
remembering it is a header that is missing on the next handler.

What each one is actually for here:

* **`Content-Security-Policy`** bounds where the page may load code and where it
  may send data. The gain is not「阻止 XSS」in the abstract: it is that the
  thumbnail proxy, the vendored HTMX/Alpine builds and the SSE stream are all
  same-origin by design, so a policy of `'self'` costs nothing and turns any
  future injected `<script src=…>` or exfiltrating `fetch` into a blocked
  request. `frame-ancestors 'none'` is the clickjacking half.
* **`X-Content-Type-Options: nosniff`** stops content sniffing. The thumbnail
  proxy serves attacker-influenced bytes; they are re-encoded to WebP and the
  type is declared, and this is what keeps a browser from second-guessing that
  declaration.
* **`Referrer-Policy`** keeps a candidate's URL out of the `Referer` sent to a
  third party. The pages carry gallery ids in their paths.
* **`X-Frame-Options`** duplicates `frame-ancestors` for anything that predates
  CSP level 2. One line, no downside.

**Why the script policy is not stricter.** Alpine evaluates `x-data` and `@click`
expressions with `new Function`, which needs `'unsafe-eval'`, and the pre-paint
theme bootstrap in `base.html` and `login.html` is an inline `<script>`. A nonce
would cover the second but not the first, and a nonce that sits beside
`'unsafe-eval'` buys nothing -- so the policy states the two exceptions plainly
instead of implying a strictness it does not have. Everything else (`default-src`,
`connect-src`, `img-src`, `form-action`, `base-uri`, `object-src`) is locked to
this origin, and that is the part a browser really does enforce.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


#: `style-src` allows inline because the templates carry a handful of
#: presentational `style=` attributes; `img-src` allows `data:` for the inline
#: SVG placeholder the thumbnail proxy falls back to.
CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self' 'unsafe-inline' 'unsafe-eval'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "object-src 'none'",
    )
)

SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach the fixed header set to every response.

    Existing values are not overwritten: a route that deliberately sets one of
    these (none does today) is making a decision this middleware should not
    silently reverse.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response


__all__ = [
    "CONTENT_SECURITY_POLICY",
    "SECURITY_HEADERS",
    "SecurityHeadersMiddleware",
]
