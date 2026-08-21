"""Take delivery of a finished torrent without disturbing the seed.

The client keeps seeding after the download completes, so the payload in
`save_path` belongs to the client, not to EhBot. Everything here is therefore
non-destructive: a hard link when the filesystem allows it, a copy when it does
not, and never a move.
"""

from __future__ import annotations

from pathlib import Path
import shutil

from app.archive.safety import is_image_member
from app.telegraph.packer import pack_directory
from app.torrent.models import TorrentDelivery, TorrentError


#: Single-file torrents EhBot can register as an archive directly. EH torrents
#: are usually exactly one `.zip`.
ARCHIVE_SUFFIXES = frozenset({".zip", ".cbz", ".rar", ".cbr", ".7z", ".cb7"})


def resolve_content_path(
    content_path: str, client_root: str, local_root: str
) -> Path:
    """Translate the client's view of a path into EhBot's view.

    qBittorrent reports `content_path` in its own filesystem namespace. When the
    client runs in another container the two namespaces differ, so the client
    prefix is swapped for the local one. Identical roots make this a no-op.
    """
    if not content_path:
        raise TorrentError(
            "TORRENT_CONTENT_UNREACHABLE", "客户端未报告内容路径"
        )
    if not client_root or not local_root or client_root == local_root:
        return Path(content_path)
    normalized = content_path.replace("\\", "/")
    prefix = client_root.replace("\\", "/").rstrip("/")
    if normalized.lower().startswith(prefix.lower()):
        relative = normalized[len(prefix) :].lstrip("/")
        return Path(local_root) / relative
    # A path outside the configured save directory is not something to guess at.
    return Path(content_path)


def _link_or_copy(source: Path, destination: Path) -> None:
    """Hard-link the payload, falling back to a copy.

    A move would break seeding, so it is never attempted. A hard link costs no
    space; a copy is the price of a filesystem or mount boundary.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    try:
        destination.hardlink_to(source)
        return
    except (OSError, NotImplementedError):
        shutil.copy2(source, destination)


def take_delivery(
    content_path: Path, destination_dir: Path, candidate_id: int
) -> TorrentDelivery:
    """Produce an archive EhBot owns from a finished torrent's payload."""
    if not content_path.exists():
        raise TorrentError(
            "TORRENT_CONTENT_UNREACHABLE",
            f"读不到下载内容: {content_path}",
        )
    destination_dir.mkdir(parents=True, exist_ok=True)
    if content_path.is_file():
        suffix = content_path.suffix.lower()
        if suffix in ARCHIVE_SUFFIXES:
            destination = (
                destination_dir / f"candidate-{candidate_id}{suffix}"
            )
            _link_or_copy(content_path, destination)
            return TorrentDelivery(
                archive_path=str(destination),
                size_bytes=destination.stat().st_size,
                was_directory=False,
            )
        if is_image_member(content_path.name):
            # A single loose image is a book of one page; pack it so the rest
            # of the pipeline sees the archive it expects.
            destination = destination_dir / f"candidate-{candidate_id}.zip"
            size = pack_directory(content_path.parent, destination)
            return TorrentDelivery(
                archive_path=str(destination),
                size_bytes=size,
                was_directory=True,
            )
        raise TorrentError(
            "TORRENT_CONTENT_UNEXPECTED",
            f"下载内容既不是压缩包也不是图片: {content_path.name}",
        )
    if content_path.is_dir():
        nested = [
            path
            for path in sorted(content_path.rglob("*"))
            if path.is_file()
        ]
        if not nested:
            raise TorrentError(
                "TORRENT_CONTENT_UNEXPECTED", "下载目录为空"
            )
        archives = [
            path
            for path in nested
            if path.suffix.lower() in ARCHIVE_SUFFIXES
        ]
        if len(archives) == 1 and len(nested) == 1:
            destination = (
                destination_dir
                / f"candidate-{candidate_id}{archives[0].suffix.lower()}"
            )
            _link_or_copy(archives[0], destination)
            return TorrentDelivery(
                archive_path=str(destination),
                size_bytes=destination.stat().st_size,
                was_directory=False,
            )
        destination = destination_dir / f"candidate-{candidate_id}.zip"
        size = pack_directory(content_path, destination)
        return TorrentDelivery(
            archive_path=str(destination),
            size_bytes=size,
            was_directory=True,
        )
    raise TorrentError(
        "TORRENT_CONTENT_UNEXPECTED",
        f"下载内容既非文件也非目录: {content_path}",
    )


__all__ = ["ARCHIVE_SUFFIXES", "resolve_content_path", "take_delivery"]