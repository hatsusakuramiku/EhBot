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
from pathlib import Path

from app.conversion.naming import (
    LibraryTemplateError,
    safe_library_name,
    unique_library_target,
)
from app.db.database import Database
from app.downloads.models import (
    CONVERSION_STATE_RUNNING,
    DOWNLOAD_STATE_PENDING,
    OPEN_DOWNLOAD_STATES,
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
        if self._notify is not None:
            self._notify(candidate_id)
        return {
            "candidate_id": candidate_id,
            "path": str(resolved),
            "relative_path": relative.as_posix(),
            "moved": True,
        }

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