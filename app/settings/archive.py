"""Shared archive submission fields for every client.

Validation of server paths belongs to the backend regardless of the client.
Preference commands reuse the archive service's validation before atomic CAS.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.archive.service import LIMIT_KEYS
from app.settings.forms import checkbox

#: The qBittorrent fields the form carries. Named here so the page and the API
#: cannot disagree about which ones are writable -- an omitted key means 「leave
#: it alone」to `save_torrent_client`, which is why this is a list rather than a
#: comprehension over whatever arrived.
TORRENT_CLIENT_FIELDS: tuple[str, ...] = (
    "base_url",
    "username",
    "password",
    "category",
    "save_path",
    "local_save_path",
)


def torrent_submission(values: Mapping[str, Any]) -> dict[str, Any]:
    """The qBittorrent client settings.

    Missing JSON fields retain their stored values. Plain forms explicitly add
    false for unchecked switches before calling the same command.
    """
    payload: dict[str, Any] = {
        key: str(values[key] or '') for key in TORRENT_CLIENT_FIELDS if key in values
    }
    for key in ("keep_seeding", "auto_pack"):
        if key in values:
            payload[key] = checkbox(values[key])
    return payload


def limits_submission(values: Mapping[str, Any]) -> dict[str, str]:
    """The safety limits, as the strings `save_limits` validates.

    Typed by hand on the page and the field that realistically fails validation,
    so they are passed through untouched: a rejected number has to reach the
    service, which owns the message that names the bound.
    """
    return {key: str(values.get(key) or "") for key in LIMIT_KEYS}


__all__ = [
    "TORRENT_CLIENT_FIELDS",
    "limits_submission",
    "torrent_submission",
]
