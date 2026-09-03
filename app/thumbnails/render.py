"""Image decode and WebP re-encode for the thumbnail proxy.

Security gates (magic-number pre-check, pixel-count limits, no upscaling) are
applied before any Pillow pixel operation. The output is always WebP, so the
cache stores bytes we control.
"""

from __future__ import annotations

import io
import logging
from typing import Tuple

from PIL import Image

from app.archive.safety import looks_like_image
from app.thumbnails import (
    MAX_PIXEL_COUNT,
    MAX_PIXEL_EDGE,
    THUMBNAIL_CARD_MAX_EDGE,
    THUMBNAIL_WEBP_QUALITY,
)
from app.thumbnails.errors import ThumbnailError

logger = logging.getLogger(__name__)


def _dimensions_ok(width: int, height: int) -> bool:
    """Reject if either dimension exceeds ``MAX_PIXEL_EDGE`` or total pixels exceed ``MAX_PIXEL_COUNT``."""
    if width > MAX_PIXEL_EDGE or height > MAX_PIXEL_EDGE:
        return False
    if width * height > MAX_PIXEL_COUNT:
        return False
    return True


def render_card(data: bytes) -> Tuple[bytes, str, int, int]:
    """Decode raw image bytes, re-encode as a WebP card variant.

    Parameters
    ----------
    data
        Raw image bytes from upstream.

    Returns
    -------
    ``(webp_bytes, content_type, width, height)``
        The re-encoded image, always ``image/webp``.

    Raises
    ------
    ThumbnailError
        If the bytes are not a recognised image, exceed dimension limits, or
        Pillow cannot decode them.
    """
    if not looks_like_image(data):
        raise ThumbnailError(
            "IMAGE_NOT_RECOGNISED",
            "上游返回的数据不是可识别的图片格式",
        )

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as exc:
        # The decoder's own words go in the message: `error` is not a field
        # the JSON formatter serialises, so attaching it there lost it.
        logger.warning(
            "thumbnail_decode_failed reason=%s",
            exc,
            extra={"error_code": "IMAGE_DECODE_FAILED"},
        )
        raise ThumbnailError(
            "IMAGE_DECODE_FAILED",
            "无法解码上游图片",
        ) from exc

    if not _dimensions_ok(img.width, img.height):
        raise ThumbnailError(
            "IMAGE_TOO_LARGE",
            "上游图片尺寸超出安全限制",
        )

    # Downscale if the longest edge exceeds the card cap. Never upscale.
    if max(img.width, img.height) > THUMBNAIL_CARD_MAX_EDGE:
        ratio = THUMBNAIL_CARD_MAX_EDGE / max(img.width, img.height)
        new_width = int(img.width * ratio)
        new_height = int(img.height * ratio)
        img = img.resize((new_width, new_height), Image.LANCZOS)

    # Convert RGBA/P to RGB if needed — WebP supports alpha, but the card
    # variant doesn't need it and stripping it saves bytes.
    if img.mode in ("P", "PA"):
        img = img.convert("RGBA")
    if img.mode == "RGBA":
        background = Image.new("RGB", img.size, (255, 255, 255))
        background.paste(img, mask=img.split()[3])
        img = background
    elif img.mode != "RGB":
        img = img.convert("RGB")

    out = io.BytesIO()
    img.save(out, format="WEBP", quality=THUMBNAIL_WEBP_QUALITY)
    webp_bytes = out.getvalue()

    return webp_bytes, "image/webp", img.width, img.height


__all__ = ["render_card"]