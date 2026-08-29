from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from app.archive.errors import (
    ArchiveError,
    ArchivePasswordRequired,
    ArchiveVolumesMissing,
)
from app.archive.models import SafetyLimits
from app.archive.processor import ArchiveProcessor
from app.archive.quality import quality_note
from app.archive.service import ArchiveSettingsService
from app.conversion.comicinfo import build_comicinfo_xml
from app.conversion.convert import ConversionError
from app.conversion.naming import (
    DEFAULT_LIBRARY_TEMPLATE,
    MAX_RELATIVE_PATH_LENGTH,
    LibraryPathError,
    LibraryTemplateError,
    check_library_segment,
    plan_library_path,
    render_library_path,
    safe_library_name,
    unique_library_target,
)
from app.db.database import Database
from app.downloads.models import (
    CONVERSION_STATE_COMPLETED,
    CONVERSION_STATE_FAILED,
    CONVERSION_STATE_PENDING,
    CONVERSION_STATE_RUNNING,
    CONVERSION_STATE_WAITING_PASSWORD,
    CONVERSION_STATE_WAITING_PATH,
    CONVERSION_STATE_WAITING_VOLUMES,
    DOWNLOAD_STATE_COMPLETED,
    DOWNLOAD_STATE_FAILED,
    DOWNLOAD_STATE_PENDING,
    PROVIDER_CONVERSION,
    RECOVERABLE_CONVERSION_STATES,
    DownloadState,
)
from app.review.models import (
    METADATA_FIELDS,
    STATUS_APPROVED,
)


def _metadata_lookup(metadata, field_name: str) -> str | None:
    for entry in metadata:
        if entry.field_name == field_name:
            return entry.field_value
    return None


def _scan_information(metadata, image_quality: str | None) -> str | None:
    """Append the re-encode policy to the source grade already recorded.

    The provider grade (for example `EH_TORRENT original 121.0MiB`) says where
    the pages came from; the appended note says what EhBot did to them. Keeping
    both in one field means a re-encoded book can be told apart from an
    untouched one without opening a single page.
    """
    source = _metadata_lookup(metadata, "ScanInformation")
    note = quality_note(image_quality)
    if not note:
        return source
    return f"{source} {note}" if source else note


def _metadata_tags(metadata) -> tuple[str, ...]:
    tags: list[str] = []
    for field_name in ("TagsRaw", "Tags"):
        value = _metadata_lookup(metadata, field_name)
        if not value:
            continue
        for item in value.replace("\n", ",").split(","):
            tag = item.strip()
            if tag and tag not in tags:
                tags.append(tag)
    return tuple(tags)


class ConversionService:
    def __init__(
        self,
        database: Database,
        work_path: Path,
        library_path: Path,
        settings_service: ArchiveSettingsService | None = None,
        data_path: Path | None = None,
        notify: Callable[..., object] | None = None,
    ) -> None:
        self._database = database
        self._work_path = work_path
        self._library_path = library_path
        self._settings = settings_service or ArchiveSettingsService(
            database,
            data_path or work_path,
            default_library_path=library_path,
            default_work_path=work_path,
        )
        self._worker_task: asyncio.Task[None] | None = None
        self._notify = notify

    async def _effective_paths(self) -> tuple[Path, Path]:
        """Read the directories per job so an operator change applies at once.

        The environment values remain the defaults; a stored override wins.
        Resolving here instead of in `__init__` means the setting takes effect
        on the next job rather than only after a restart.
        """
        library = await self._settings.library_path()
        work = await self._settings.work_path()
        return (library or self._library_path, work or self._work_path)

    async def _library_target(
        self,
        candidate_id: int,
        library_path: Path,
        metadata,
        title: str,
    ) -> Path:
        """Where this book's CBZ goes, per the operator's layout template.

        Read per job for the same reason the directories are: a template saved
        now applies to the next pack rather than after a restart. A stored
        template that no longer validates falls back to the flat default instead
        of failing the job -- the book is already downloaded, and refusing to
        publish it over a settings mistake is the worse outcome. The settings
        page is where an invalid template is caught, and it cannot be saved.
        """
        fallback = f"candidate-{candidate_id}"
        reserved = frozenset(
            await asyncio.to_thread(self._existing_cbz_paths_sync, candidate_id)
        )
        # An operator who renamed or moved this book has pinned where it lives,
        # and a repack must land there. Re-rendering the template would move the
        # file back and `unique_library_target` would then see the operator's
        # copy as somebody else's book and grow a ` (2)` beside it -- so the
        # rename would read as undone *and* duplicated. The pin wins over the
        # template for the same reason `is_locked` wins over a scrape: it is a
        # judgement the operator already made about this one book.
        pinned = await asyncio.to_thread(
            self._pinned_library_path_sync, candidate_id
        )
        if pinned is not None:
            # Re-checked here rather than trusted from the write. The write did
            # validate, but the ceiling is on the *whole* path and the library
            # root is a setting: moving the library deeper can push a path that
            # was legal when it was pinned past what the filesystem takes. The
            # refusal parks the job with the reason on it, which is the only way
            # the operator finds out at all.
            refusal = next(
                (
                    check_library_segment(segment)
                    for segment in (*pinned.parent.parts, pinned.stem)
                    if check_library_segment(segment) is not None
                ),
                None,
            )
            if refusal is not None:
                raise LibraryPathError(*refusal)
            if len(str(pinned)) > MAX_RELATIVE_PATH_LENGTH:
                raise LibraryPathError(
                    "PATH_TOO_LONG",
                    f"归档路径长度 {len(str(pinned))} 超过上限 "
                    f"{MAX_RELATIVE_PATH_LENGTH}，请在作品详情页改短归档路径",
                )
            return await asyncio.to_thread(
                unique_library_target, library_path / pinned, reserved=reserved
            )
        template = await self._settings.library_template()
        values = {
            "category": _metadata_lookup(metadata, "Category"),
            "artist": _metadata_lookup(metadata, "Artist"),
            "title": title,
        }
        try:
            relative = render_library_path(
                template, values, title_fallback=fallback
            )
        except LibraryTemplateError:
            logging.getLogger(__name__).warning(
                "library_template_unusable",
                extra={"error_code": "TEMPLATE_INVALID"},
            )
            relative = render_library_path(
                DEFAULT_LIBRARY_TEMPLATE, values, title_fallback=fallback
            )
        # Appended rather than `with_suffix`, which would read 「Vol. 1」 as a
        # name with a `. 1` extension and publish the book as `Vol.cbz`.
        target = library_path / relative.parent / f"{relative.name}.cbz"
        return await asyncio.to_thread(
            unique_library_target, target, reserved=reserved
        )

    async def planned_library_path(
        self, candidate_id: int, title: str, metadata
    ) -> PurePosixPath:
        """What the current template gives this book, refusing if unusable.

        The strict counterpart of `_library_target`, and the split is deliberate.
        `_library_target` runs inside a job for a book that is already
        downloaded, so it repairs what it can and never refuses -- failing a job
        over a punctuation mark would leave the book unpublished for a reason the
        operator did not ask about. This runs while the operator is waiting for
        an answer, on a path they have not agreed to yet, so it reports instead:
        a batch re-file that silently sanitised fifty titles would move fifty
        books to names nobody chose.

        Raises `LibraryPathError`, which the batch turns into a per-work reason.
        """
        template = await self._settings.library_template()
        return plan_library_path(
            template,
            {
                "category": _metadata_lookup(metadata, "Category"),
                "artist": _metadata_lookup(metadata, "Artist"),
                "title": title,
            },
            title_fallback=f"candidate-{candidate_id}",
        )

    async def metadata_for(self, candidate_id: int):
        """This work's metadata rows, for a caller planning its path.

        Exposed because `planned_library_path` needs them and the batch has no
        business reaching into `_fetch_metadata_sync`.
        """
        return await asyncio.to_thread(self._fetch_metadata_sync, candidate_id)

    @staticmethod
    def title_of(metadata, candidate_id: int) -> str:
        """The title the packer would use, resolved the one way it resolves it."""
        return (
            _metadata_lookup(metadata, "Title") or f"Candidate {candidate_id}"
        )

    def _pinned_library_path_sync(self, candidate_id: int) -> PurePosixPath | None:
        """The library-relative path this book is pinned to, if any.

        Two sources, newest first. `work_archive_paths` is where an explicit path
        is recorded now: it is keyed by candidate, so it exists before the first
        pack and survives a work's jobs being removed. `artifacts
        .library_relative_path` is what renames written before migration 015
        recorded, and reading it as a fallback is what keeps those working
        without a data migration that would have to guess.

        Read as a relative path and re-joined onto the *current* library root
        rather than stored absolute, so moving the library directory carries a
        renamed book with it. Validated before use even though this process
        wrote it: a path read back out of the database and joined onto a root is
        exactly the shape that must not be trusted twice -- an absolute value or
        a `..` would escape the library, so it is ignored and the template
        renders the path instead.
        """
        with self._database._connect() as connection:  # noqa: SLF001
            row = connection.execute(
                "SELECT relative_path FROM work_archive_paths "
                "WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            if row is None or not row[0]:
                row = connection.execute(
                    "SELECT artifacts.library_relative_path FROM artifacts "
                    "JOIN download_jobs ON download_jobs.id = artifacts.job_id "
                    "WHERE download_jobs.candidate_id = ? "
                    "AND artifacts.artifact_type = 'CBZ' "
                    "AND artifacts.library_relative_path IS NOT NULL "
                    "ORDER BY artifacts.id DESC LIMIT 1",
                    (candidate_id,),
                ).fetchone()
        if row is None or not row[0]:
            return None
        relative = PurePosixPath(str(row[0]).replace("\\", "/"))
        if relative.is_absolute() or ".." in relative.parts:
            logging.getLogger(__name__).warning(
                "library_relative_path_refused",
                extra={"error_code": "PATH_OUTSIDE_ROOT"},
            )
            return None
        return relative

    def _existing_cbz_paths_sync(self, candidate_id: int) -> tuple[str, ...]:
        """Paths already recorded as this book's own CBZ.

        Re-packing must land on the file it replaces. Without this, the conflict
        suffix would treat the previous CBZ as somebody else's book and every
        重新打包 would leave `book.cbz`, `book (2).cbz`, `book (3).cbz` behind.
        """
        with self._database._connect() as connection:  # noqa: SLF001
            rows = connection.execute(
                "SELECT artifacts.path FROM artifacts "
                "JOIN download_jobs ON download_jobs.id = artifacts.job_id "
                "WHERE download_jobs.candidate_id = ? "
                "AND artifacts.artifact_type = 'CBZ'",
                (candidate_id,),
            ).fetchall()
        return tuple(str(row[0]) for row in rows if row[0])

    async def enqueue_for_candidate(self, candidate_id: int) -> int:
        return await asyncio.to_thread(
            self._enqueue_sync, candidate_id
        )

    def _enqueue_sync(self, candidate_id: int) -> int:
        with self._database._connect() as connection:  # noqa: SLF001
            candidate_row = connection.execute(
                "SELECT status FROM candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()
            if candidate_row is None:
                raise ConversionError(
                    "CANDIDATE_NOT_FOUND",
                    "Candidate does not exist",
                )
            if str(candidate_row[0]) not in {STATUS_APPROVED, "DOWNLOADED"}:
                raise ConversionError(
                    "CANDIDATE_NOT_READY",
                    "Only approved candidates can be converted",
                )
            artifact_row = connection.execute(
                "SELECT a.path FROM download_jobs dj "
                "JOIN artifacts a ON a.job_id = dj.id "
                "WHERE dj.candidate_id = ? AND dj.state = ? "
                "AND a.artifact_type = 'ARCHIVE' "
                "ORDER BY dj.id DESC LIMIT 1",
                (candidate_id, DOWNLOAD_STATE_COMPLETED),
            ).fetchone()
            if artifact_row is None:
                raise ConversionError(
                    "ARCHIVE_NOT_READY",
                    "No downloaded archive available for conversion",
                )
            before = connection.total_changes
            connection.execute(
                "INSERT INTO download_jobs "
                "(candidate_id, idempotency_key, provider, state, "
                "details_json) VALUES (?, ?, ?, ?, '{}') "
                "ON CONFLICT(idempotency_key) DO NOTHING",
                (
                    candidate_id,
                    f"convert:{candidate_id}",
                    PROVIDER_CONVERSION,
                    CONVERSION_STATE_PENDING,
                ),
            )
            created = connection.total_changes > before
            if not created:
                # The task already exists, so requeueing it is an UPDATE. Every
                # state except RUNNING is requeueable, and each for its own
                # reason:
                #
                # * WAITING_VOLUMES / WAITING_PASSWORD / WAITING_PATH -- the
                #   operator has supplied what was missing, or fixed the path.
                # * FAILED -- a retry after fixing the cause.
                # * COMPLETED -- 重新打包. This one is the whole point of the
                #   action and was missing until 2026-08-28: `DO NOTHING` above
                #   left the row COMPLETED, the worker claims only PENDING, so
                #   the request 303'd back to a page reporting success while
                #   nothing had been re-packed and the CBZ on disk was
                #   untouched. Landing on the file it replaces is already
                #   handled -- `_existing_cbz_paths_sync` reserves this book's
                #   own path so the conflict suffix does not invent
                #   `book (2).cbz`.
                #
                # RUNNING is excluded because the worker holds that row; the
                # requeue would be overwritten by whatever it writes next.
                connection.execute(
                    "UPDATE download_jobs SET state = ?, error_code = NULL, "
                    "error_message = NULL, updated_at = CURRENT_TIMESTAMP "
                    "WHERE idempotency_key = ? AND state IN (?, ?, ?, ?, ?)",
                    (
                        CONVERSION_STATE_PENDING,
                        f"convert:{candidate_id}",
                        CONVERSION_STATE_WAITING_VOLUMES,
                        CONVERSION_STATE_WAITING_PASSWORD,
                        CONVERSION_STATE_WAITING_PATH,
                        CONVERSION_STATE_FAILED,
                        CONVERSION_STATE_COMPLETED,
                    ),
                )
            row = connection.execute(
                "SELECT id FROM download_jobs WHERE idempotency_key = ?",
                (f"convert:{candidate_id}",),
            ).fetchone()
            return int(row[0])

    async def start(self) -> None:
        if self._worker_task is not None:
            return
        self._worker_task = asyncio.create_task(
            self._run_worker(), name="conversion-worker"
        )

    async def stop(self) -> None:
        if self._worker_task is None:
            return
        self._worker_task.cancel()
        await asyncio.gather(
            self._worker_task, return_exceptions=True
        )
        self._worker_task = None

    async def _run_worker(self) -> None:
        while True:
            try:
                processed = await self._process_one()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logging.getLogger(__name__).exception(
                    "conversion_worker_error",
                    extra={"error_code": "CONVERSION_WORKER_ERROR"},
                )
                processed = False
            if not processed:
                await asyncio.sleep(1.0)

    async def _process_one(self) -> bool:
        job = await asyncio.to_thread(self._claim_pending_job_sync)
        if job is None:
            return False
        try:
            await self._handle_job(job)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            await asyncio.to_thread(
                self._mark_failed_sync, job["job_id"], "WORKER_EXCEPTION", str(exc)
            )
        # Announced once here rather than at each of the five terminal writes
        # inside `_handle_job`. Every exit -- packed, failed, waiting for a
        # volume, waiting for a password, worker crash -- has already committed
        # its row by the time control reaches this line, so one publish covers
        # all of them and a future branch cannot forget to notify.
        self._announce(job["job_id"], job["candidate_id"])
        return True

    def _announce(self, job_id: int, candidate_id: int) -> None:
        """Tell the interface a packaging job moved, if anything is listening.

        Never allowed to disturb the worker: the activity page falls back to
        polling, so a subscriber problem must not fail a CBZ that was written
        successfully.
        """
        if self._notify is None:
            return
        try:
            self._notify(job_id=job_id, candidate_id=candidate_id)
        except Exception:  # noqa: BLE001 - notification is best-effort
            logging.getLogger(__name__).warning(
                "conversion_notify_failed",
                extra={"error_code": "CONVERSION_NOTIFY_FAILED"},
            )

    def _claim_pending_job_sync(self) -> dict | None:
        with self._database._connect() as connection:
            row = connection.execute(
                "SELECT id, candidate_id FROM download_jobs "
                "WHERE state = ? AND provider = ? "
                # Same ordering as the download claim: a promoted packaging job
                # runs first, and within one priority the queue stays FIFO. The
                # two queues are separate but an operator adjusts priority the
                # same way in both, so they must honour it the same way.
                "ORDER BY priority, id LIMIT 1",
                (CONVERSION_STATE_PENDING, PROVIDER_CONVERSION),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE download_jobs SET state = ?, "
                "attempt_count = attempt_count + 1, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (CONVERSION_STATE_RUNNING, int(row[0])),
            )
            return {
                "job_id": int(row[0]),
                "candidate_id": int(row[1]),
            }

    async def _handle_job(self, job: dict) -> None:
        source_artifact = await asyncio.to_thread(
            self._fetch_source_artifact_sync, job["candidate_id"]
        )
        if source_artifact is None:
            await asyncio.to_thread(
                self._mark_failed_sync,
                job["job_id"],
                "ARCHIVE_NOT_READY",
                "Archive artifact no longer exists",
            )
            return
        source_path = Path(source_artifact["path"])
        if not source_path.exists():
            await asyncio.to_thread(
                self._mark_failed_sync,
                job["job_id"],
                "ARCHIVE_MISSING",
                "Archive file was removed before conversion",
            )
            return

        metadata = await asyncio.to_thread(
            self._fetch_metadata_sync, job["candidate_id"]
        )
        title = (
            _metadata_lookup(metadata, "Title")
            or f"Candidate {job['candidate_id']}"
        )
        library_path, work_path = await self._effective_paths()
        try:
            library_target = await self._library_target(
                job["candidate_id"], library_path, metadata, title
            )
        except LibraryPathError as exc:
            # Only a *pinned* path can raise here -- the template branch
            # sanitises. So this is a path an operator or a batch recorded that
            # this filesystem will not take, and the book is parked rather than
            # failed: nothing was attempted, the archive is intact, and the
            # remedy is an edit on the work detail page followed by a requeue.
            await asyncio.to_thread(
                self._mark_waiting_sync,
                job["job_id"],
                CONVERSION_STATE_WAITING_PATH,
                exc.code,
                exc.public_message,
                {},
            )
            return
        image_quality = await self._settings.image_quality()
        processor = await self._build_processor(image_quality)
        try:
            result = await asyncio.to_thread(
                processor.process,
                source_path,
                destination=library_target,
                work_directory=work_path / "conversion",
                comicinfo_builder=lambda page_count: self._build_comicinfo(
                    metadata, title, page_count, image_quality
                ),
                library_path=library_path,
            )
        except ArchiveVolumesMissing as exc:
            await asyncio.to_thread(
                self._mark_waiting_sync,
                job["job_id"],
                CONVERSION_STATE_WAITING_VOLUMES,
                exc.code,
                exc.public_message,
                {"missing_volumes": list(exc.missing)},
            )
            return
        except ArchivePasswordRequired as exc:
            await asyncio.to_thread(
                self._mark_waiting_sync,
                job["job_id"],
                CONVERSION_STATE_WAITING_PASSWORD,
                exc.code,
                exc.public_message,
                {},
            )
            return
        except (ArchiveError, ConversionError) as exc:
            await asyncio.to_thread(
                self._mark_failed_sync,
                job["job_id"],
                exc.code,
                exc.public_message,
            )
            return
        if result.password_id is not None:
            await self._settings.mark_password_success(result.password_id)
        await asyncio.to_thread(
            self._record_cbz_artifact_sync,
            job["job_id"],
            result.cbz_path,
            result.page_count,
            library_path,
        )
        await asyncio.to_thread(
            self._mark_completed_sync,
            job["job_id"],
            {
                **result.snapshot.as_dict(),
                "page_count": result.page_count,
                "volume_count": result.volume_count,
                "skipped_members": list(result.skipped_members),
                "password_entry_id": result.password_id,
                "image_quality": result.image_quality,
                "rewritten_pages": result.rewritten_pages,
            },
        )
        if not await self._settings.keep_original():
            await asyncio.to_thread(self._remove_original_sync, source_path)

    async def _build_processor(
        self, image_quality: str | None = None
    ) -> ArchiveProcessor:
        profiles = await self._settings.profiles(enabled_only=True)
        limits = await self._settings.limits()
        passwords = await self._settings.password_attempts()
        if image_quality is None:
            image_quality = await self._settings.image_quality()
        return ArchiveProcessor(
            profiles=profiles,
            limits=limits,
            passwords=passwords,
            tools_path=self._settings.tools_path,
            image_quality=image_quality,
        )

    @staticmethod
    def _build_comicinfo(
        metadata, title: str, page_count: int, image_quality: str | None = None
    ) -> bytes:
        rating_value = _metadata_lookup(metadata, "Rating")
        try:
            rating = float(rating_value) if rating_value else None
        except ValueError:
            rating = None
        return build_comicinfo_xml(
            title=title,
            artist=_metadata_lookup(metadata, "Artist"),
            language=_metadata_lookup(metadata, "Language"),
            category=_metadata_lookup(metadata, "Category"),
            tags=_metadata_tags(metadata),
            rating=rating,
            description=_metadata_lookup(metadata, "Description"),
            page_count=page_count,
            japanese_title=_metadata_lookup(metadata, "JapaneseTitle"),
            group=_metadata_lookup(metadata, "Group"),
            parody=_metadata_lookup(metadata, "Parody"),
            character=_metadata_lookup(metadata, "Character"),
            web=_metadata_lookup(metadata, "Web"),
            scan_information=_scan_information(metadata, image_quality),
        )

    @staticmethod
    def _remove_original_sync(source_path: Path) -> None:
        """Delete the original archive only after the CBZ record is committed."""
        try:
            source_path.unlink(missing_ok=True)
        except OSError:
            logging.getLogger(__name__).warning(
                "original_archive_removal_failed",
                extra={"error_code": "ARCHIVE_CLEANUP_FAILED"},
            )

    def _fetch_source_artifact_sync(
        self, candidate_id: int
    ) -> dict | None:
        with self._database._connect() as connection:
            row = connection.execute(
                "SELECT a.path FROM download_jobs dj "
                "JOIN artifacts a ON a.job_id = dj.id "
                "WHERE dj.candidate_id = ? AND dj.state = ? "
                "AND a.artifact_type = 'ARCHIVE' "
                "ORDER BY dj.id DESC LIMIT 1",
                (candidate_id, DOWNLOAD_STATE_COMPLETED),
            ).fetchone()
            return {"path": str(row[0])} if row else None

    def _fetch_metadata_sync(self, candidate_id: int) -> list:
        with self._database._connect() as connection:
            rows = connection.execute(
                "SELECT field_name, field_value FROM metadata_values "
                "WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchall()
        return [
            type("MetaRow", (), {"field_name": r[0], "field_value": r[1]})
            for r in rows
        ]

    def _record_cbz_artifact_sync(
        self,
        job_id: int,
        destination: Path,
        page_count: int,
        library_path: Path | None = None,
    ) -> None:
        """Record the packed CBZ the way the download path records an archive.

        `size_bytes` used to receive `page_count`, so every packed CBZ reported
        a size of a few dozen bytes and the column could not be compared
        against the archive row it was produced from. It now carries the real
        file size, and the digest is computed the same way — streamed in
        chunks, because a CBZ is arbitrarily large and must not be read into
        memory to be hashed.
        """
        sha256 = hashlib.sha256()
        with destination.open("rb") as handle:
            while True:
                chunk = handle.read(64 * 1024)
                if not chunk:
                    break
                sha256.update(chunk)
        # Recorded on *every* pack, not only after a rename. The column arrived
        # in 014 for the rename case alone, which left a freshly packed book with
        # no answer to 「相对于书库它在哪里」 -- so the archive-path form on the
        # detail page had nothing to prefill its 目录 field with, and an operator
        # editing only the filename would have submitted an empty directory and
        # moved the book to the library root. Deriving it here is also the only
        # place that can: `library_path` is the effective root for this job.
        relative: str | None = None
        if library_path is not None:
            try:
                relative = destination.resolve().relative_to(
                    library_path.resolve()
                ).as_posix()
            except (OSError, ValueError):
                # A destination outside the library is already impossible by the
                # time we get here, but a root that cannot be resolved must not
                # fail a pack that has otherwise succeeded.
                relative = None
        with self._database._connect() as connection:
            connection.execute(
                "INSERT INTO artifacts "
                "(job_id, artifact_type, path, sha256, size_bytes, "
                "page_count, library_relative_path) "
                "VALUES (?, 'CBZ', ?, ?, ?, ?, ?) "
                "ON CONFLICT(job_id, artifact_type) DO UPDATE SET "
                "path = excluded.path, sha256 = excluded.sha256, "
                "size_bytes = excluded.size_bytes, "
                "page_count = excluded.page_count, "
                "library_relative_path = excluded.library_relative_path",
                (
                    job_id,
                    str(destination),
                    sha256.hexdigest(),
                    destination.stat().st_size,
                    int(page_count),
                    relative,
                ),
            )

    def _mark_completed_sync(self, job_id: int, details: dict) -> None:
        with self._database._connect() as connection:
            connection.execute(
                "UPDATE download_jobs SET state = ?, error_code = NULL, "
                "error_message = NULL, details_json = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (
                    CONVERSION_STATE_COMPLETED,
                    json.dumps(details, ensure_ascii=False, separators=(",", ":")),
                    job_id,
                ),
            )

    def _mark_waiting_sync(
        self,
        job_id: int,
        state: str,
        code: str,
        message: str,
        details: dict,
    ) -> None:
        """Park a task in a recoverable state without losing its snapshot."""
        with self._database._connect() as connection:
            connection.execute(
                "UPDATE download_jobs SET state = ?, error_code = ?, "
                "error_message = ?, details_json = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (
                    state,
                    code,
                    message,
                    json.dumps(details, ensure_ascii=False, separators=(",", ":")),
                    job_id,
                ),
            )

    def _mark_failed_sync(
        self, job_id: int, code: str, message: str
    ) -> None:
        with self._database._connect() as connection:
            connection.execute(
                "UPDATE download_jobs SET state = ?, error_code = ?, "
                "error_message = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ?",
                (CONVERSION_STATE_FAILED, code, message, job_id),
            )


__all__ = [
    "ConversionError",
    "ConversionService",
    "CONVERSION_STATE_PENDING",
    "CONVERSION_STATE_RUNNING",
    "CONVERSION_STATE_COMPLETED",
    "CONVERSION_STATE_FAILED",
    "CONVERSION_STATE_WAITING_PASSWORD",
    "CONVERSION_STATE_WAITING_PATH",
    "CONVERSION_STATE_WAITING_VOLUMES",
    "RECOVERABLE_CONVERSION_STATES",
]
