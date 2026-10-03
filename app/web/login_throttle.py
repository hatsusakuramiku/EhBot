"""The failed-login throttle, shared by the page and the JSON login.

Extracted from `app/web/routes/auth.py` when the mobile client grew its own
`POST /api/v1/auth/login`. Both entries take the same administrator password, so
they must share one counter: a client cannot get a fresh budget by switching
endpoints, and the two 429s mean the same thing.

The counter is per-application state (`app.state.login_attempts`), not a module
global, so two applications in one test session cannot share a lockout.

Two properties are worth stating, because both were wrong:

**It is bounded.** Entries used to be removed only on a successful login, or
when the same address came back after its lock expired -- so an address that
failed once and never returned stayed in the dict for the life of the process.
Attempts from many addresses therefore grew it without limit. Expired entries
are now pruned on write, and the dict has a hard ceiling.

**The key is the client address**, which behind a reverse proxy is only the real
client when `TRUST_PROXY_HEADERS` is set. Untrusted, a proxied deployment
collapses every caller onto one bucket. That is deliberately not worked around:
honouring an unverified header is how an attacker bypasses the throttle
entirely, and for a single-administrator service the safe failure is one shared
bucket rather than a forgeable one.
"""

from __future__ import annotations

#: Consecutive failures before the address is locked out.
MAX_FAILED_ATTEMPTS = 5

#: How long that lockout lasts.
LOCKOUT_SECONDS = 60.0

#: Hard ceiling on tracked addresses. Reaching it means the pruning below could
#: not keep up -- a spray from thousands of addresses -- and the oldest entries
#: are dropped. Losing a counter is the right failure: it costs an attacker
#: nothing they did not already have, while an unbounded dict costs the process
#: memory it cannot reclaim.
MAX_TRACKED_CLIENTS = 1024


def _prune_expired(attempts: dict[str, tuple[int, float]], now: float) -> None:
    """Drop entries whose lock has run out, then bound what is left.

    Called before every write. An entry with `locked_until == 0` is a partial
    failure count with no expiry of its own, so it is only shed by the ceiling.
    """
    for key in [
        key
        for key, (_, locked_until) in attempts.items()
        if locked_until and locked_until <= now
    ]:
        attempts.pop(key, None)
    while len(attempts) > MAX_TRACKED_CLIENTS:
        attempts.pop(next(iter(attempts)))


def is_locked(attempts: dict[str, tuple[int, float]], key: str, now: float) -> bool:
    """Whether this address is currently serving a lockout."""
    return attempts.get(key, (0, 0.0))[1] > now


def failed_count(
    attempts: dict[str, tuple[int, float]], key: str, now: float
) -> int:
    """The current run of failures, clearing a lock whose time has passed."""
    count, locked_until = attempts.get(key, (0, 0.0))
    if locked_until and locked_until <= now:
        attempts.pop(key, None)
        return 0
    return count


def record_failure(
    attempts: dict[str, tuple[int, float]],
    key: str,
    now: float,
    count: int,
) -> bool:
    """Count one failure; return whether it just tripped the lock."""
    count += 1
    locked = count >= MAX_FAILED_ATTEMPTS
    _prune_expired(attempts, now)
    attempts[key] = (count, now + LOCKOUT_SECONDS if locked else 0.0)
    return locked


def clear(attempts: dict[str, tuple[int, float]], key: str) -> None:
    attempts.pop(key, None)


__all__ = [
    "LOCKOUT_SECONDS",
    "MAX_FAILED_ATTEMPTS",
    "MAX_TRACKED_CLIENTS",
    "clear",
    "failed_count",
    "is_locked",
    "record_failure",
]
