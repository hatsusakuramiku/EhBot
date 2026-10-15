"""The 已下载内容 domain: remove, rename/relocate and re-download.

Restores the book-management surface cut on 2026-08-26 and reinstated on
2026-08-28 by operator instruction, per §1.3.1 of the requirements document.

Why this is its own service rather than more methods on `DownloadService`
------------------------------------------------------------------------
`DownloadService` owns the *queue*: it claims jobs, leases them, retries them
and writes their terminal states, and every method on it is about a task in
flight. These actions manage work records and published files --
they remove records, move files inside the library and requeue a source --
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
import hashlib
import json
import logging
import sqlite3
import zipfile
from contextlib import closing
from types import SimpleNamespace
from xml.etree import ElementTree as ET
from pathlib import Path, PurePosixPath
from uuid import uuid4

from app.conversion.naming import (
    CBZ_SUFFIX,
    LibraryLimits,
    LibraryPathError,
    LibraryTemplateError,
    detect_library_limits,
    file_name_max,
    safe_library_name,
    strict_library_segment,
    unique_library_target,
)
from app.archive.backends.zip_backend import rewrite_comicinfo
from app.conversion.comicinfo import (
    DERIVED_ELEMENTS, EDITABLE_ELEMENTS, build_from_metadata, field_writes_archive,
)
from app.conversion.service import _PACK_FORBIDDEN_STATUSES
from app.conversion.convert import ConversionError
from app.conversion.publication import _inspect, _stamp
from app.conversion.relocation import Relocations
from app.db.database import Database, METADATA_SOURCE_RANK_SQL
from app.downloads.queue import QueueControl, DownloadError
from app.downloads.execution import current_execution, run_sync
from app.downloads.models import (
    CONVERSION_STATE_RUNNING,
    CONVERSION_STATE_WAITING_PATH,
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


def _parse_relative_path(raw: str, limits: LibraryLimits) -> PurePosixPath:
    """Validate an already-rendered library path into one this service will move to.

    `limits` is passed in rather than looked up here: this function is pure, and
    the caller has already resolved the library root it is about to join onto.

    `strict_library_segment` rather than `safe_library_name`: the two callers are
    the re-archive sweep and this service's own `refile_work`, and both hand over
    a path that came out of `plan_library_path`, which refuses rather than
    repairs. Re-checking it is not redundant -- the pin is joined onto a library
    root that is itself a setting, so a path legal when it was rendered can be
    unusable by the time it is applied -- and it is what keeps a stored row from
    reaching `Path.replace` without a second look.

    Refused as `ArchivedWorkError`, which is the error type the operator-facing
    callers already translate into a message.
    """
    pieces = [
        token.strip()
        for token in str(raw or "").replace("\\", "/").split("/")
        if token.strip() and token.strip() != "."
    ]
    segments: list[str] = []
    try:
        for piece in pieces:
            segments.append(
                strict_library_segment(piece, name_max=limits.name_max)
            )
    except LibraryPathError as exc:
        raise ArchivedWorkError(exc.code, exc.public_message) from exc
    if not segments:
        raise ArchivedWorkError("PATH_REQUIRED", "归档路径不能为空")
    relative = PurePosixPath(*segments)
    encoded = len(str(relative).encode("utf-8"))
    if encoded > limits.relative_max:
        raise ArchivedWorkError(
            "PATH_TOO_LONG",
            f"归档路径占 {encoded} 字节，超过本机文件系统给这个书库留下的 "
            f"{limits.relative_max} 字节，请缩短目录或文件名",
        )
    return relative


class ArchivedWorkService:
    """Work records and files, including durable removal of running producers."""

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
        self._notify = notify
        self._removal_lock = asyncio.Lock()

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

    # ----------------------------------------------------- archive metadata

    @staticmethod
    def _metadata_rows(connection, candidate_id: int) -> list:
        """Read the same trust order used by packing and effective metadata."""
        rows = connection.execute(
            "SELECT mv.field_name, mv.field_value, mv.is_manual, mv.is_locked, mv.value_source FROM metadata_values mv "
            "WHERE mv.candidate_id = ? ORDER BY mv.is_manual DESC, "
            + METADATA_SOURCE_RANK_SQL + " DESC, mv.confidence DESC, mv.created_at DESC",
            (candidate_id,),
        ).fetchall()
        return [SimpleNamespace(field_name=r[0], field_value=r[1], is_manual=bool(r[2]),
                                is_locked=bool(r[3]), value_source=r[4])
                for r in rows]

    @staticmethod
    def _cbz_row(connection, candidate_id: int):
        return connection.execute(
            "SELECT a.id, a.path, a.page_count FROM artifacts a "
            "JOIN download_jobs dj ON dj.id = a.job_id "
            "WHERE dj.candidate_id = ? AND a.artifact_type = 'CBZ' "
            "ORDER BY dj.id DESC LIMIT 1", (candidate_id,),
        ).fetchone()

    async def archive_metadata(self, candidate_id: int, *, fetched_names=()) -> dict:
        """One editor snapshot: actual file values when packed, projected otherwise."""
        library, _ = await self._roots()
        return await asyncio.to_thread(self._archive_metadata_sync, candidate_id, library, fetched_names)

    def _archive_metadata_sync(self, candidate_id: int, library: Path, fetched_names=()) -> dict:
        from app.conversion.comicinfo import element_sources
        from app.review.models import field_label
        from app.api.status import metadata_source_view
        with self._database.connection() as connection:
            row = self._cbz_row(connection, candidate_id)
            metadata = self._metadata_rows(connection, candidate_id)
            audit = connection.execute(
                "SELECT created_at FROM review_actions WHERE candidate_id = ? "
                "AND action = 'EDIT_METADATA' "
                "AND json_extract(details_json, '$.archive_synced') = 1 "
                "ORDER BY id DESC LIMIT 1", (candidate_id,),
            ).fetchone()
        values = {entry.field_name: entry.field_value for entry in reversed(metadata)}
        expected = ET.fromstring(build_from_metadata(
            metadata, values.get("Title") or f"candidate-{candidate_id}",
            int(row[2] or 0) if row else 0,
        ))
        field_values = {child.tag: child.text or "" for child in expected}
        file_values = {}
        error = None
        if row:
            try:
                path = _resolve_inside(library, Path(row[1]))
                with zipfile.ZipFile(path) as archive:
                    root = ET.fromstring(archive.read("ComicInfo.xml"))
                    if root.tag != "ComicInfo":
                        raise ET.ParseError("Expected ComicInfo root")
                file_values = {child.tag: child.text or "" for child in root}
            except (OSError, zipfile.BadZipFile, KeyError, ET.ParseError, ArchivedWorkError):
                error = "无法读取归档 ComicInfo.xml，请检查文件后重试"
        names = list(file_values)
        names.extend(name for name in field_values if name not in names)
        # Offer missing mapped elements without inventing every optional custom
        # ComicInfo field; those are added through the custom-field form.
        names.extend(name for name in (
            "Title", "JapaneseTitle", "Series", "LocalizedSeries", "Publisher", "Writer", "Penciller",
            "CoverArtist", "Characters", "LanguageISO", "Genre", "Tags", "Rating",
            "Summary", "Web", "ScanInformation",
        ) if name not in names)
        best = {entry.field_name: entry for entry in reversed(metadata)}
        elements = []
        fetched = {}
        for name in names:
            dependencies = element_sources(name, values)
            sources = [best[key] for key in dependencies if key in best]
            protected = any(entry.is_manual or entry.is_locked for entry in sources)
            if name in EDITABLE_ELEMENTS and not protected and set(dependencies) & set(fetched_names):
                fetched[name] = field_values.get(name, "")
            source = sources[0] if sources else None
            elements.append({
                "name": name, "field_name": name, "field_label": field_label(name),
                "field_value": file_values.get(name, "") if row else field_values.get(name, ""),
                "file_value": file_values.get(name), "in_file": name in file_values,
                "editable": name in EDITABLE_ELEMENTS and not error, "derived": name in DERIVED_ELEMENTS,
                "source": metadata_source_view(source.value_source).to_payload() if source else None,
                "is_manual": any(entry.is_manual for entry in sources),
                "is_locked": any(entry.is_locked for entry in sources),
                "lock_field": source.field_name if source else name,
                "lock_available": bool(source) and name in EDITABLE_ELEMENTS and not error,
            })
        return {
            "packaged": row is not None, "path": str(row[1]) if row else None,
            "synced_at": str(audit[0]) if audit else None, "error": error,
            "elements": elements, "fetched_fields": fetched,
        }

    async def metadata_editor(self, candidate_id: int, *, fetched_names=()) -> dict:
        """Share archive fields and non-archive operator fields across both UIs."""
        from app.api.serializers import metadata_entry
        from app.review.models import split_metadata_entries
        snapshot = await self.archive_metadata(candidate_id, fetched_names=fetched_names)
        primary, _ = split_metadata_entries(await self._database.list_metadata(candidate_id))
        snapshot["fields"] = snapshot.pop("elements")
        for entry in primary:
            if field_writes_archive(entry.field_name):
                continue
            field = metadata_entry(entry)
            field.update(lock_field=entry.field_name, lock_available=field["editable"], archive_field=False)
            snapshot["fields"].append(field)
        return snapshot

    async def sync_archive_metadata(self, candidate_id: int, *, fields=None, fetched_fields=None, operator_name="admin") -> dict:
        """Update ComicInfo in place; skipped writes retain the database edit.

        Unpacked works, running packs and unapproved works are reported as
        skipped. A write reservation covers the state check, file replacement
        and artifact update, preventing a worker claim or concurrent sync from
        overtaking this operation. No image decoding or source archive is used.
        """
        library, _ = await self._roots()
        return await asyncio.to_thread(self._sync_archive_metadata_sync, candidate_id, library, fields, fetched_fields or {}, operator_name)

    def _sync_archive_metadata_sync(self, candidate_id: int, library: Path, fields=None, fetched_fields=None, operator_name="admin") -> dict:
        from app.conversion.comicinfo import changed_elements, element_sources
        with self._database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._cbz_row(connection, candidate_id)
            reason = None
            message = "元数据已保存，归档文件已同步"
            if row is None:
                reason, message = "WORK_NOT_PACKED", "元数据已保存，尚未打包，下次打包生效"
            elif connection.execute(
                "SELECT 1 FROM download_jobs WHERE candidate_id = ? AND state = ?",
                (candidate_id, CONVERSION_STATE_RUNNING),
            ).fetchone():
                reason, message = "WORK_PACK_RUNNING", "元数据已保存，正在打包，本次未同步归档"
            else:
                status = connection.execute(
                    "SELECT status FROM candidates WHERE id = ?", (candidate_id,),
                ).fetchone()
                if status is None or status[0] in _PACK_FORBIDDEN_STATUSES:
                    reason, message = "WORK_PACK_FORBIDDEN", "元数据已保存，当前审核状态不允许同步归档"
            if reason:
                return {"synced": False, "reason": reason, "message": message}
            try:
                path = _resolve_inside(library, Path(row[1]))
                with zipfile.ZipFile(path) as archive:
                    previous = archive.read("ComicInfo.xml")
                metadata = self._metadata_rows(connection, candidate_id)
                projected = ET.fromstring(build_from_metadata(metadata, f"candidate-{candidate_id}", int(row[2] or 0)))
                root = ET.fromstring(previous)
                if root.tag != "ComicInfo":
                    raise ET.ParseError("Expected ComicInfo root")
                # Callers supply only this operation's edits. Legacy service callers
                # default to manual overrides, never the whole database projection.
                names = fields if fields is not None else [entry.field_name for entry in metadata if entry.is_manual]
                updates = {name: projected.findtext(name, "") for name in changed_elements(names)}
                values = {entry.field_name: entry.field_value for entry in reversed(metadata)}
                protected = {entry.field_name for entry in metadata if entry.is_manual or entry.is_locked}
                updates.update({name: value for name, value in (fetched_fields or {}).items()
                                if name in EDITABLE_ELEMENTS and not set(element_sources(name, values)) & protected
                                and name not in updates})
                if not updates:
                    return {"synced": False, "reason": "NO_ARCHIVE_CHANGES", "message": "元数据已保存，没有需要写回归档的修改"}
                for name, value in updates.items():
                    child = root.find(name)
                    if child is None:
                        child = ET.SubElement(root, name)
                    child.text = value
                comicinfo = ET.tostring(root, encoding="utf-8", xml_declaration=True)
                rewrite_comicinfo(path, comicinfo)
                with path.open("rb") as handle:
                    digest = hashlib.file_digest(handle, "sha256").hexdigest()
            except (OSError, zipfile.BadZipFile, KeyError, ET.ParseError, ArchivedWorkError) as exc:
                logger.warning("archive_metadata_sync_failed: %s", exc)
                return {"synced": False, "reason": "ARCHIVE_METADATA_SYNC_FAILED",
                        "message": "元数据已保存到数据库，但归档同步失败，请检查文件后重试"}
            connection.execute(
                "UPDATE artifacts SET sha256 = ?, size_bytes = ? WHERE id = ?",
                (digest, path.stat().st_size, row[0]),
            )
            audit = None
            if names:
                placeholders = ",".join("?" for _ in names)
                audit = connection.execute(
                    "SELECT id, details_json FROM review_actions WHERE candidate_id = ? "
                    "AND action = 'EDIT_METADATA' AND json_extract(details_json, '$.field') IN ("
                    + placeholders + ") ORDER BY id DESC LIMIT 1", (candidate_id, *names),
                ).fetchone()
            if audit:
                details = json.loads(audit[1])
                details.update(archive_synced=True, path=str(path))
                connection.execute("UPDATE review_actions SET details_json = ? WHERE id = ?",
                                   (json.dumps(details, ensure_ascii=False), audit[0]))
            elif fetched_fields:
                # A fetched-only save has no manual edit to annotate. Record the
                # explicit file write without mislabelling an older manual edit.
                connection.execute(
                    "INSERT INTO review_actions (candidate_id, action, operator_name, details_json) "
                    "VALUES (?, 'EDIT_METADATA', ?, ?)",
                    (candidate_id, operator_name, json.dumps({"fetched_fields": sorted(updates),
                     "archive_synced": True, "path": str(path)}, ensure_ascii=False)),
                )
            return {"synced": True, "message": message, "path": str(path)}

    # ---------------------------------------------------------------- removal


    async def _delete_targets(self, targets) -> tuple[list[str], list[str]]:
        """Apply a persisted file manifest with the same managed-root checks."""
        allowed_roots = {root.resolve() for root in await self._roots()}
        deleted: list[str] = []
        failed: list[str] = []
        seen: set[str] = set()
        for raw, root in targets:
            root = Path(root)
            try:
                if root.resolve() not in allowed_roots:
                    raise ArchivedWorkError("PATH_OUTSIDE_ROOT", "清理目录已不在当前受管目录内")
                target = _resolve_inside(root, Path(raw))
            except ArchivedWorkError:
                # Refusing one file must not abandon the removal: the operator
                # asked for the work to go, and a path we will not touch is
                # reported rather than silently skipped.
                failed.append(raw)
                logger.warning(
                    "archived_work_path_refused",
                    extra={"error_code": "PATH_OUTSIDE_ROOT"},
                )
                continue
            if str(target) in seen:
                continue
            seen.add(str(target))
            try:
                await run_sync(self._unlink_unreferenced_sync, target, raw)
            except FileNotFoundError:
                # Already gone is the desired end state, not an error. It still
                # counts as deleted: the operator asked for it not to be there,
                # and the audit row should not claim the bytes might remain.
                deleted.append(raw)
                await run_sync(_prune_empty_parents, target, root)
                continue
            except OSError:
                failed.append(raw)
                logger.warning(
                    "archived_work_delete_failed",
                    extra={"error_code": "FILE_DELETE_FAILED"},
                )
                continue
            deleted.append(raw)
            await run_sync(_prune_empty_parents, target, root)
        return deleted, failed

    def _unlink_unreferenced_sync(self, target: Path, raw: str) -> None:
        """Serialize a short unlink with publishers and refuse a surviving owner."""
        with self._database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM artifacts WHERE path IN (?, ?)",
                                  (raw, str(target))).fetchone() is not None:
                raise OSError("File is still referenced by another artifact")
            target.unlink()

    async def remove_work(
        self, candidate_id: int, *, delete_files: bool = False, operator_name: str = "admin",
    ) -> dict:
        """Remove job records immediately; durable operations stop their producers.

        delete_files=False preserves existing files; True cleans the persisted
        manifest only after all local and remote executions have stopped.
        """
        return await self._remove(candidate_id, delete_files, operator_name, purge=False)

    async def purge_work(
        self, candidate_id: int, *, delete_files: bool = False, operator_name: str = "admin",
    ) -> dict:
        """Delete candidate and jobs while independent stop/cleanup records survive."""
        return await self._remove(candidate_id, delete_files, operator_name, purge=True)

    async def _remove(self, candidate_id, delete_files, operator_name, *, purge):
        await run_sync(Relocations(self._database).recover, required_only=True)
        library, work_root = await self._roots()
        work = await self._database.downloaded_work(candidate_id)
        result = await run_sync(self._begin_removal_sync, candidate_id, bool(delete_files),
                                operator_name, purge, library, work_root, work)
        # Completed/queued work needs no asynchronous stop. Keep its historical
        # immediate file-result contract, while running work returns its identity.
        await self.process_removals()
        with self._database.connection() as connection:
            row = connection.execute(
                "SELECT status, deleted_json, failed_json FROM queue_removals WHERE removal_id = ?",
                (result["removal_id"],),
            ).fetchone()
        result.update(cleanup_status=row[0], deleted_files=tuple(json.loads(row[1])),
                      failed_files=tuple(json.loads(row[2])))
        if self._notify is not None:
            self._notify(candidate_id)
        return result

    def _begin_removal_sync(self, candidate_id, delete_files, operator_name, purge,
                            library, work_root, work):
        with self._database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # Running refile uses the existing physical-stop barrier. A manual
            # rename has no worker, so wait for its short publication to end.
            if connection.execute("SELECT 1 FROM queue_relocations WHERE candidate_id = ? "
                                  "AND status IN ('PREPARED', 'RECOVERY_REQUIRED') AND task_id IS NULL",
                                  (candidate_id,)).fetchone():
                raise ArchivedWorkError("PUBLICATION_RECOVERY_REQUIRED", "文件移动尚未结束或需要核实，请稍后移除")
            candidate = connection.execute(
                "SELECT ex_gid, CASE WHEN magnet_url IS NOT NULL THEN torrent_hash END "
                "FROM candidates WHERE id = ?", (candidate_id,),
            ).fetchone()
            if candidate is None:
                raise ArchivedWorkError("CANDIDATE_NOT_FOUND", "该候选不存在或已被删除")
            jobs = connection.execute(
                "SELECT j.id, j.provider, j.state, j.details_json, j.task_id, "
                "t.current_attempt_id, t.generation FROM download_jobs j "
                "JOIN queue_tasks t ON t.task_id = j.task_id WHERE j.candidate_id = ? ORDER BY j.id",
                (candidate_id,),
            ).fetchall()
            if not purge and not jobs:
                raise ArchivedWorkError("WORK_NOT_DOWNLOADED", "该作品没有下载任务，无法移除")
            targets = [
                [str(row[1]), str(library if row[0] == "CBZ" else work_root)]
                for row in connection.execute(
                    "SELECT a.artifact_type, a.path FROM artifacts a JOIN download_jobs j "
                    "ON j.id = a.job_id WHERE j.candidate_id = ?", (candidate_id,),
                )
            ]
            pin = connection.execute(
                "SELECT relative_path FROM work_archive_paths WHERE candidate_id = ?", (candidate_id,),
            ).fetchone()
            if pin is not None and pin[0]:
                targets.append([str(library / pin[0]), str(library)])
            removal_id = uuid4().hex
            directories = []
            for job in jobs:
                for row in connection.execute("SELECT directory FROM queue_attempt_paths WHERE task_id = ?", (job[4],)):
                    directory = Path(row[0])
                    root = library if directory.resolve().is_relative_to(library.resolve()) else work_root
                    directories.append([str(directory), str(root)])
            connection.execute(
                "INSERT INTO queue_removals(removal_id, candidate_id, ex_gid, torrent_hash, "
                "delete_files, purge_candidate, targets_json, directories_json, task_ids_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (removal_id, candidate_id, candidate[0], candidate[1], int(delete_files), int(purge),
                 json.dumps(targets), json.dumps(directories), json.dumps([row[4] for row in jobs])),
            )
            for job_id, provider, state, details, task_id, attempt_id, generation in jobs:
                previous = connection.execute(
                    "SELECT operation_id, action FROM queue_operations WHERE task_id = ? "
                    "AND status NOT IN ('COMPLETED', 'FAILED')", (task_id,),
                ).fetchone()
                params = {"removal_id": removal_id}
                if previous is not None:
                    params["superseded_action"] = previous[1]
                    connection.execute(
                        "UPDATE queue_operations SET action = 'remove', params_json = ?, "
                        "updated_at = CURRENT_TIMESTAMP WHERE operation_id = ?",
                        (json.dumps(params), previous[0]),
                    )
                else:
                    # A completed torrent may still seed. Even a legacy handle
                    # must be reconciled instead of interpreting FAILED as stopped.
                    remote = provider == "EH_TORRENT" and (
                        details not in (None, "", "{}") or connection.execute(
                            "SELECT 1 FROM queue_remote_resources WHERE task_id = ? "
                            "AND status <> 'STOP_CONFIRMED'", (task_id,),
                        ).fetchone() is not None
                    )
                    running = state in {"DOWNLOADING", "WAITING_TORRENT", "CONVERSION_RUNNING", "STOPPING"}
                    connection.execute(
                        "INSERT INTO queue_operations(operation_id, task_id, attempt_id, generation, "
                        "legacy_job_id, candidate_id, action, params_json, resource_json, status) "
                        "VALUES (?, ?, ?, ?, ?, ?, 'remove', ?, ?, ?)",
                        (uuid4().hex, task_id, attempt_id, generation, job_id, candidate_id,
                         json.dumps(params), details or "{}",
                         "PENDING" if running or remote else "COMPLETED"),
                    )
            audit_id = None
            if jobs:
                identity = next((row for row in reversed(jobs) if row[1] != PROVIDER_CONVERSION), jobs[-1])
                audit_id = connection.execute(
                    "INSERT INTO removed_works(candidate_id, job_id, provider, title, archive_path, "
                    "cbz_path, deleted_files, operator_name) VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
                    (candidate_id, work.job_id if work else identity[0],
                     work.provider if work else identity[1], work.title if work else None,
                     work.archive_path if work else None, work.cbz_path if work else None, operator_name),
                ).lastrowid
                connection.execute("UPDATE queue_removals SET audit_id = ? WHERE removal_id = ?",
                                   (audit_id, removal_id))
            connection.execute(
                "DELETE FROM artifacts WHERE job_id IN (SELECT id FROM download_jobs WHERE candidate_id = ?)",
                (candidate_id,),
            )
            connection.execute("DELETE FROM download_jobs WHERE candidate_id = ?", (candidate_id,))
            if purge:
                connection.execute("DELETE FROM review_actions WHERE candidate_id = ?", (candidate_id,))
                connection.execute("DELETE FROM candidates WHERE id = ?", (candidate_id,))
            return {"candidate_id": candidate_id, "removed_jobs": len(jobs), "removal_id": removal_id,
                    "task_ids": [row[4] for row in jobs],
                    "operation_ids": [row[0] for row in connection.execute(
                        "SELECT operation_id FROM queue_operations WHERE json_extract(params_json, '$.removal_id') = ?",
                        (removal_id,))],
                    **({"removed_candidate": 1} if purge else {})}

    async def process_removals(self) -> int:
        """Resume persisted manifests only after every producer stop is confirmed."""
        async with self._removal_lock:
            with self._database.connection() as connection:
                rows = connection.execute(
                    "SELECT removal_id, candidate_id, delete_files, purge_candidate, targets_json, directories_json, audit_id FROM queue_removals r "
                    "WHERE status = 'PENDING' AND NOT EXISTS(SELECT 1 FROM queue_operations o "
                    "WHERE json_extract(o.params_json, '$.removal_id') = r.removal_id "
                    "AND o.status <> 'COMPLETED') ORDER BY created_at, removal_id",
                ).fetchall()
            for removal_id, candidate_id, delete_files, purge_candidate, targets_json, directories_json, audit_id in rows:
                deleted, failed = ([], [])
                if delete_files:
                    deleted, failed = await self._delete_targets(json.loads(targets_json))
                retained = set() if delete_files else {Path(raw).resolve() for raw, _ in json.loads(targets_json)}
                leftovers, refused = await self._delete_directories(json.loads(directories_json), retained)
                deleted = list(dict.fromkeys(deleted + leftovers))
                failed = list(dict.fromkeys(failed + refused))
                with self._database.connection() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        "UPDATE queue_removals SET status = ?, deleted_json = ?, failed_json = ?, "
                        "updated_at = CURRENT_TIMESTAMP WHERE removal_id = ?",
                        ("PENDING" if failed else "COMPLETED", json.dumps(deleted), json.dumps(failed), removal_id),
                    )
                    if delete_files and not failed:
                        connection.execute("UPDATE removed_works SET deleted_files = 1 WHERE id = ?", (audit_id,))
                        # A re-added candidate may have a new pin: only clear the
                        # captured path, never its replacement.
                        for raw, root in ([] if purge_candidate else json.loads(targets_json)):
                            connection.execute(
                                "DELETE FROM work_archive_paths WHERE candidate_id = ? "
                                "AND relative_path = ?",
                                (candidate_id, str(Path(raw).relative_to(root)).replace("\\", "/")
                                 if Path(raw).is_relative_to(root) else ""),
                            )
                if self._notify is not None:
                    self._notify(candidate_id)
            return len(rows)

    async def _delete_directories(self, directories, retained: set[Path]) -> tuple[list[str], list[str]]:
        """Clean only registered attempt trees, never the shared work/library root."""
        deleted, failed = [], []
        allowed_roots = {root.resolve() for root in await self._roots()}
        for raw, root_raw in directories:
            directory, root = Path(raw), Path(root_raw)
            try:
                if root.resolve() not in allowed_roots or directory.is_symlink() or directory.is_junction():
                    raise OSError("Unmanaged or redirected attempt directory")
                target = _resolve_inside(root, directory)
                if target == root.resolve():
                    raise OSError("Attempt directory cannot be a managed root")
                if not target.exists():
                    continue
                children = await run_sync(lambda: list(target.rglob("*")))
                files = []
                for child in children:
                    if child.is_symlink() or child.is_junction():
                        failed.append(str(child))
                    elif child.is_file() and child.resolve() not in retained:
                        files.append([str(child), str(root)])
                gone, refused = await self._delete_targets(files)
                deleted.extend(gone)
                failed.extend(refused)
                for child in sorted(children, key=lambda path: len(path.parts), reverse=True):
                    if (child.is_dir() and not child.is_symlink() and not child.is_junction()
                            and not any(path.is_relative_to(child) for path in retained)):
                        await run_sync(child.rmdir)
                if target.exists() and not any(path.is_relative_to(target) for path in retained):
                    await run_sync(target.rmdir)
            except (OSError, ArchivedWorkError):
                failed.append(raw)
        return deleted, failed

    async def get_removal(self, removal_id: str) -> dict:
        """Read stop and file-cleanup facts independently of removed business rows."""
        return await run_sync(self._get_removal_sync, removal_id)

    def _get_removal_sync(self, removal_id: str) -> dict:
        with self._database.connection() as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT removal_id, candidate_id, status, delete_files, task_ids_json, deleted_json, "
                "failed_json, created_at, updated_at FROM queue_removals WHERE removal_id = ?", (removal_id,),
            ).fetchone()
            if row is None:
                raise ArchivedWorkError("REMOVAL_NOT_FOUND", "移除操作不存在")
            result = dict(row)
            for raw, public in (("task_ids_json", "task_ids"), ("deleted_json", "deleted_files"),
                                ("failed_json", "failed_files")):
                result[public] = json.loads(result.pop(raw))
            result["delete_files"] = bool(result["delete_files"])
            result["operations"] = [dict(row) for row in connection.execute(
                "SELECT operation_id, task_id, action, status, error_code FROM queue_operations "
                "WHERE json_extract(params_json, '$.removal_id') = ? ORDER BY operation_id", (removal_id,),
            )]
            return result

    # ----------------------------------------------------------- re-download

    async def redownload_work(
        self,
        candidate_id: int,
        *,
        repack: bool = False,
    ) -> dict:
        """Run the work's source again, replacing what is on disk.

        The provider projection keeps its integer id for compatibility. The
        queue core creates a fresh durable task when a terminal job restarts;
        each actual worker claim records its own attempt. Requeueing alone does
        not count as a download attempt. Saved remote handles require cleanup.

        `retry_job` cannot serve this: it refuses a COMPLETED job on purpose,
        because inside the queue a completed download is done. Here the operator
        is explicitly asking to fetch it again.

        `repack` chains packaging afterwards, per the requirements document's
        「下载后替换掉旧文件并重新打包（可选是否重新打包）」. It is off by default
        because the download has to finish first. The restart and explicit pack
        intent commit together; the conversion worker binds the newly committed
        artifact even when the global auto-pack setting is off.
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
        await asyncio.to_thread(self._reset_job_sync, work.job_id, repack)
        if self._notify is not None:
            self._notify(candidate_id)
        return {
            "candidate_id": candidate_id,
            "job_id": work.job_id,
            "provider": work.provider,
            "repack": bool(repack),
        }

    def _reset_job_sync(self, job_id: int, repack: bool = False) -> None:
        try:
            QueueControl(self._database).internal_command(job_id, "restart", pack_requested=repack)
        except DownloadError as exc:
            raise ArchivedWorkError(exc.code, exc.public_message) from exc

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
        await run_sync(self._reconcile_moves_sync, candidate_id)
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
        limits = detect_library_limits(library_root)
        source = _resolve_inside(library_root, Path(work.cbz_path))

        stem = (filename or "").strip()
        if stem:
            if stem.lower().endswith(CBZ_SUFFIX):
                stem = stem[: -len(CBZ_SUFFIX)]
            cleaned = safe_library_name(
                stem, fallback="", name_max=file_name_max(limits)
            )
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
                safe_segment = safe_library_name(
                    token, fallback="", name_max=limits.name_max
                )
                if not safe_segment:
                    raise ArchivedWorkError(
                        "DIRECTORY_INVALID",
                        f"目录片段「{token}」清理后为空，请换一个名称",
                    )
                target_dir = target_dir / safe_segment

        target = _resolve_inside(library_root, target_dir / f"{cleaned}{CBZ_SUFFIX}")
        if target == source:
            # Not an error: the operator submitted the name it already has, and
            # refusing would read as the rename being broken.
            return {
                "candidate_id": candidate_id,
                "path": str(source),
                "relative_path": source.relative_to(
                    library_root.resolve()
                ).as_posix(),
                "moved": False,
            }
        return await self._relocate(
            work,
            source,
            target,
            library_root=library_root,
            is_manual=True,
            override_manual=False,
            operator_name=operator_name,
        )

    def _reconcile_moves_sync(self, candidate_id):
        moves = Relocations(self._database)
        moves.recover(required_only=True)
        try:
            moves.check(candidate_id=candidate_id)
        except ConversionError as exc:
            raise ArchivedWorkError(exc.code, exc.public_message) from exc

    async def _relocate(
        self,
        work: DownloadedWork,
        source: Path,
        target: Path,
        *,
        library_root: Path,
        is_manual: bool,
        override_manual: bool,
        operator_name: str,
    ) -> dict:
        """Move one published CBZ and record where it went, once, for both callers.

        The half `rename_work` and `refile_work` share. They differ in what they
        accept and in whether the result is recorded as an operator's decision,
        so each validates its own input and hands the move here -- one place
        creates the directory, prunes what the move emptied, and writes both the
        artifact row and the pin. A second copy per caller is how the two would
        start disagreeing about what a completed move means.

        `override_manual` is the one thing that cannot be inferred from the rest:
        `refile_work` is reachable for a book whose path an operator typed only
        through 强制重新归档, and on that run the move *must* also replace the pin
        -- leaving it would point the next pack at the path the file was just
        moved off. Named at the call site rather than left to `is_manual`, so the
        single place this codebase demotes a decision is greppable.
        """
        result = await run_sync(
            self._relocate_sync, work, source, target, library_root,
            is_manual, override_manual, operator_name,
        )
        if self._notify is not None:
            self._notify(work.candidate_id)
        return result

    def _relocate_sync(
        self,
        work: DownloadedWork,
        source: Path,
        target: Path,
        library_root: Path,
        is_manual: bool,
        override_manual: bool,
        operator_name: str,
    ) -> dict:
        """Journal before rename; commit the path, pin and refile result together."""
        source = _resolve_inside(library_root, source)
        target = _resolve_inside(library_root, target)
        digest, stamp = _inspect(source)
        if digest is None:
            raise ArchivedWorkError("FILE_MOVE_FAILED", "原文件不存在，无法移动")
        relocation_id = uuid4().hex
        execution = current_execution.get()

        def validate(connection):
            if execution is not None:
                execution.check(connection)
                if execution.stop_requested.is_set():
                    raise sqlite3.IntegrityError("QUEUE_STALE_EXECUTION")
            row = connection.execute(
                "SELECT a.path, j.state, a.artifact_id FROM artifacts a JOIN download_jobs j ON j.id = a.job_id "
                "WHERE j.id = ? AND j.candidate_id = ? AND a.artifact_type = 'CBZ'",
                (work.pack_job_id, work.candidate_id),
            ).fetchone()
            if row is None or Path(row[0]).resolve() != source or _stamp(source) != stamp:
                raise ArchivedWorkError("WORK_PATH_CHANGED", "作品路径或文件已改变，请刷新后重试")
            if execution is None and row[1] in {CONVERSION_STATE_RUNNING, "STOPPING"}:
                raise ArchivedWorkError("WORK_PACK_RUNNING", "该作品正在执行或停止，请等待收尾后修改路径")
            if execution is not None:
                bound = connection.execute("SELECT json_extract(details_json, '$.input_artifact_id') "
                    "FROM queue_tasks WHERE task_id = ?", (execution.task_id,)).fetchone()
                if bound and bound[0] is not None and bound[0] != row[2]:
                    raise ArchivedWorkError("PACK_INPUT_CONFLICT", "入队时的 CBZ 版本已改变，请重新入队")
            return row[2]

        with self._database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                Relocations(self._database).check(candidate_id=work.candidate_id, connection=connection)
            except ConversionError as exc:
                raise ArchivedWorkError(exc.code, exc.public_message) from exc
            artifact_id = validate(connection)
            try:
                resolved = unique_library_target(target, reserved=frozenset({str(source)}))
            except LibraryTemplateError as exc:
                raise ArchivedWorkError(exc.code, exc.public_message) from exc
            relative = resolved.relative_to(library_root.resolve()).as_posix()
            if resolved != source:
                connection.execute("INSERT INTO queue_relocations(relocation_id, candidate_id, task_id, attempt_id, "
                    "input_artifact_id, source_path, destination, sha256) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    (relocation_id, work.candidate_id, execution.task_id if execution else None,
                     execution.attempt_id if execution else None, artifact_id, str(source), str(resolved), digest))
        moved = False
        try:
            with self._database.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if validate(connection) != artifact_id:
                    raise ArchivedWorkError("WORK_PATH_CHANGED", "作品版本已改变，请刷新后重试")
                try:
                    if resolved != source:
                        if resolved.exists():
                            raise ArchivedWorkError("WORK_PATH_CHANGED", "目标路径已被占用，请重试")
                        resolved.parent.mkdir(parents=True, exist_ok=True)
                        try:
                            source.replace(resolved)
                        except OSError as exc:
                            raise ArchivedWorkError("FILE_MOVE_FAILED", f"移动文件失败：{exc}") from exc
                        moved = True
                    connection.execute(
                        "UPDATE artifacts SET path = ?, library_relative_path = ? "
                        "WHERE job_id = ? AND artifact_type = 'CBZ'",
                        (str(resolved), relative, work.pack_job_id),
                    )
                    self._database.write_archive_path_pin(
                        connection, work.candidate_id, relative, is_manual, operator_name,
                        override_manual=override_manual,
                    )
                    connection.execute("UPDATE queue_relocations SET status = 'COMMITTED' WHERE relocation_id = ?",
                                       (relocation_id,))
                    if execution is not None:
                        connection.execute("UPDATE download_jobs SET state = 'CONVERSION_COMPLETED', error_code = NULL, "
                            "error_message = NULL, details_json = json_patch(details_json, ?) WHERE task_id = ?",
                            (json.dumps({'refiled': True, 'moved': moved, 'relative_path': relative}), execution.task_id))
                except BaseException:
                    # Restore on SQL failure before another writer can observe
                    # the rolled-back path and start removal/republication.
                    if moved and not source.exists() and _stamp(resolved) == stamp:
                        resolved.replace(source)
                        moved = False
                    raise
        except BaseException:
            if moved:
                Relocations(self._database).recover(relocation_id=relocation_id)
            elif resolved != source:
                # This live caller knows it either never renamed or restored
                # its rename. A newly occupied target belongs to someone else.
                with closing(self._database._connect()) as connection, connection:
                    connection.execute("UPDATE queue_relocations SET status = 'ROLLED_BACK' "
                        "WHERE relocation_id = ? AND status = 'PREPARED'", (relocation_id,))
            raise
        if moved:
            _prune_empty_parents(source, library_root)
        return {"candidate_id": work.candidate_id, "path": str(resolved),
                "relative_path": relative, "moved": moved, "committed": execution is not None}

    async def clear_path_pin(self, candidate_id: int) -> None:
        """Drop where this book was told to land, so the current mode decides.

        The one caller is a 强制 re-archive of a work that is not packed yet: the
        sweep's instruction is 「忽略手动指定的路径」, and a pin left in place
        would be read by the packing job before AI mode gets a turn -- the book
        would land on the typed path while the run reported itself as forced.
        Packed works do not need this: `refile_work` replaces the pin as part of
        the move. Named rather than inlined so the one place a manual decision is
        dropped stays greppable, beside `override_archive_path_pin`.
        """
        await self._database.clear_archive_path_pin(candidate_id)
        if self._notify is not None:
            self._notify(candidate_id)

    async def refile_work(
        self,
        candidate_id: int,
        relative_path: str,
        *,
        operator_name: str = "admin",
        allow_while_running: bool = False,
    ) -> dict:
        """Move a packed book to the path the current rules computed for it.

        The actor half of 一键重新归档, and deliberately not `rename_work`. The two
        take opposite stances on their input: `rename_work` repairs what an
        operator typed (`safe_library_name`) and records the result as
        `is_manual=True`, because an operator decided it; this one is handed a
        path that already went through `plan_library_path`, so it re-validates
        rather than repairs, and records `is_manual=False` -- the next template
        change has to be able to re-file the book again, which a manual pin
        forbids by design.

        That also means a manual pin is *overwritten* here. The guard against
        that lives in the caller: the sweep keeps typed paths out of its plan
        unless the operator asked for 强制, which is a request for exactly this.
        No other caller should reach this method with a path the operator typed.

        A packed work is moved, never repacked: the CBZ on disk is already this
        book's content, and the instruction is to 「更新路径与名称」 rather than
        spend the host's CPU re-deriving bytes that do not change.

        `allow_while_running` exists for exactly one caller: the conversion job
        that recomputes a path. That job *is* the row in `CONVERSION_RUNNING`, so
        the guard below -- which is there to stop a page action racing a pack in
        flight -- would refuse the very job that owns the row. Every other caller
        leaves it False.
        """
        await run_sync(self._reconcile_moves_sync, candidate_id)
        work = await self._require_work(candidate_id)
        if not work.cbz_path:
            raise ArchivedWorkError(
                "WORK_NOT_PACKAGED",
                "该作品还没有打包产物，请先打包再修改文件名或路径",
            )
        if work.pack_state == CONVERSION_STATE_RUNNING and not allow_while_running:
            raise ArchivedWorkError(
                "WORK_PACK_RUNNING",
                "该作品正在打包，请等待打包结束再修改路径",
            )
        library_root, _ = await self._roots()
        relative = _parse_relative_path(
            relative_path, detect_library_limits(library_root)
        )
        source = _resolve_inside(library_root, Path(work.cbz_path))
        target = _resolve_inside(library_root, library_root / relative)
        return await self._relocate(
            work,
            source,
            target,
            library_root=library_root,
            is_manual=False,
            override_manual=True,
            operator_name=operator_name,
        )

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
        with self._database.connection() as connection:
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

        relative = self._plan_explicit_path(
            work,
            directory,
            filename,
            detect_library_limits(library_root),
        )
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
        work: DownloadedWork,
        directory: str,
        filename: str,
        limits: LibraryLimits,
    ) -> PurePosixPath:
        """Validate the operator's directory and filename into one path.

        Every refusal here names what is wrong with which segment, because the
        operator is looking at the form and can fix it. `strict_library_segment`
        is what makes it a refusal rather than a repair.
        """
        stem = (filename or "").strip()
        if stem.lower().endswith(CBZ_SUFFIX):
            stem = stem[: -len(CBZ_SUFFIX)]
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
                segments.append(
                    strict_library_segment(token, name_max=limits.name_max)
                )
            segments.append(
                strict_library_segment(stem, name_max=file_name_max(limits))
            )
        except LibraryPathError as exc:
            raise ArchivedWorkError(exc.code, exc.public_message) from exc

        relative = PurePosixPath(*segments[:-1], f"{segments[-1]}{CBZ_SUFFIX}")
        encoded = len(str(relative).encode("utf-8"))
        if encoded > limits.relative_max:
            raise ArchivedWorkError(
                "PATH_TOO_LONG",
                f"归档路径占 {encoded} 字节，超过本机文件系统给这个书库留下的 "
                f"{limits.relative_max} 字节，请缩短目录或文件名",
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
        with self._database.connection() as connection:
            connection.execute(
                "UPDATE artifacts SET path = ?, library_relative_path = ? "
                "WHERE job_id = ? AND artifact_type = 'CBZ'",
                (path, relative, pack_job_id),
            )


__all__ = [
    "ArchivedWorkError",
    "ArchivedWorkService",
]
