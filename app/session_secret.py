"""Resolve the session signing key, generating a durable one when absent.

The key only has to be *stable*, not operator-chosen: it signs session cookies
and nothing else. Requiring it to be created by hand before the first start
bought no security — the value is random either way — while making a missing
file an obstacle between an operator and a running service.

It therefore lives beside the archive password vault key in `<data>/private`,
which is already a persisted, permission-restricted directory, and is generated
on first start exactly like the bootstrap admin password.

An explicitly configured `APP_SECRET_KEY` / `APP_SECRET_KEY_FILE` still wins, so
a deployment that runs several replicas behind a load balancer can keep sharing
one key from its own secret manager.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import secrets

from app.private_files import write_private_text


SESSION_SECRET_NAME = "session_secret_key"

#: Matches the readiness floor for an operator-supplied key.
MINIMUM_LENGTH = 32


@dataclass(frozen=True, slots=True)
class SessionSecret:
    """The key in use, plus why it could not be persisted if it could not be."""

    key: str
    source: str
    error: str | None = None

    @property
    def is_ephemeral(self) -> bool:
        return self.source == "ephemeral"


def resolve_session_secret(
    data_path: Path, configured: str | None = None
) -> SessionSecret:
    """Return the configured key, the stored key, or a freshly stored one.

    A stored key that is too short is replaced rather than trusted: it can only
    come from a truncated write, and silently signing sessions with it would
    hide the damage.

    When the private directory cannot be written the process still starts with
    an in-memory key, but reports the failure through readiness. Refusing to
    start would take down a service whose only real problem is that sessions
    will not survive a restart; staying silent would strand operators being
    logged out at random with nothing to point at.
    """
    if configured:
        return SessionSecret(key=configured, source="configured")
    path = data_path / "private" / SESSION_SECRET_NAME
    try:
        if path.is_file():
            existing = path.read_text(encoding="utf-8").strip()
            if len(existing) >= MINIMUM_LENGTH:
                return SessionSecret(key=existing, source="stored")
        generated = secrets.token_urlsafe(48)
        write_private_text(path, generated)
        return SessionSecret(key=generated, source="generated")
    except OSError as exc:
        return SessionSecret(
            key=secrets.token_urlsafe(48),
            source="ephemeral",
            error=(
                f"session secret could not be persisted at {path}: {exc}; "
                "sessions will not survive a restart"
            ),
        )


__all__ = [
    "MINIMUM_LENGTH",
    "SESSION_SECRET_NAME",
    "SessionSecret",
    "resolve_session_secret",
]
