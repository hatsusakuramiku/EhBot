from __future__ import annotations

import logging
import shutil
from pathlib import Path

from app.archive.backends.seven_zip import SevenZipBackend
from app.archive.backends.zip_backend import ZipfileBackend
from app.archive.errors import (
    ArchiveError,
    ArchivePasswordRequired,
    ArchiveToolUnavailable,
    ArchiveVolumesMissing,
    UnsupportedArchiveFormat,
)
from app.archive.formats import detect_source_format, resolve_volumes
from app.archive.models import (
    BACKEND_SEVEN_ZIP,
    BACKEND_ZIPFILE,
    FORMAT_UNKNOWN,
    FORMAT_ZIP,
    ArchiveProcessResult,
    ArchiveTaskSnapshot,
    SafetyLimits,
    ToolProfile,
)
from app.archive.quality import (
    QUALITY_ORIGINAL,
    ImageQualityProfile,
    normalize_quality,
    quality_profile,
    reencode_page,
)
from app.archive.safety import page_file_names, validate_manifest


LOGGER = logging.getLogger(__name__)


class ArchiveProcessor:
    """Run the archive pipeline for one task with one selected backend.

    The order is fixed: inspect volumes, resolve the password, validate
    safety, extract or stream, then pack the CBZ with the same backend.
    """

    def __init__(
        self,
        *,
        profiles: tuple[ToolProfile, ...],
        limits: SafetyLimits | None = None,
        passwords: tuple[tuple[int, str], ...] = (),
        subprocess_runner=None,
        tools_path: Path | None = None,
        image_quality: str = QUALITY_ORIGINAL,
    ) -> None:
        self._profiles = profiles
        self._limits = limits or SafetyLimits()
        self._passwords = passwords
        self._subprocess_runner = subprocess_runner
        self._tools_path = tools_path
        self._image_quality = normalize_quality(image_quality)

    def select_profile(self, source_format: str) -> ToolProfile:
        if source_format == FORMAT_UNKNOWN:
            raise UnsupportedArchiveFormat(source_format)
        eligible = [
            profile
            for profile in self._profiles
            if profile.enabled and profile.supports(source_format)
        ]
        if not eligible:
            raise UnsupportedArchiveFormat(source_format)
        # Prefer the streaming built-in backend when it can handle the format.
        eligible.sort(key=lambda profile: (profile.backend != BACKEND_ZIPFILE,))
        return eligible[0]

    def build_backend(self, profile: ToolProfile):
        if profile.backend == BACKEND_ZIPFILE:
            return ZipfileBackend()
        if profile.backend == BACKEND_SEVEN_ZIP:
            return SevenZipBackend(
                profile,
                runner=self._subprocess_runner,
                tools_path=self._tools_path,
            )
        raise ArchiveToolUnavailable(
            f"\u540e\u7aef {profile.backend} \u672a\u5b9e\u73b0"
        )

    def process(
        self,
        source: Path,
        *,
        destination: Path,
        work_directory: Path,
        comicinfo_builder,
        library_path: Path | None = None,
    ) -> ArchiveProcessResult:
        if not source.exists():
            raise ArchiveError(
                "ARCHIVE_SOURCE_MISSING",
                "\u539f\u59cb\u538b\u7f29\u5305\u4e0d\u5b58\u5728",
            )
        if source.resolve() == destination.resolve():
            raise ArchiveError(
                "ARCHIVE_SOURCE_DESTINATION_CONFLICT",
                "\u6e90\u6587\u4ef6\u4e0e\u76ee\u6807\u6587\u4ef6\u4e0d\u80fd\u76f8\u540c",
            )
        volumes, missing = resolve_volumes(source)
        if missing:
            raise ArchiveVolumesMissing(missing)
        source_format = detect_source_format(source)
        profile = self.select_profile(source_format)
        backend = self.build_backend(profile)
        snapshot = ArchiveTaskSnapshot(
            backend=profile.backend,
            tool_profile=profile.name,
            source_format=source_format,
            library_path=str(library_path or destination.parent),
            work_path=str(work_directory),
        )

        password_id: int | None = None
        password: str | None = None
        try:
            manifest = backend.inspect(volumes, None)
        except ArchivePasswordRequired:
            # A header-encrypted archive cannot even be listed without the
            # password, so the vault must be consulted before inspection.
            password_id, password = self._resolve_password(
                backend, volumes, probe=self._probe_inspect(backend)
            )
            manifest = backend.inspect(volumes, password)
        else:
            if manifest.encrypted:
                password_id, password = self._resolve_password(backend, volumes)
                manifest = backend.inspect(volumes, password)
            else:
                backend.test_password(volumes, None)

        pages = validate_manifest(manifest, self._limits)
        page_names = page_file_names(pages)
        comicinfo = comicinfo_builder(len(pages))

        quality = quality_profile(self._image_quality)
        rewritten = 0

        partial = destination.with_name(f"{destination.name}.part")
        partial.parent.mkdir(parents=True, exist_ok=True)
        partial.unlink(missing_ok=True)
        task_directory = work_directory / f"extract-{destination.stem}"
        try:
            # Streaming copies members byte-for-byte, so it can only be used
            # when the pages are published unchanged. A re-encode has to see
            # decoded pixels and therefore goes through the extract path.
            if (
                getattr(backend, "streaming", False)
                and source_format == FORMAT_ZIP
                and not quality.rewrites
            ):
                written = backend.stream_pages(
                    volumes, password, pages, page_names, partial, comicinfo
                )
            else:
                shutil.rmtree(task_directory, ignore_errors=True)
                extracted = backend.extract(
                    volumes, task_directory, password, pages
                )
                staged = tuple(
                    (page_name, extracted[member.name])
                    for member, page_name in zip(pages, page_names, strict=True)
                    if member.name in extracted
                )
                if len(staged) != len(pages):
                    raise ArchiveError(
                        "ARCHIVE_MEMBER_MISSING",
                        "\u89e3\u538b\u7ed3\u679c\u7f3a\u5c11\u90e8\u5206\u56fe\u7247\u9875",
                    )
                if quality.rewrites:
                    staged, rewritten = self._reencode_pages(
                        staged, quality, task_directory / "requality"
                    )
                written = backend.pack_cbz(staged, partial, comicinfo)
            destination.parent.mkdir(parents=True, exist_ok=True)
            partial.replace(destination)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        finally:
            shutil.rmtree(task_directory, ignore_errors=True)

        return ArchiveProcessResult(
            cbz_path=destination,
            page_count=written,
            skipped_members=tuple(
                member.name
                for member in manifest.files
                if member not in pages
            ),
            snapshot=snapshot,
            password_id=password_id,
            volume_count=len(volumes),
            image_quality=self._image_quality,
            rewritten_pages=rewritten,
        )

    @staticmethod
    def _reencode_pages(
        staged: tuple[tuple[str, Path], ...],
        profile: ImageQualityProfile,
        staging: Path,
    ) -> tuple[tuple[tuple[str, Path], ...], int]:
        """Re-encode the staged pages, keeping originals wherever it does not help.

        The page order and the page names are the ones already decided by the
        safety layer, so a re-encode can never reorder or rename a book: it
        only ever replaces the bytes behind a page.
        """
        rewritten = 0
        result: list[tuple[str, Path]] = []
        for page_name, path in staged:
            outcome = reencode_page(page_name, path, profile, staging)
            if outcome.rewritten:
                rewritten += 1
            result.append((page_name, outcome.path))
        if rewritten:
            # In the message rather than through `extra=`: the formatter
            # serialises a fixed field list and these three are not on it, so
            # the line used to reach the log with none of its numbers.
            LOGGER.info(
                "archive_pages_reencoded quality=%s rewritten=%d of %d pages",
                profile.level,
                rewritten,
                len(staged),
            )
        return tuple(result), rewritten

    @staticmethod
    def _probe_inspect(backend):
        def probe(volumes, password) -> None:
            backend.inspect(volumes, password)

        return probe

    def _resolve_password(
        self, backend, volumes, probe=None
    ) -> tuple[int | None, str | None]:
        """Try the vault entries in attempt order, then the empty password.

        The vault is already ordered `last successful -> priority -> id`, so
        the first entry that opens the archive wins.
        """
        verify = probe or backend.test_password
        attempts: list[tuple[int | None, str | None]] = [
            (password_id, plaintext) for password_id, plaintext in self._passwords
        ]
        attempts.append((None, None))
        for password_id, plaintext in attempts:
            try:
                verify(volumes, plaintext)
            except ArchivePasswordRequired:
                continue
            return password_id, plaintext
        raise ArchivePasswordRequired()


__all__ = ["ArchiveProcessor"]