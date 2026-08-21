"""Pack fetched pages into a ZIP the existing archive pipeline can consume.

``ZIP_STORED`` is used because every member is already a compressed image;
deflating them again costs CPU on a 1C host and saves nothing. The output is
written to a ``.part`` file and renamed, so a crash never leaves a truncated
archive that the conversion pipeline would happily pick up.
"""

from __future__ import annotations

from pathlib import Path
import zipfile

from app.archive.safety import is_image_member, natural_sort_key
from app.telegraph.models import FetchedImage, TelegraphError


def pack_images(
    images: tuple[FetchedImage, ...], destination: Path
) -> int:
    """Write the given pages to ``destination`` in the order supplied."""
    if not images:
        raise TelegraphError(
            "TELEGRAPH_NO_IMAGES", "没有可打包的图片"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    total = 0
    try:
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_STORED) as archive:
            for image in images:
                archive.writestr(image.name, image.data)
                total += len(image.data)
        partial.replace(destination)
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise TelegraphError(
            "TELEGRAPH_IMAGE_FAILED", f"打包失败: {exc}"
        ) from exc
    return total


def pack_directory(source: Path, destination: Path) -> int:
    """Pack an image directory, used when a torrent delivers loose files.

    Members are ordered with the same natural sort the archive pipeline uses,
    so page order does not change depending on which provider delivered the
    book. A directory holding anything other than images is refused rather
    than silently filtered, because a partial book is worse than a failure.
    """
    if not source.is_dir():
        raise TelegraphError(
            "TELEGRAPH_NO_IMAGES", f"不是目录: {source}"
        )
    files = sorted(
        (path for path in source.rglob("*") if path.is_file()),
        key=lambda path: natural_sort_key(
            path.relative_to(source).as_posix()
        ),
    )
    if not files:
        raise TelegraphError(
            "TELEGRAPH_NO_IMAGES", f"目录中没有文件: {source}"
        )
    non_images = [
        path.relative_to(source).as_posix()
        for path in files
        if not is_image_member(path.name)
    ]
    if non_images:
        raise TelegraphError(
            "TELEGRAPH_IMAGE_BLOCKED",
            f"目录包含非图片文件: {', '.join(non_images[:3])}",
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    total = 0
    try:
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_STORED) as archive:
            for index, path in enumerate(files, start=1):
                suffix = path.suffix.lower() or ".jpg"
                archive.write(path, f"{index:04d}{suffix}")
                total += path.stat().st_size
        partial.replace(destination)
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise TelegraphError(
            "TELEGRAPH_IMAGE_FAILED", f"打包失败: {exc}"
        ) from exc
    return total


__all__ = ["pack_directory", "pack_images"]