from __future__ import annotations

import logging
from pathlib import Path
import zipfile

from app.conversion.comicinfo import build_comicinfo_xml


CHUNK_SIZE = 64 * 1024


class ConversionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message


SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(
    {".zip", ".cbz"}
)


def detect_format(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".zip", ".cbz"}:
        return "zip"
    if suffix == ".rar":
        return "rar"
    if suffix == ".7z":
        return "7z"
    return "unknown"


def is_supported(path: Path) -> bool:
    return detect_format(path) in {"zip"}


def stream_zip_to_cbz(
    source: Path,
    destination: Path,
    *,
    title: str,
    artist: str | None = None,
    language: str | None = None,
    category: str | None = None,
    tags: tuple[str, ...] = (),
    rating: float | None = None,
    description: str | None = None,
    japanese_title: str | None = None,
    group: str | None = None,
    parody: str | None = None,
    character: str | None = None,
    web: str | None = None,
) -> int:
    """Stream the ZIP/CBZ source to a destination CBZ, prepending ComicInfo.xml.

    Returns the number of entries (excluding ComicInfo.xml) that were copied.
    """
    if not source.exists():
        raise ConversionError(
            "CONVERSION_SOURCE_MISSING",
            "Source archive does not exist",
        )
    if source.resolve() == destination.resolve():
        raise ConversionError(
            "CONVERSION_SOURCE_DESTINATION_CONFLICT",
            "Source and destination paths must differ",
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    pages: list[str] = []
    try:
        with zipfile.ZipFile(source, "r") as zin:
            entries = [
                info
                for info in zin.infolist()
                if not info.is_dir() and not info.filename.endswith("/")
            ]
            with zipfile.ZipFile(
                destination, "w", zipfile.ZIP_DEFLATED
            ) as zout:
                comicinfo = build_comicinfo_xml(
                    title=title,
                    artist=artist,
                    language=language,
                    category=category,
                    tags=tags,
                    rating=rating,
                    description=description,
                    page_count=len(entries),
                    japanese_title=japanese_title,
                    group=group,
                    parody=parody,
                    character=character,
                    web=web,
                )
                zout.writestr("ComicInfo.xml", comicinfo)
                for info in entries:
                    name = info.filename
                    if name.lower() == "comicinfo.xml":
                        continue
                    pages.append(name)
                    with zin.open(info, "r") as reader:
                        with zout.open(name, "w") as writer:
                            while True:
                                chunk = reader.read(CHUNK_SIZE)
                                if not chunk:
                                    break
                                writer.write(chunk)
    except (OSError, zipfile.BadZipFile) as exc:
        destination.unlink(missing_ok=True)
        logging.getLogger(__name__).warning(
            "zip_to_cbz_failed",
            extra={"error_code": "CONVERSION_FAILED"},
        )
        raise ConversionError(
            "CONVERSION_FAILED",
            f"无法将压缩包转换为 CBZ: {exc}",
        ) from exc
    return len(pages)


__all__ = [
    "ConversionError",
    "SUPPORTED_EXTENSIONS",
    "detect_format",
    "is_supported",
    "stream_zip_to_cbz",
]
