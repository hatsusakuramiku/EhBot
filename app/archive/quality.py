"""Optional lossy re-encode of pages before they are packed into a CBZ.

Re-encoding is irreversible, so ``original`` stays the default and every other
level is an explicit operator choice made on the settings page. The levels are
deliberately coarse (three presets, not free-form sliders) because the useful
decision is "reading copy vs archive copy", and a per-book JPEG quality knob
would only produce libraries nobody can reason about later.

Only JPEG pages are re-encoded. PNG line art frequently *grows* when pushed
through JPEG and loses alpha on the way, so a PNG page is always copied
byte-for-byte and simply counted as skipped. A re-encoded page is also kept
only when it is actually smaller than the original: spending CPU to produce a
bigger, lossier file would be strictly worse than doing nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath


QUALITY_ORIGINAL = "original"
QUALITY_HIGH = "high"
QUALITY_MEDIUM = "medium"
QUALITY_LOW = "low"

#: Ordered for the settings page; ``original`` first because it is the default.
IMAGE_QUALITY_LEVELS: tuple[str, ...] = (
    QUALITY_ORIGINAL,
    QUALITY_HIGH,
    QUALITY_MEDIUM,
    QUALITY_LOW,
)

#: Extensions handled by the JPEG re-encoder. Everything else is passed
#: through untouched.
JPEG_EXTENSIONS: frozenset[str] = frozenset({".jpg", ".jpeg"})


@dataclass(frozen=True, slots=True)
class ImageQualityProfile:
    """One re-encode preset: JPEG quality plus an optional downscale."""

    level: str
    jpeg_quality: int | None = None
    max_edge: int | None = None

    @property
    def rewrites(self) -> bool:
        return self.jpeg_quality is not None


IMAGE_QUALITY_PROFILES: dict[str, ImageQualityProfile] = {
    QUALITY_ORIGINAL: ImageQualityProfile(QUALITY_ORIGINAL),
    QUALITY_HIGH: ImageQualityProfile(QUALITY_HIGH, jpeg_quality=85),
    QUALITY_MEDIUM: ImageQualityProfile(QUALITY_MEDIUM, jpeg_quality=60),
    QUALITY_LOW: ImageQualityProfile(QUALITY_LOW, jpeg_quality=40, max_edge=3000),
}

QUALITY_LABELS: dict[str, str] = {
    QUALITY_ORIGINAL: "\u539f\u59cb\u6587\u4ef6\uff08\u4e0d\u91cd\u7f16\u7801\uff09",
    QUALITY_HIGH: "\u9ad8\u8d28\u91cf\uff08JPEG 85\u3001\u4e0d\u7f29\u653e\uff09",
    QUALITY_MEDIUM: "\u4e2d\u8d28\u91cf\uff08JPEG 60\u3001\u4e0d\u7f29\u653e\uff09",
    QUALITY_LOW: "\u4f4e\u8d28\u91cf\uff08JPEG 40\u3001\u6700\u957f\u8fb9 3000px\uff09",
}


def normalize_quality(value: str | None) -> str:
    """Map a stored or submitted value onto a known level, defaulting to original."""
    candidate = (value or "").strip().lower()
    if candidate in IMAGE_QUALITY_PROFILES:
        return candidate
    return QUALITY_ORIGINAL


def quality_profile(level: str | None) -> ImageQualityProfile:
    return IMAGE_QUALITY_PROFILES[normalize_quality(level)]


def is_jpeg_page(name: str) -> bool:
    return PurePosixPath(name.lower()).suffix in JPEG_EXTENSIONS


@dataclass(frozen=True, slots=True)
class ReencodeOutcome:
    """What actually happened to one page, for provenance and logging."""

    page_name: str
    path: Path
    rewritten: bool
    original_bytes: int
    final_bytes: int


def reencode_page(
    page_name: str,
    source: Path,
    profile: ImageQualityProfile,
    staging: Path,
) -> ReencodeOutcome:
    """Re-encode one JPEG page into ``staging``, or keep the original.

    The original is kept whenever re-encoding cannot help or cannot be trusted:
    a non-JPEG page, an unreadable image, or a result that is not smaller than
    what came in. A failure here never fails the book, because a slightly
    larger page is not worth losing an otherwise complete archive over.
    """
    original_bytes = _size_of(source)
    if not profile.rewrites or not is_jpeg_page(page_name):
        return ReencodeOutcome(
            page_name=page_name,
            path=source,
            rewritten=False,
            original_bytes=original_bytes,
            final_bytes=original_bytes,
        )
    try:
        from PIL import Image, ImageFile
    except ImportError:  # pragma: no cover - Pillow is a declared dependency
        return ReencodeOutcome(
            page_name=page_name,
            path=source,
            rewritten=False,
            original_bytes=original_bytes,
            final_bytes=original_bytes,
        )
    staging.mkdir(parents=True, exist_ok=True)
    target = staging / page_name
    try:
        # A truncated page still reads as a valid JPEG for most readers, so
        # rewriting what was decoded beats refusing the whole book.
        ImageFile.LOAD_TRUNCATED_IMAGES = True
        with Image.open(source) as image:
            image.load()
            converted = image.convert("RGB") if image.mode != "RGB" else image
            if profile.max_edge is not None:
                converted = _downscale(converted, profile.max_edge, Image)
            converted.save(
                target,
                format="JPEG",
                quality=profile.jpeg_quality,
                optimize=True,
                progressive=False,
            )
    except (OSError, ValueError, Image.DecompressionBombError):
        # A page that cannot be decoded, or one large enough to look like a
        # decompression bomb, is shipped as-is instead of failing the book.
        target.unlink(missing_ok=True)
        return ReencodeOutcome(
            page_name=page_name,
            path=source,
            rewritten=False,
            original_bytes=original_bytes,
            final_bytes=original_bytes,
        )
    final_bytes = _size_of(target)
    if original_bytes and final_bytes >= original_bytes:
        target.unlink(missing_ok=True)
        return ReencodeOutcome(
            page_name=page_name,
            path=source,
            rewritten=False,
            original_bytes=original_bytes,
            final_bytes=original_bytes,
        )
    return ReencodeOutcome(
        page_name=page_name,
        path=target,
        rewritten=True,
        original_bytes=original_bytes,
        final_bytes=final_bytes,
    )


def _downscale(image, max_edge: int, image_module):
    width, height = image.size
    longest = max(width, height)
    if longest <= max_edge:
        return image
    scale = max_edge / longest
    return image.resize(
        (max(int(width * scale), 1), max(int(height * scale), 1)),
        image_module.Resampling.LANCZOS,
    )


def _size_of(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def quality_note(level: str | None) -> str:
    """Describe the applied re-encode policy, empty for ``original``.

    The note records the policy rather than per-page results, because it is
    written into ComicInfo.xml before the pages are packed. It is what lets a
    reading-grade book be found and replaced later without opening every page.
    """
    normalized = normalize_quality(level)
    if normalized == QUALITY_ORIGINAL:
        return ""
    profile = IMAGE_QUALITY_PROFILES[normalized]
    parts = [f"requality={normalized}", f"q{profile.jpeg_quality}"]
    if profile.max_edge is not None:
        parts.append(f"max{profile.max_edge}px")
    return " ".join(parts)


__all__ = [
    "IMAGE_QUALITY_LEVELS",
    "IMAGE_QUALITY_PROFILES",
    "ImageQualityProfile",
    "QUALITY_HIGH",
    "QUALITY_LABELS",
    "QUALITY_LOW",
    "QUALITY_MEDIUM",
    "QUALITY_ORIGINAL",
    "ReencodeOutcome",
    "is_jpeg_page",
    "normalize_quality",
    "quality_note",
    "quality_profile",
    "reencode_page",
]
