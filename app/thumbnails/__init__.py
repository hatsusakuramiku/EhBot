"""Thumbnail types, constants and domain errors.

A candidate cover thumbnail is a server-side proxy image: the upstream gdata
``thumb`` URL is fetched, re-encoded as WebP and cached on disk. The hash is
a SHA-256 digest of the source identity (URL + variant), making the serving
URL stable and allowing ``immutable`` cache headers.

Only candidate covers are in scope for R2. Library covers (first page of a
packed CBZ) are deferred until downstream tooling needs them.
"""

from __future__ import annotations

from app.thumbnails.errors import ThumbnailError
from app.thumbnails.identity import disk_path, identity_hash
from app.thumbnails.models import ThumbnailResult, ThumbnailRow


#: Thumbnail kinds. Currently only candidate covers are in scope; library
#: covers are deferred.
THUMBNAIL_KIND_CANDIDATE_COVER = "CANDIDATE_COVER"

#: Variants. R2 implements only the ``card`` variant — a WebP re-encode with
#: longest edge capped at 512 CSS pixels and no upscaling. Additional variants
#: (e.g. ``origin`` for the raw upstream bytes, or size suffixes) can be added
#: when the product needs them.
THUMBNAIL_VARIANT_CARD = "card"

THUMBNAIL_VARIANTS: tuple[str, ...] = (THUMBNAIL_VARIANT_CARD,)

#: Max bytes (4 MB) for an upstream cover. Gdata thumbnails are tiny (~250 px
#: JPEG, well under 100 KB), so this is a safety-net rather than a tuning knob.
MAX_INBOUND_BYTES = 4 * 1024 * 1024

#: Max pixel dimension for any thumbnail. The source is never upscaled, so
#: this is the cap that prevents a decompression-bomb pixel from being loaded.
MAX_PIXEL_EDGE = 4096

#: Max pixel count for the source image. Pillow's own ``Image.MAX_IMAGE_PIXELS``
#: is used as a second line of defence; this is the explicit limit.
MAX_PIXEL_COUNT = 40_000_000

#: Max concurrent outbound fetches. This lives in the service rather than in
#: config because it is a back-pressure tunable for the proxy, not a setting
#: an operator would reasonably change per deployment.
PROXY_CONCURRENCY = 4

#: States a thumbnail row can be in.
THUMBNAIL_STATE_PENDING = "PENDING"
THUMBNAIL_STATE_READY = "READY"
THUMBNAIL_STATE_FAILED = "FAILED"

#: WebP encode quality for the ``card`` variant.
THUMBNAIL_WEBP_QUALITY = 85

#: Longest-edge cap for the ``card`` variant, in CSS pixels.
THUMBNAIL_CARD_MAX_EDGE = 512

__all__ = [
    "MAX_INBOUND_BYTES",
    "MAX_PIXEL_COUNT",
    "MAX_PIXEL_EDGE",
    "PROXY_CONCURRENCY",
    "THUMBNAIL_CARD_MAX_EDGE",
    "THUMBNAIL_KIND_CANDIDATE_COVER",
    "THUMBNAIL_STATE_FAILED",
    "THUMBNAIL_STATE_PENDING",
    "THUMBNAIL_STATE_READY",
    "THUMBNAIL_VARIANT_CARD",
    "THUMBNAIL_VARIANTS",
    "THUMBNAIL_WEBP_QUALITY",
    "ThumbnailError",
    "ThumbnailResult",
    "ThumbnailRow",
    "disk_path",
    "identity_hash",
]