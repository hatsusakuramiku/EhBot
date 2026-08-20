from __future__ import annotations

import zipfile
from pathlib import Path

from app.archive.errors import ArchiveError, ArchivePasswordRequired
from app.archive.models import (
    BACKEND_ZIPFILE,
    FORMAT_ZIP,
    ArchiveManifest,
    ArchiveMember,
)
from app.archive.safety import normalize_member_name


CHUNK_SIZE = 64 * 1024
HEADER_SIZE = 16

# Central-directory flag bit 0 marks an encrypted member.
_ENCRYPTED_FLAG = 0x1
# Unix symlink mode bits stored in the high half of external_attr.
_SYMLINK_MODE = 0xA000


class ZipfileBackend:
    """Standard-library ZIP/CBZ backend used for the guaranteed low-memory path."""

    name = BACKEND_ZIPFILE
    streaming = True

    def inspect(
        self, volumes: tuple[Path, ...], password: str | None
    ) -> ArchiveManifest:
        source = volumes[0]
        members: list[ArchiveMember] = []
        encrypted = False
        try:
            with zipfile.ZipFile(source, "r") as archive:
                if password:
                    archive.setpassword(password.encode("utf-8"))
                for info in archive.infolist():
                    member_encrypted = bool(info.flag_bits & _ENCRYPTED_FLAG)
                    encrypted = encrypted or member_encrypted
                    is_dir = info.is_dir() or info.filename.endswith("/")
                    header = b""
                    if not is_dir and not member_encrypted:
                        header = self._read_header(archive, info)
                    members.append(
                        ArchiveMember(
                            name=info.filename,
                            size=int(info.file_size),
                            compressed_size=int(info.compress_size),
                            is_dir=is_dir,
                            is_symlink=self._is_symlink(info),
                            encrypted=member_encrypted,
                            header=header,
                        )
                    )
        except (OSError, zipfile.BadZipFile) as exc:
            raise ArchiveError(
                "ARCHIVE_UNREADABLE",
                f"\u65e0\u6cd5\u8bfb\u53d6 ZIP \u538b\u7f29\u5305: {exc}",
            ) from exc
        return ArchiveManifest(
            source_format=FORMAT_ZIP,
            members=tuple(members),
            volumes=(source,),
            encrypted=encrypted,
        )

    @staticmethod
    def _is_symlink(info: zipfile.ZipInfo) -> bool:
        return bool((info.external_attr >> 16) & _SYMLINK_MODE == _SYMLINK_MODE)

    @staticmethod
    def _read_header(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
        try:
            with archive.open(info, "r") as reader:
                return reader.read(HEADER_SIZE)
        except (OSError, RuntimeError, zipfile.BadZipFile, NotImplementedError):
            return b""

    def test_password(
        self, volumes: tuple[Path, ...], password: str | None
    ) -> None:
        source = volumes[0]
        try:
            with zipfile.ZipFile(source, "r") as archive:
                if password:
                    archive.setpassword(password.encode("utf-8"))
                target = next(
                    (info for info in archive.infolist() if not info.is_dir()),
                    None,
                )
                if target is None:
                    return
                with archive.open(target, "r") as reader:
                    reader.read(1)
        except RuntimeError as exc:
            raise ArchivePasswordRequired() from exc
        except NotImplementedError as exc:
            raise ArchiveError(
                "ARCHIVE_COMPRESSION_UNSUPPORTED",
                "ZIP \u4f7f\u7528\u4e86\u4e0d\u53d7\u652f\u6301\u7684\u52a0\u5bc6\u6216\u538b\u7f29\u65b9\u5f0f",
            ) from exc
        except (OSError, zipfile.BadZipFile) as exc:
            raise ArchiveError(
                "ARCHIVE_UNREADABLE",
                f"\u65e0\u6cd5\u8bfb\u53d6 ZIP \u538b\u7f29\u5305: {exc}",
            ) from exc

    def extract(
        self,
        volumes: tuple[Path, ...],
        destination: Path,
        password: str | None,
        members: tuple[ArchiveMember, ...],
    ) -> dict[str, Path]:
        source = volumes[0]
        destination.mkdir(parents=True, exist_ok=True)
        resolved_root = destination.resolve()
        extracted: dict[str, Path] = {}
        wanted = {normalize_member_name(member.name): member.name for member in members}
        try:
            with zipfile.ZipFile(source, "r") as archive:
                if password:
                    archive.setpassword(password.encode("utf-8"))
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    safe_name = normalize_member_name(info.filename)
                    if safe_name not in wanted:
                        continue
                    target = (destination / safe_name).resolve()
                    if not target.is_relative_to(resolved_root):
                        raise ArchiveError(
                            "ARCHIVE_MEMBER_TRAVERSAL",
                            f"\u6210\u5458 {info.filename} \u8df3\u51fa\u4e86\u5de5\u4f5c\u76ee\u5f55",
                        )
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info, "r") as reader:
                        with target.open("wb") as writer:
                            while True:
                                chunk = reader.read(CHUNK_SIZE)
                                if not chunk:
                                    break
                                writer.write(chunk)
                    extracted[wanted[safe_name]] = target
        except RuntimeError as exc:
            raise ArchivePasswordRequired() from exc
        except (OSError, zipfile.BadZipFile) as exc:
            raise ArchiveError(
                "ARCHIVE_EXTRACT_FAILED",
                f"ZIP \u89e3\u538b\u5931\u8d25: {exc}",
            ) from exc
        return extracted

    def stream_pages(
        self,
        volumes: tuple[Path, ...],
        password: str | None,
        members: tuple[ArchiveMember, ...],
        page_names: tuple[str, ...],
        destination: Path,
        comicinfo: bytes,
    ) -> int:
        """Copy pages straight from the source ZIP into the CBZ.

        This keeps the 512 MB profile's guaranteed path free of a full
        extraction to disk.
        """
        source = volumes[0]
        destination.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        try:
            with zipfile.ZipFile(source, "r") as archive:
                if password:
                    archive.setpassword(password.encode("utf-8"))
                index = {info.filename: info for info in archive.infolist()}
                with zipfile.ZipFile(
                    destination, "w", zipfile.ZIP_STORED
                ) as target:
                    target.writestr("ComicInfo.xml", comicinfo)
                    for member, page_name in zip(members, page_names, strict=True):
                        info = index.get(member.name)
                        if info is None:
                            raise ArchiveError(
                                "ARCHIVE_MEMBER_MISSING",
                                f"\u538b\u7f29\u5305\u6210\u5458 {member.name} \u5df2\u4e0d\u53ef\u8bfb",
                            )
                        with archive.open(info, "r") as reader:
                            with target.open(page_name, "w") as writer:
                                while True:
                                    chunk = reader.read(CHUNK_SIZE)
                                    if not chunk:
                                        break
                                    writer.write(chunk)
                        written += 1
        except RuntimeError as exc:
            destination.unlink(missing_ok=True)
            raise ArchivePasswordRequired() from exc
        except (OSError, zipfile.BadZipFile) as exc:
            destination.unlink(missing_ok=True)
            raise ArchiveError(
                "ARCHIVE_PACK_FAILED",
                f"\u751f\u6210 CBZ \u5931\u8d25: {exc}",
            ) from exc
        return written

    def pack_cbz(
        self,
        pages: tuple[tuple[str, Path], ...],
        destination: Path,
        comicinfo: bytes,
    ) -> int:
        destination.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        try:
            with zipfile.ZipFile(destination, "w", zipfile.ZIP_STORED) as target:
                target.writestr("ComicInfo.xml", comicinfo)
                for page_name, path in pages:
                    with path.open("rb") as reader:
                        with target.open(page_name, "w") as writer:
                            while True:
                                chunk = reader.read(CHUNK_SIZE)
                                if not chunk:
                                    break
                                writer.write(chunk)
                    written += 1
        except OSError as exc:
            destination.unlink(missing_ok=True)
            raise ArchiveError(
                "ARCHIVE_PACK_FAILED",
                f"\u751f\u6210 CBZ \u5931\u8d25: {exc}",
            ) from exc
        return written


__all__ = ["ZipfileBackend"]