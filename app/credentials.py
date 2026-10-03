"""Mobile / API credentials: the wire format, the hash, and the stored record.

Two families share one table and one wire format, `<public_id>.<secret>`:

* ``api_key`` -- the single long-lived key an operator mints in the web UI.
  It never expires; "at most one valid key" is enforced by a partial unique
  index, so re-minting has to revoke the old row first.
* ``access`` / ``refresh`` -- the pair a password login issues. The refresh
  rotates on every use; both carry a ``family_id`` so a whole device can be
  logged out at once, and a password change revokes every family.

A secret is 32 random bytes rendered by ``secrets.token_urlsafe``; only
``sha256(secret)`` is stored. Plaintext exists in the response that minted it
and nowhere else -- there is deliberately no read path that returns it. SHA-256
rather than argon2 is right here because the secret is high-entropy: there is no
dictionary to attack, and this hash runs once per authenticated request.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass


KIND_API_KEY = "api_key"
KIND_ACCESS = "access"
KIND_REFRESH = "refresh"

#: Kinds a bearer header may carry. A refresh token is deliberately excluded:
#: it is only accepted at `POST /api/v1/auth/refresh`.
BEARER_KINDS = (KIND_API_KEY, KIND_ACCESS)

#: The password-derived family, revoked together when the password changes.
PASSWORD_KINDS = (KIND_ACCESS, KIND_REFRESH)

API_KEY_PREFIX = "ehk_"
ACCESS_PREFIX = "eha_"
REFRESH_PREFIX = "ehr_"

_PREFIX_BY_KIND = {
    KIND_API_KEY: API_KEY_PREFIX,
    KIND_ACCESS: ACCESS_PREFIX,
    KIND_REFRESH: REFRESH_PREFIX,
}

#: Public ids are short and random; the secret is long. Validated by shape at
#: the edge so a path-unfriendly or truncated value never reaches the database
#: lookup -- the same reason thumbnail hashes are pattern-checked.
_TOKEN_PATTERN = re.compile(
    r"^(?:ehk_|eha_|ehr_)[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{16,}$"
)

SECONDS_PER_HOUR = 3600
SECONDS_PER_DAY = 86400

#: Defaults for a password login. Editable under 设置 › 系统.
DEFAULT_ACCESS_TTL_SECONDS = 12 * SECONDS_PER_HOUR
DEFAULT_REFRESH_TTL_SECONDS = 30 * SECONDS_PER_DAY

#: Bounds for the editable TTLs: below a minute is not a session, and above a
#: year is indistinguishable from the API key.
MIN_TOKEN_TTL_SECONDS = 60
MAX_TOKEN_TTL_SECONDS = 365 * SECONDS_PER_DAY


@dataclass(frozen=True, slots=True)
class ApiCredential:
    """One row of `api_credentials`."""

    id: int
    kind: str
    label: str
    public_id: str
    secret_hash: str
    family_id: str = ""
    created_at: str | None = None
    last_used_at: str | None = None
    #: Unix epoch seconds, or None for "never expires" (every API key).
    expires_at: int | None = None
    revoked_at: str | None = None
    rotated_from: int | None = None

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    def is_expired(self, now: int) -> bool:
        return self.expires_at is not None and self.expires_at <= now


def new_token(kind: str) -> tuple[str, str, str]:
    """Mint one credential: ``(full_token, public_id, secret)``.

    The caller stores ``public_id`` and ``hash_secret(secret)``; the full token
    is what it shows the operator or sends the client, and is never persisted.
    """
    if kind not in _PREFIX_BY_KIND:
        raise ValueError(f"unknown credential kind: {kind!r}")
    public_id = f"{_PREFIX_BY_KIND[kind]}{secrets.token_urlsafe(12)}"
    secret = secrets.token_urlsafe(32)
    return f"{public_id}.{secret}", public_id, secret


def parse_token(raw: str | None) -> tuple[str, str] | None:
    """Split `<public_id>.<secret>`, or return None when the shape is wrong."""
    value = (raw or "").strip()
    if not _TOKEN_PATTERN.match(value):
        return None
    public_id, _, secret = value.partition(".")
    return public_id, secret


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def secret_matches(secret: str, stored_hash: str | None) -> bool:
    """Constant-time comparison of a presented secret against the stored hash."""
    if not stored_hash:
        return False
    return hmac.compare_digest(hash_secret(secret), stored_hash)


__all__ = [
    "ACCESS_PREFIX",
    "API_KEY_PREFIX",
    "BEARER_KINDS",
    "DEFAULT_ACCESS_TTL_SECONDS",
    "DEFAULT_REFRESH_TTL_SECONDS",
    "KIND_ACCESS",
    "KIND_API_KEY",
    "KIND_REFRESH",
    "MAX_TOKEN_TTL_SECONDS",
    "MIN_TOKEN_TTL_SECONDS",
    "PASSWORD_KINDS",
    "REFRESH_PREFIX",
    "ApiCredential",
    "hash_secret",
    "new_token",
    "parse_token",
    "secret_matches",
]
