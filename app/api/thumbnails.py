"""Thumbnail proxy endpoint.

The URL carries only a content hash, so the response can be cached forever:
the hash is a digest of the source identity, and a different source produces a
different URL. Nothing here accepts a caller-supplied URL — that would make the
endpoint an open proxy — so the row must already exist, written when the
candidate's ``thumb_url`` was persisted.

A cover that cannot be fetched is served as a **placeholder with status 200**,
not a 404. An ``<img>`` whose source 404s renders as a broken-image icon and
there is no way to style around it; a real image with
``X-Thumbnail-State: failed`` lets the grid stay intact while still telling a
client (and an operator reading response headers) what happened.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse

from app.api import deps
from app.api.contracts import ApiError
from app.thumbnails import THUMBNAIL_STATE_READY


router = APIRouter(tags=["thumbnails"])


#: A hash is a SHA-256 hex digest and nothing else. Validating the shape at the
#: edge keeps a path separator or a `..` out of the disk-layout helper, which
#: builds a path from these characters.
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")

#: Ready thumbnails are immutable by construction: the hash covers the source
#: identity, so these bytes can never change under this URL.
_READY_CACHE_CONTROL = "private, max-age=31536000, immutable"

#: A placeholder is cached briefly so a transient upstream failure does not
#: pin a broken cover into the browser cache for a year, but a grid of 50
#: failures still does not re-ask 50 times per scroll.
_PLACEHOLDER_CACHE_CONTROL = "private, max-age=60"

_PLACEHOLDER_PATH = (
    Path(__file__).resolve().parent.parent
    / "web"
    / "static"
    / "thumb-placeholder.svg"
)


def _placeholder_response(state: str, error_code: str | None) -> Response:
    """Serve the fallback cover with the real state in a header."""
    headers = {
        "Cache-Control": _PLACEHOLDER_CACHE_CONTROL,
        "X-Thumbnail-State": state.lower(),
    }
    if error_code:
        headers["X-Thumbnail-Error"] = error_code
    try:
        body = _PLACEHOLDER_PATH.read_bytes()
    except OSError:  # pragma: no cover - asset ships with the package
        return Response(status_code=204, headers=headers)
    return Response(
        content=body,
        media_type="image/svg+xml",
        headers=headers,
    )


@router.get("/thumbnails/{thumbnail_hash}")
async def get_thumbnail(request: Request, thumbnail_hash: str) -> Response:
    """Serve one cached cover thumbnail, fetching it on first request."""
    deps.require_session(request)
    if not _HASH_PATTERN.match(thumbnail_hash):
        raise ApiError(
            "THUMBNAIL_HASH_INVALID",
            "缩略图标识格式不正确",
            status_code=404,
        )

    service = deps.thumbnail_service(request)
    result = await service.get_or_create(thumbnail_hash)

    if result.state != THUMBNAIL_STATE_READY or result.file_path is None:
        return _placeholder_response(result.state, result.error_code)

    # The hash is already a strong validator, so it doubles as the ETag. A
    # conditional request costs one string comparison and saves the body.
    etag = f'"{thumbnail_hash}"'
    if request.headers.get("if-none-match") == etag:
        return Response(
            status_code=304,
            headers={"ETag": etag, "Cache-Control": _READY_CACHE_CONTROL},
        )

    return FileResponse(
        result.file_path,
        media_type=result.content_type,
        headers={
            "ETag": etag,
            "Cache-Control": _READY_CACHE_CONTROL,
            "X-Thumbnail-State": THUMBNAIL_STATE_READY.lower(),
        },
    )


__all__ = ["router"]