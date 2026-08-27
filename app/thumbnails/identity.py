"""Content-addressed identity for cached thumbnails.

Separate from ``service`` so a caller that only needs to *name* a thumbnail —
the scrape path recording a cover, a serializer building a URL — does not pull
in httpx and Pillow to do it.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def identity_hash(source_url: str, variant: str) -> str:
    """Deterministic SHA-256 of the source identity (URL + variant).

    This is the cache key and the serving URL path. It is derivable before the
    image has been fetched, so the interface can render a cover URL without
    waiting on a round-trip. The variant is part of the digest rather than a
    query parameter, which is what makes ``immutable`` cache headers honest:
    a different rendering is a different URL, not the same URL with new bytes.

    The NUL separator keeps the two fields from colliding — without it a
    variant/URL pair could be re-split at a different boundary and hash equal
    to a different pair.
    """
    return hashlib.sha256(
        f"{variant}\0{source_url}".encode("utf-8")
    ).hexdigest()


def disk_path(
    thumbnail_dir: Path,
    hash_str: str,
    *,
    mkdir: bool = False,
) -> Path:
    """Two-level fan-out layout: ``<dir>/ab/cd/<hash>.webp``.

    Flat directories with tens of thousands of entries are slow to list on
    every filesystem worth supporting, so the first two byte-pairs of the
    digest become directories.
    """
    if len(hash_str) != 64:
        raise ValueError("thumbnail hash must be a 64-char SHA-256 hex digest")
    part = thumbnail_dir / hash_str[:2] / hash_str[2:4]
    if mkdir:
        part.mkdir(parents=True, exist_ok=True)
    return part / f"{hash_str}.webp"


__all__ = ["disk_path", "identity_hash"]