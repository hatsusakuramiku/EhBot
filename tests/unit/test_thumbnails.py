"""Unit coverage for the thumbnail identity, disk layout and renderer."""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image
import pytest

from app.archive.safety import looks_like_image
from app.thumbnails import (
    THUMBNAIL_CARD_MAX_EDGE,
    THUMBNAIL_VARIANT_CARD,
    ThumbnailError,
)
from app.thumbnails.identity import disk_path, identity_hash
from app.thumbnails.render import render_card


THUMB_URL = "https://ehgt.org/ab/cd/abcdef0123456789-1234567-250-350-jpg_250.jpg"


def encode(width: int, height: int, *, mode: str = "RGB", fmt: str = "JPEG") -> bytes:
    """Produce a real encoded image, so the renderer runs its real decode path."""
    buffer = io.BytesIO()
    Image.new(mode, (width, height), (120, 90, 60)).save(buffer, format=fmt)
    return buffer.getvalue()


class TestIdentityHash:
    def test_the_same_source_always_names_the_same_thumbnail(self) -> None:
        first = identity_hash(THUMB_URL, THUMBNAIL_VARIANT_CARD)
        second = identity_hash(THUMB_URL, THUMBNAIL_VARIANT_CARD)

        assert first == second
        assert len(first) == 64
        assert first == first.lower()

    def test_the_variant_is_part_of_the_identity(self) -> None:
        """Two variants of one URL must not collide.

        The serving URL carries no query string and the response is marked
        `immutable`, so a second variant sharing the hash would permanently
        hand out the wrong size from the browser cache.
        """
        card = identity_hash(THUMB_URL, THUMBNAIL_VARIANT_CARD)
        other = identity_hash(THUMB_URL, "origin")

        assert card != other

    def test_a_separator_keeps_variant_and_url_from_blurring(self) -> None:
        """`variant + url` concatenation alone would let the boundary move."""
        assert identity_hash("b", "cardax") != identity_hash("axb", "card")


class TestDiskPath:
    def test_the_hash_fans_out_into_two_levels(self, tmp_path: Path) -> None:
        digest = identity_hash(THUMB_URL, THUMBNAIL_VARIANT_CARD)

        path = disk_path(tmp_path, digest)

        assert path == (
            tmp_path / digest[:2] / digest[2:4] / f"{digest}.webp"
        )
        assert not path.parent.exists()

    def test_mkdir_creates_the_parents_but_not_the_file(
        self, tmp_path: Path
    ) -> None:
        digest = identity_hash(THUMB_URL, THUMBNAIL_VARIANT_CARD)

        path = disk_path(tmp_path, digest, mkdir=True)

        assert path.parent.is_dir()
        assert not path.exists()

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "abc",
            "../../etc/passwd",
            "a" * 63,
            "a" * 65,
        ],
    )
    def test_anything_that_is_not_a_digest_is_refused(
        self, tmp_path: Path, bad: str
    ) -> None:
        """The layout helper builds a path out of these characters.

        The endpoint validates the shape too, but the guard lives here so a
        future caller cannot reach the filesystem through a bare service call.
        """
        with pytest.raises(ValueError):
            disk_path(tmp_path, bad)


class TestLooksLikeImage:
    @pytest.mark.parametrize(
        "payload",
        [
            b"\xff\xd8\xff\xe0" + b"\x00" * 32,
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 32,
            b"GIF89a" + b"\x00" * 32,
            b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 32,
            b"BM" + b"\x00" * 32,
        ],
    )
    def test_known_containers_pass(self, payload: bytes) -> None:
        assert looks_like_image(payload)

    def test_an_html_error_page_is_not_an_image(self) -> None:
        """The failure mode this gate exists for.

        An image host under load answers with a styled HTML error page and a
        200, so the status code alone does not tell us we got pixels.
        """
        assert not looks_like_image(b"<!DOCTYPE html><html><body>403")
        assert not looks_like_image(b"\n  <html>" + b" " * 32)

    def test_a_short_body_is_not_trusted(self) -> None:
        assert not looks_like_image(b"\xff\xd8\xff")


class TestRenderCard:
    def test_output_is_always_webp(self) -> None:
        webp, content_type, width, height = render_card(encode(250, 350))

        assert content_type == "image/webp"
        assert (width, height) == (250, 350)
        with Image.open(io.BytesIO(webp)) as decoded:
            assert decoded.format == "WEBP"
            assert decoded.size == (250, 350)

    def test_a_png_source_is_re_encoded_not_passed_through(self) -> None:
        """Everything served is bytes we produced, whatever came in."""
        webp, _, _, _ = render_card(encode(200, 200, fmt="PNG"))

        with Image.open(io.BytesIO(webp)) as decoded:
            assert decoded.format == "WEBP"

    def test_an_oversized_cover_is_downscaled_to_the_card_cap(self) -> None:
        _, _, width, height = render_card(encode(1600, 2400))

        assert max(width, height) == THUMBNAIL_CARD_MAX_EDGE
        # Aspect ratio survives the downscale.
        assert width == int(1600 * (THUMBNAIL_CARD_MAX_EDGE / 2400))

    def test_a_small_cover_is_never_upscaled(self) -> None:
        """Gdata thumbs are already small; enlarging them only wastes bytes."""
        _, _, width, height = render_card(encode(120, 160))

        assert (width, height) == (120, 160)

    def test_transparency_is_flattened_onto_white(self) -> None:
        buffer = io.BytesIO()
        Image.new("RGBA", (64, 64), (255, 0, 0, 0)).save(buffer, format="PNG")

        webp, _, _, _ = render_card(buffer.getvalue())

        with Image.open(io.BytesIO(webp)) as decoded:
            assert decoded.mode == "RGB"
            assert decoded.convert("RGB").getpixel((0, 0)) == (255, 255, 255)

    def test_a_palette_image_survives_the_conversion(self) -> None:
        buffer = io.BytesIO()
        Image.new("P", (64, 64)).save(buffer, format="PNG")

        _, content_type, _, _ = render_card(buffer.getvalue())

        assert content_type == "image/webp"

    def test_non_image_bytes_are_refused_before_pillow_sees_them(self) -> None:
        with pytest.raises(ThumbnailError) as excinfo:
            render_card(b"<html><body>not an image</body></html>")

        assert excinfo.value.code == "IMAGE_NOT_RECOGNISED"

    def test_a_truncated_image_fails_to_decode(self) -> None:
        payload = encode(200, 200)

        with pytest.raises(ThumbnailError) as excinfo:
            render_card(payload[: len(payload) // 3])

        assert excinfo.value.code == "IMAGE_DECODE_FAILED"

    def test_an_oversized_edge_is_refused(self) -> None:
        """A decompression bomb costs its pixels at `load()`, not at `save()`.

        The gate can only run after the decode, so what it protects is every
        pixel operation after it — the resize in particular.
        """
        with pytest.raises(ThumbnailError) as excinfo:
            render_card(encode(5000, 10))

        assert excinfo.value.code == "IMAGE_TOO_LARGE"
