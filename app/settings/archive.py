"""Reading the 归档与工具 tab's writes, once, for the page and the JSON API.

Three of that tab's forms are things a phone can sensibly change: the qBittorrent
client, the safety limits, and whether a finished download packs itself. The
tool profiles and the toolchain install stay on the page -- they name executables
and paths on the *server*, and a value typed on a phone cannot be checked against
a filesystem the operator is not looking at.

What is shared is only how a submission is read. Every refusal is the archive
settings service's, so a limit the page would refuse is refused through the API
by the same code.
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

    The two switches are read with `checkbox` so an unchecked box (absent field)
    means off in both worlds rather than 「whatever the other caller meant」.
    """
    payload: dict[str, Any] = {
        key: values.get(key) for key in TORRENT_CLIENT_FIELDS
    }
    payload["keep_seeding"] = checkbox(values.get("keep_seeding"))
    payload["auto_pack"] = checkbox(values.get("auto_pack"))
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
