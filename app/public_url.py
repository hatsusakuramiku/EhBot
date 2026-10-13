"""A configured public origin controls URL generation, never client identity."""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit


def normalize_origin(value: str) -> str:
    """Validate an HTTP origin and normalize host case and default ports."""
    if not value or any(char.isspace() or ord(char) < 32 for char in value):
        raise ValueError("PUBLIC_BASE_URL must be an http(s) origin")
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in {"", "/"} or "?" in value or "#" in value):
        raise ValueError("PUBLIC_BASE_URL must contain only scheme, host and optional port; use APP_ROOT_PATH for a prefix")
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    try:
        address = ipaddress.ip_address(host)
        host = f"[{address.compressed}]" if address.version == 6 else str(address)
    except ValueError:
        if len(host) > 253 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
                                  for part in host.split(".")):
            raise ValueError("PUBLIC_BASE_URL contains an invalid host") from None
    port = parsed.port
    if parsed.netloc.endswith(":") or port == 0:
        raise ValueError("PUBLIC_BASE_URL contains an invalid port")
    default = 443 if parsed.scheme == "https" else 80
    return f"{parsed.scheme}://{host}" + (f":{port}" if port is not None and port != default else "")


class PublicURLMiddleware:
    """Copy scheme and Host from fixed configuration; preserve client and paths."""

    def __init__(self, app, origin: str = ""):
        self.app = app
        self.origin = urlsplit(origin) if origin else None

    async def __call__(self, scope, receive, send):
        if self.origin and scope["type"] in {"http", "websocket"}:
            scope = dict(scope)
            scheme = self.origin.scheme
            scope["scheme"] = {"http": "ws", "https": "wss"}[scheme] if scope["type"] == "websocket" else scheme
            scope["headers"] = [(name, value) for name, value in scope["headers"] if name.lower() != b"host"]
            scope["headers"].append((b"host", self.origin.netloc.encode("ascii")))
            scope["server"] = (self.origin.hostname, self.origin.port or (443 if scheme == "https" else 80))
        async def local_redirect(message):
            if message["type"] == "http.response.start":
                from starlette.requests import Request
                origin = Request(scope).url
                headers = []
                for name, value in message["headers"]:
                    if name.lower() == b"location":
                        target = urlsplit(value.decode("latin-1"))
                        if not target.netloc or (target.scheme == origin.scheme and target.netloc == origin.netloc):
                            path = target.path
                            prefix = scope.get("root_path", "")
                            if prefix and path.startswith("/") and path != prefix and not path.startswith(prefix + "/"):
                                path = prefix + path
                            value = (path + ("?" + target.query if target.query else "") + ("#" + target.fragment if target.fragment else "")).encode("latin-1")
                    headers.append((name, value))
                message = dict(message, headers=headers)
            await send(message)

        await self.app(scope, receive, local_redirect if scope["type"] == "http" else send)
