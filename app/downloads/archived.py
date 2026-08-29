"""The 已下载内容 domain: remove, rename/relocate and re-download.

Restores the book-management surface cut on 2026-08-26 and reinstated on
2026-08-28 by operator instruction, per §1.3.1 of the requirements document.

Why this is its own service rather than more methods on `DownloadService`
------------------------------------------------------------------------
`DownloadService` owns the *queue*: it claims jobs, leases them, retries them
and writes their terminal states, and every method on it is about a task in
flight. The three actions here are about a work that has already finished --
they delete bookkeeping, move files inside the library and requeue a source --
and two of them touch the filesystem outside the work directory, which the
download worker never does. Keeping them apart is what lets the queue's
invariants stay readable, and it is why the worker cannot accidentally call
`remove_work`.

The rule every method here follows: **the database row and the bytes are two
separate decisions.** An operator removing forty history rows is doing
bookkeeping; one deleting forty CBZ files is destroying archived books. The
requirements document is explicit that the second is opt-in and off by default
(「已完成下载的任务移除时可选是否移除已下载文件，默认不移除」), so
`delete_files` defaults to False everywhere and no caller may make it implicit.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path, PurePosixPath

from app.conversion.naming import (
    MAX_RELATIVE_PATH_LENGTH,
    LibraryPathError,
    LibraryTemplateError,
    safe_library_name,
    strict_library_segment,
    unique_library_target,
)
from app.db.database import Database
from app.downloads.models import (
    CONVERSION_STATE_RUNNING,
    CONVERSION_STATE_WAITING_PATH,
    DOWNLOAD_STATE_PENDING,
    OPEN_DOWNLOAD_STATES,
    PROVIDER_CONVERSION,
    DownloadedWork,
)


logger = logging.getLogger(__name__)


class ArchivedWorkError(ValueError):
    """An action refused, with a message the operator is meant to read."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message


def _resolve_inside(root: Path, target: Path) -> Path:
    """Resolve `target` and prove it stays under `root`.

    Every path this service deletes or writes goes through here. The stored
    path is not trusted even though this service wrote it: the layout template
    is operator input, an ExHentai artist name reaches a path segment, and the
    directories themselves are configurable, so a row written under an older
    setting can name a file outside today's library. Deleting that file because
    a database row asked us to is the failure mode this prevents.

    `resolve()` before comparing, so a symlink inside the library cannot point
    the delete at something outside it.
    """
    resolved_root = root.resolve()
    resolved = target.resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ArchivedWorkError(
            "PATH_OUTSIDE_ROOT",
            f"路径不在允许的目录内，已拒绝操作：{target}",
        )
    return resolved


def _prune_empty_parents(path: Path, root: Path) -> None:
    """Remove directories the deleted file left behind, never past `root`.

    A layout template of `{category}/{artist}/{title}` means deleting an
    artist's only book leaves two empty directories, and a library that
    accumulates them stops being browsable. Stops at the first non-empty
    directory and never touches `root` itself.
    """
    resolved_root = root.resolve()
    current = path.parent.resolve()
    while current != resolved_root and resolved_root in current.parents:
        try:
            next(current.iterdir())
        except StopIteration:
            pass
        except OSError:
            return
        else:
            return
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


class ArchivedWorkService:
    """Actions on a work whose download has already finished."""

    def __init__(
        self,
        database: Database,
        library_path: Path,
        work_path: Path,
        *,
        settings_service=None,
        conversion_enqueue=None,
        notify=None,
    ) -> None:
        self._database = database
        self._library_path = library_path
        self._work_path = work_path
        self._settings = settings_service
        # Injected rather than imported so re-download can optionally chain into
        # packaging without this module importing the conversion service, and so
        # a test can observe the chaining without running a packer.
        self._conversion_enqueue = conversion_enqueue
        self._notify = notify

    async def _roots(self) -> tuple[Path, Path]:
        """The library and work directories as configured right now.

        Read per action for the reason the conversion service reads them per
        job: an operator who corrects a directory expects the next action to use
        it, not the next restart. These are also the roots every path is
        validated against, so reading a stale value would validate against the
        wrong tree.
        """
        if self._settings is None:
            return (self._library_path, self._work_path)
        library = await self._settings.library_path()
        work = await self._settings.work_path()
        return (library or self._library_path, work or self._work_path)

    async def _require_work(self, candidate_id: int) -> DownloadedWork:
        work = await self._database.downloaded_work(candidate_id)
        if work is None:
            raise ArchivedWorkError(
                "WORK_NOT_DOWNLOADED",
                "该作品没有已下载的档案，无法执行此操作",
            )
        return work

    # ---------------------------------------------------------------- removal

    async def remove_work(
        self,
        candidate_id: int,
        *,
        delete_files: bool = False,
        operator_name: str = "admin",
    ) -> dict:
        """Remove a downloaded work's records, and optionally its files.

        Two steps that must not be conflated, so the return value reports them
        separately: `removed_jobs` is bookkeeping, `deleted_files` is bytes. The
        requirements document makes file deletion opt-in and off by default, and
        the parameter keeps that shape -- a caller that forgets it deletes
        nothing rather than everything.

        A work with a task still in flight is refused rather than raced: the
        download worker or the packer holds that row and would write to it (or
        to the file) after we deleted it. Cancel first, then remove.
        """
        work = await self._require_work(candidate_id)
        if work.state in OPEN_DOWNLOAD_STATES:
            raise ArchivedWorkError(
                "WORK_STILL_RUNNING",
                "该作品仍有下载任务在进行，请先取消或等待完成再移除",
            )
        if work.pack_state == CONVERSION_STATE_RUNNING:
            raise ArchivedWorkError(
                "WORK_PACK_RUNNING",
                "该作品正在打包，请等待打包结束再移除",
            )
        library_root, work_root = await self._roots()

        deleted: list[str] = []
        failed: list[str] = []
        if delete_files:
            # The CBZ lives under the library; the source archive under the work
            # directory. Each is validated against its own root, so a path that
            # has drifted between the two is refused rather than deleted from
            # whichever tree happens to contain it.
            for raw, root in (
                (work.cbz_path, library_root),
                (work.archive_path, work_root),
            ):
                if not raw:
                    continue
                try:
                    target = _resolve_inside(root, Path(raw))
                except ArchivedWorkError:
                    # Refusing one file must not abandon the removal: the
                    # operator asked for the work to go, and a path we will not
                    # touch is reported rather than silently skipped.
                    failed.append(raw)
                    logger.warning(
                        "archived_work_path_refused",
                        extra={"error_code": "PATH_OUTSIDE_ROOT"},
                    )
                    continue
                try:
                    await asyncio.to_thread(target.unlink)
                except FileNotFoundError:
                    # Already gone is the desired end state, not an error.
                    pass
                except OSError:
                    failed.append(raw)
                    logger.warning(
                        "archived_work_delete_failed",
                        extra={"error_code": "FILE_DELETE_FAILED"},
                    )
                    continue
                deleted.append(raw)
                if root == library_root:
                    await asyncio.to_thread(
                        _prune_empty_parents, target, library_root
                    )

        removed_jobs = await asyncio.to_thread(
            self._remove_records_sync,
            work,
            bool(delete_files) and not failed,
            operator_name,
        )
        if delete_files and not failed:
            # The pin named a file that no longer exists, so keeping it would
            # make a later re-download publish to a path chosen for a book that
            # was deleted -- and hold that name against every other work in the
            # meantime. A records-only removal keeps the pin on purpose: the file
            # is still there, and the operator's decision about where it lives
            # has not changed.
            await self._database.clear_archive_path_pin(candidate_id)
        if self._notify is not None:
            self._notify(candidate_id)
        return {
            "candidate_id": candidate_id,
            "removed_jobs": removed_jobs,
            "deleted_files": tuple(deleted),
            "failed_files": tuple(failed),
        }

    def _remove_records_sync(
        self, work: DownloadedWork, deleted_files: bool, operator_name: str
    ) -> int:
        """Delete the job rows in one transaction, leaving an audit row.

        `artifacts` is deleted first because it holds a foreign key onto
        `download_jobs` and `PRAGMA foreign_keys` is ON for every connection --
        the other order fails rather than cascading.

        The candidate is deliberately left alone. It is the work's identity, it
        carries the metadata and the review history, and deleting it would
        orphan `review_actions` rows that point at it. Removing downloaded
        content means the download is gone, not that the book was never seen.
        """
        job_ids = [work.job_id]
        if work.pack_job_id is not None:
            job_ids.append(work.pack_job_id)
        placeholders = ", ".join("?" for _ in job_ids)
        with self._database._connect() as connection:  # noqa: SLF001
            connection.execute(
                f"DELETE FROM artifacts WHERE job_id IN ({placeholders})",
                tuple(job_ids),
            )
            cursor = connection.execute(
                f"DELETE FROM download_jobs WHERE id IN ({placeholders})",
                tuple(job_ids),
            )
            removed = int(cursor.rowcount or 0)
            connection.execute(
                "INSERT INTO removed_works "
                "(candidate_id, job_id, provider, title, archive_path, "
                " cbz_path, deleted_files, operator_name) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    work.candidate_id,
                    work.job_id,
                    work.provider,
                    work.title,
                    work.archive_path,
                    work.cbz_path,
                    1 if deleted_files else 0,
                    operator_name,
                ),
            )
        return removed

    # ----------------------------------------------------------- re-download

    async def redownload_work(
        self,
        candidate_id: int,
        *,
        repack: bool = False,
    ) -> dict:
        """Run the work's source again, replacing what is on disk.

        The original job row is reused and reset to PENDING rather than a new one
        inserted, for the reason `retry_job` reuses it: `idempotency_key` is
        UNIQUE per source, so a second row would need a different key and would
        split one book's attempt history across two entries. Resetting keeps
        `attempt_count` climbing, which is what makes a book that has been
        re-fetched five times visible as such.

        `retry_job` cannot serve this: it refuses a COMPLETED job on purpose,
        because inside the queue a completed download is done. Here the operator
        is explicitly asking to fetch it again.

        `repack` chains packaging afterwards, per the requirements document's
        「下载后替换掉旧文件并重新打包（可选是否重新打包）」. It is off by default
        because the download has to finish first -- the flag is recorded and the
        worker's existing auto-pack path is what acts on it.
        """
        work = await self._require_work(candidate_id)
        if work.state in OPEN_DOWNLOAD_STATES:
            raise ArchivedWorkError(
                "WORK_STILL_RUNNING",
                "该作品已有下载任务在进行，无需重新下载",
            )
        if work.pack_state == CONVERSION_STATE_RUNNING:
            raise ArchivedWorkError(
                "WORK_PACK_RUNNING",
                "该作品正在打包，请等待打包结束再重新下载",
            )
        await asyncio.to_thread(self._reset_job_sync, work.job_id)
        if repack and self._conversion_enqueue is not None:
            # Recorded now so the packaging task exists and the page can show it
            # queued behind the download. The conversion worker will not claim it
            # before the archive lands: it reads the ARCHIVE artifact, and the
            # download rewrites that row when it finishes.
            await self._conversion_enqueue(candidate_id)
        if self._notify is not None:
            self._notify(candidate_id)
        return {
            "candidate_id": candidate_id,
            "job_id": work.job_id,
            "provider": work.provider,
            "repack": bool(repack),
        }

    def _reset_job_sync(self, job_id: int) -> None:
        with self._database._connect() as connection:  # noqa: SLF001
            cursor = connection.execute(
                "UPDATE download_jobs SET state = ?, error_code = NULL, "
                "error_message = NULL, lease_owner = NULL, "
                "lease_expires_at = NULL, retry_at = NULL, "
                "attempt_count = attempt_count + 1, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (DOWNLOAD_STATE_PENDING, job_id),
            )
            if int(cursor.rowcount or 0) == 0:
                raise ArchivedWorkError("JOB_NOT_FOUND", "下载任务不存在")

    # ------------------------------------------------------------ relocation

    async def rename_work(
        self,
        candidate_id: int,
        *,
        filename: str | None = None,
        directory: str | None = None,
        operator_name: str = "admin",
    ) -> dict:
        """Move or rename this work's published CBZ inside the library.

        Both parts are sanitised as path segments, never joined raw:
        `safe_library_name` is the same function the packer uses, so a title an
        operator types here and one that arrives from ExHentai are cleaned by
        one rule. `directory` is validated segment by segment, which is what
        stops `..` and an absolute path from walking out of the library -- the
        settings page refuses those in a template for the same reason.

        The new location is recorded on the artifact as
        `library_relative_path`, and that is the point of the whole action: the
        next 重新打包 reads it and lands on the operator's path instead of
        re-deriving one from the layout template, which would silently undo the
        rename.
        """
        work = await self._require_work(candidate_id)
        if not work.cbz_path:
            raise ArchivedWorkError(
                "WORK_NOT_PACKAGED",
                "该作品还没有打包产物，请先打包再修改文件名或路径",
            )
        if work.pack_state == CONVERSION_STATE_RUNNING:
            raise ArchivedWorkError(
                "WORK_PACK_RUNNING",
                "该作品正在打包，请等待打包结束再修改路径",
            )
        library_root, _ = await self._roots()
        source = _resolve_inside(library_root, Path(work.cbz_path))

        stem = (filename or "").strip()
        if stem:
            if stem.lower().endswith(".cbz"):
                stem = stem[:-4]
            cleaned = safe_library_name(stem, fallback="")
            if not cleaned:
                raise ArchivedWorkError(
                    "FILENAME_INVALID",
                    "文件名在清理非法字符后为空，请换一个名称",
                )
        else:
            cleaned = source.stem

        target_dir = library_root
        if directory:
            for segment in str(directory).replace("\\", "/").split("/"):
                token = segment.strip()
                if not token or token == ".":
                    continue
                if token == "..":
                    raise ArchivedWorkError(
                        "DIRECTORY_INVALID",
                        "目录不能包含 ..，请使用库根目录下的相对路径",
                    )
                safe_segment = safe_library_name(token, fallback="")
                if not safe_segment:
                    raise ArchivedWorkError(
                        "DIRECTORY_INVALID",
                        f"目录片段「{token}」清理后为空，请换一个名称",
                    )
                target_dir = target_dir / safe_segment

        target = _resolve_inside(library_root, target_dir / f"{cleaned}.cbz")
        if target == source:
            # Not an error: the operator submitted the name it already has, and
            # refusing would read as the rename being broken.
            return {
                "candidate_id": candidate_id,
                "path": str(source),
                "moved": False,
            }
        try:
            resolved = unique_library_target(
                target, reserved=frozenset({str(source)})
            )
        except LibraryTemplateError as exc:
            raise ArchivedWorkError(exc.code, exc.public_message) from exc

        await asyncio.to_thread(resolved.parent.mkdir, parents=True, exist_ok=True)
        try:
            await asyncio.to_thread(source.replace, resolved)
        except OSError as exc:
            raise ArchivedWorkError(
                "FILE_MOVE_FAILED", f"移动文件失败：{exc}"
            ) from exc
        await asyncio.to_thread(_prune_empty_parents, source, library_root)

        relative = resolved.relative_to(library_root.resolve())
        await asyncio.to_thread(
            self._record_path_sync,
            work.pack_job_id,
            str(resolved),
            relative.as_posix(),
        )
        # Also pinned in `work_archive_paths`, so a rename made from the list
        # survives the artifact row and is the same fact the detail page's form
        # reads and writes. Without this the two surfaces would disagree about
        # where the book belongs the moment one of them was used.
        await self._database.set_archive_path_pin(
            candidate_id,
            relative.as_posix(),
            is_manual=True,
            operator_name=operator_name,
        )
        if self._notify is not None:
            self._notify(candidate_id)
        return {
            "candidate_id": candidate_id,
            "path": str(resolved),
            "relative_path": relative.as_posix(),
            "moved": True,
        }

    async def has_manual_path_pin(self, candidate_id: int) -> bool:
        """Whether this work's archive path was typed by an operator.

        The guard the batch re-file asks before recomputing anything, and the
        reason it is a named question rather than the batch reading a column: the
        policy is 「模板不得覆盖人工决定」, and a caller that fetched the row
        itself would be free to forget it. Absent pin counts as False -- there is
        no decision to protect.
        """
        pin = await self._database.archive_path_pin(candidate_id)
        return bool(pin and pin["is_manual"])

    async def pin_computed_path(
        self,
        candidate_id: int,
        relative_path: str,
        *,
        operator_name: str = "admin",
    ) -> None:
        """Record a path the layout template computed, never overwriting a typed one.

        `is_manual=False` is the whole content of this method: the upsert keeps
        whichever flag is higher, so a template-computed path cannot demote or
        replace one the operator set by hand.
        """
        await self._database.set_archive_path_pin(
            candidate_id,
            relative_path,
            is_manual=False,
            operator_name=operator_name,
        )

    async def park_for_invalid_path(
        self, candidate_id: int, code: str, message: str
    ) -> None:
        """File a work under 需干预 because its archive path is unusable.

        The packing row is created if it does not exist and then parked in
        `CONVERSION_WAITING_PATH` with the reason on it. The row is the point: a
        batch reports skips in a flash message that is gone on the next
        navigation, and 「这本书为什么没打包」 has to still be answerable
        afterwards. Parked rather than failed because nothing was attempted and
        the archive is intact -- the remedy is an edit plus a requeue, which is
        exactly what 待补分卷 and 待补密码 mean.

        A RUNNING row is left alone: the worker holds it and would overwrite
        whatever we wrote.
        """
        await asyncio.to_thread(
            self._park_for_invalid_path_sync, candidate_id, code, message
        )

    def _park_for_invalid_path_sync(
        self, candidate_id: int, code: str, message: str
    ) -> None:
        key = f"convert:{candidate_id}"
        with self._database._connect() as connection:  # noqa: SLF001
            connection.execute(
                "INSERT INTO download_jobs "
                "(candidate_id, idempotency_key, provider, state, "
                " error_code, error_message, details_json) "
                "VALUES (?, ?, ?, ?, ?, ?, '{}') "
                "ON CONFLICT(idempotency_key) DO UPDATE SET "
                "  state = excluded.state, "
                "  error_code = excluded.error_code, "
                "  error_message = excluded.error_message, "
                "  updated_at = CURRENT_TIMESTAMP "
                "WHERE download_jobs.state <> ?",
                (
                    candidate_id,
                    key,
                    PROVIDER_CONVERSION,
                    CONVERSION_STATE_WAITING_PATH,
                    code,
                    message,
                    CONVERSION_STATE_RUNNING,
                ),
            )

    # ------------------------------------------------------- explicit path

    async def set_archive_path(
        self,
        candidate_id: int,
        *,
        directory: str = "",
        filename: str = "",
        operator_name: str = "admin",
    ) -> dict:
        """Set where this work's CBZ belongs, moving the file if there is one.

        The difference from `rename_work`, which this supersedes for the detail
        page, is what it refuses. `rename_work` sanitises the operator's input
        and lets `unique_library_target` grow a ` (2)` suffix when the name is
        taken, both of which are right for a convenience rename off a list. This
        one is the operator stating where a book must live, so:

        * every segment is validated and **nothing is silently repaired** -- a
          name containing `?` is refused with the character named, because a
          path they did not type is not the path they asked for;
        * an occupied name is **refused, not suffixed**. A ` (2)` beside the
          book already there is how two books end up with names neither operator
          chose, and the requirements are explicit: 「需要检查名称是否已存在，已
          存在不允许进行调整」;
        * the directory is **created** when it does not exist, which is the other
          half of that instruction, and is done only after every check passes so
          a refused submission leaves no empty directories behind.

        Works before the first pack as readily as after it: the pin is a fact
        about the book, so it is recorded whether or not there is a file to move
        yet. That is what makes 「先设路径，再打包」 work, which is the whole point
        of asking for it on the detail page.
        """
        work = await self._require_work(candidate_id)
        if work.pack_state == CONVERSION_STATE_RUNNING:
            raise ArchivedWorkError(
                "WORK_PACK_RUNNING",
                "该作品正在打包，请等待打包结束再修改归档路径",
            )
        library_root, _ = await self._roots()

        relative = self._plan_explicit_path(work, directory, filename)
        current = work.archive_relative_path
        if current == relative.as_posix() and work.cbz_path:
            # The submitted path is the one it already has. Not an error:
            # refusing would read as the form being broken.
            return {
                "candidate_id": candidate_id,
                "relative_path": relative.as_posix(),
                "moved": False,
                "created_directory": False,
            }

        await self._require_path_free(candidate_id, relative, library_root, work)

        target = _resolve_inside(library_root, library_root / relative)
        created_directory = not target.parent.exists()
        # Created only now, after every refusal has had its chance: a rejected
        # submission must not leave a directory tree behind it.
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)

        moved = False
        if work.cbz_path:
            source = _resolve_inside(library_root, Path(work.cbz_path))
            if source != target and source.exists():
                try:
                    await asyncio.to_thread(source.replace, target)
                except OSError as exc:
                    raise ArchivedWorkError(
                        "FILE_MOVE_FAILED", f"移动文件失败：{exc}"
                    ) from exc
                await asyncio.to_thread(
                    _prune_empty_parents, source, library_root
                )
                moved = True
                await asyncio.to_thread(
                    self._record_path_sync,
                    work.pack_job_id,
                    str(target),
                    relative.as_posix(),
                )

        await self._database.set_archive_path_pin(
            candidate_id,
            relative.as_posix(),
            is_manual=True,
            operator_name=operator_name,
        )
        if self._notify is not None:
            self._notify(candidate_id)
        return {
            "candidate_id": candidate_id,
            "relative_path": relative.as_posix(),
            "moved": moved,
            "created_directory": created_directory,
        }

    @staticmethod
    def _plan_explicit_path(
        work: DownloadedWork, directory: str, filename: str
    ) -> PurePosixPath:
        """Validate the operator's directory and filename into one path.

        Every refusal here names what is wrong with which segment, because the
        operator is looking at the form and can fix it. `strict_library_segment`
        is what makes it a refusal rather than a repair.
        """
        stem = (filename or "").strip()
        if stem.lower().endswith(".cbz"):
            stem = stem[:-4]
        if not stem:
            # Falling back to the current name keeps 「只改目录」 a one-field
            # edit instead of making the operator retype the filename.
            # Normalised before parsing: `cbz_path` is an absolute path in the
            # host's own flavour, and on Windows `PurePosixPath` reads the whole
            # thing as one segment -- so the fallback stem arrived carrying
            # backslashes and was then refused as an illegal character. The pin
            # is already posix-relative; only the artifact path needs this.
            current = work.archive_relative_path or work.cbz_path
            stem = (
                PurePosixPath(str(current).replace("\\", "/")).stem
                if current
                else ""
            )
        if not stem:
            raise ArchivedWorkError(
                "FILENAME_REQUIRED", "请填写归档文件名"
            )

        segments: list[str] = []
        try:
            for raw in str(directory or "").replace("\\", "/").split("/"):
                token = raw.strip()
                if not token or token == ".":
                    continue
                segments.append(strict_library_segment(token))
            segments.append(strict_library_segment(stem))
        except LibraryPathError as exc:
            raise ArchivedWorkError(exc.code, exc.public_message) from exc

        relative = PurePosixPath(*segments[:-1], f"{segments[-1]}.cbz")
        if len(str(relative)) > MAX_RELATIVE_PATH_LENGTH:
            raise ArchivedWorkError(
                "PATH_TOO_LONG",
                f"归档路径长度 {len(str(relative))} 超过上限 "
                f"{MAX_RELATIVE_PATH_LENGTH}，请缩短目录或文件名",
            )
        return relative

    async def _require_path_free(
        self,
        candidate_id: int,
        relative: PurePosixPath,
        library_root: Path,
        work: DownloadedWork,
    ) -> None:
        """Refuse a path another book owns, or a file already sitting there.

        Two checks because there are two ways a name can be taken and they fail
        differently. Another work's *pin* is a claim on a path whose file may not
        exist yet -- letting a second book pin it would mean the two race at pack
        time and the loser is overwritten with no trace. An existing *file* with
        no pin is a book packed before anyone pinned anything; there is no row to
        find it by, so the filesystem is the only witness.
        """
        owner = await self._database.candidate_at_archive_path(
            relative.as_posix()
        )
        if owner is not None and owner != candidate_id:
            raise ArchivedWorkError(
                "PATH_TAKEN_BY_WORK",
                f"该路径已被作品 #{owner} 占用，请换一个名称",
            )
        target = _resolve_inside(library_root, library_root / relative)
        if not await asyncio.to_thread(target.exists):
            return
        # The book's own file is not a conflict: this is the path it already
        # occupies, and a repack has to land on the file it replaces.
        if work.cbz_path:
            try:
                if _resolve_inside(library_root, Path(work.cbz_path)) == target:
                    return
            except ArchivedWorkError:
                pass
        raise ArchivedWorkError(
            "PATH_TAKEN_ON_DISK",
            f"目标位置已有同名文件：{relative.as_posix()}，请换一个名称",
        )

    def _record_path_sync(
        self, pack_job_id: int | None, path: str, relative: str
    ) -> None:
        if pack_job_id is None:
            return
        with self._database._connect() as connection:  # noqa: SLF001
            connection.execute(
                "UPDATE artifacts SET path = ?, library_relative_path = ? "
                "WHERE job_id = ? AND artifact_type = 'CBZ'",
                (path, relative, pack_job_id),
            )


__all__ = [
    "ArchivedWorkError",
    "ArchivedWorkService",
]