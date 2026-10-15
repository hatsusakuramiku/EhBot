"""Recoverable CBZ replacement, using the existing execution identity barrier."""
from contextlib import closing
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
from uuid import uuid4

from app.downloads.execution import current_execution
from app.conversion.convert import ConversionError


def _inspect(path):
    """Hash outside the write transaction and retain a cheap race check."""
    if path is None:
        return None, None
    path = Path(path)
    try:
        before = path.stat()
        with path.open('rb') as handle:
            digest = hashlib.file_digest(handle, 'sha256').hexdigest()
        after = path.stat()
    except FileNotFoundError:
        return None, None
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise OSError('Publication file changed during verification')
    return digest, (after.st_ino, after.st_size, after.st_mtime_ns)


def _stamp(path):
    try:
        stat = Path(path).stat() if path else None
    except FileNotFoundError:
        return None
    return (stat.st_ino, stat.st_size, stat.st_mtime_ns) if stat else None


class Publications:
    def __init__(self, database):
        self._database = database

    def publish(self, *args):
        publication_id = self.prepare(*args)
        self._replace(publication_id)
        status = self.finish(publication_id)
        if status == 'RECOVERY_REQUIRED':
            raise ConversionError('PUBLICATION_RECOVERY_REQUIRED', '文件发布需要核实，已保留恢复副本')
        if status != 'COMMITTED':
            raise sqlite3.IntegrityError('QUEUE_STALE_EXECUTION')

    def prepare(self, job, staged, destination, page_count, library_path, details, source_path, keep_original):
        """Persist the preimage and replacement before changing the public file."""
        staged, destination = Path(staged), Path(destination)
        digest, stamp = _inspect(staged)
        if digest is None:
            raise FileNotFoundError(staged)
        old_digest, old_stamp = _inspect(destination)
        publication_id = uuid4().hex
        backup = staged.parent / (publication_id + '.previous') if old_digest else None
        if backup is not None:
            try:
                os.link(destination, backup)
            except OSError:
                shutil.copyfile(destination, backup)
            if _inspect(backup)[0] != old_digest:
                backup.unlink(missing_ok=True)
                raise OSError('Previous archive changed while preparing publication')
        try:
            with self._database.connection() as connection:
                connection.execute('BEGIN IMMEDIATE')
                current_execution.get().check(connection)
                if _stamp(destination) != old_stamp or _stamp(staged) != stamp:
                    raise OSError('Archive changed before publication was prepared')
                relative = destination.resolve().relative_to(Path(library_path).resolve()).as_posix()
                connection.execute(
                    'INSERT INTO queue_publications(publication_id, task_id, attempt_id, generation, job_id, '
                    'candidate_id, input_artifact_id, staged_path, destination, backup_path, old_sha256, '
                    'new_sha256, size_bytes, page_count, library_relative_path, details_json, source_path, keep_original) '
                    'VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (publication_id, job['task_id'], job['attempt_id'], job['generation'], job['job_id'],
                     job['candidate_id'], job.get('input_artifact_id'), str(staged), str(destination),
                     str(backup) if backup else None, old_digest, digest, stamp[1], page_count, relative,
                     json.dumps(details, separators=(',', ':')), str(source_path), int(keep_original)),
                )
        except BaseException:
            if backup is not None:
                backup.unlink(missing_ok=True)
            raise
        return publication_id

    def _read(self, publication_id):
        with closing(self._database._connect()) as connection:
            connection.row_factory = sqlite3.Row
            return dict(connection.execute('SELECT * FROM queue_publications WHERE publication_id = ?',
                                           (publication_id,)).fetchone())

    @staticmethod
    def _eligible(connection, row):
        execution = current_execution.get()
        if execution is not None and execution.attempt_id == row['attempt_id'] and execution.stop_requested.is_set():
            return False
        return connection.execute(
            "SELECT 1 FROM queue_tasks t JOIN download_jobs j ON j.task_id = t.task_id "
            "WHERE t.task_id = ? AND t.current_attempt_id = ? AND t.generation = ? "
            "AND j.id = ? AND t.status = 'CONVERSION_RUNNING'",
            (row['task_id'], row['attempt_id'], row['generation'], row['job_id']),
        ).fetchone() is not None

    def _replace(self, publication_id):
        row = self._read(publication_id)
        staged_hash, staged_stamp = _inspect(row['staged_path'])
        destination_hash, destination_stamp = _inspect(row['destination'])
        # Diagnostic recovery may run after cancellation. It deliberately uses
        # a raw connection; only eligible producers can publish business data.
        with closing(self._database._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            if not self._eligible(connection, row):
                return
            if _stamp(row['staged_path']) != staged_stamp or _stamp(row['destination']) != destination_stamp:
                raise OSError('Archive changed during publication')
            if destination_hash == row['new_sha256']:
                return
            if staged_hash != row['new_sha256'] or destination_hash != row['old_sha256']:
                return
            Path(row['destination']).parent.mkdir(parents=True, exist_ok=True)
            Path(row['staged_path']).replace(row['destination'])

    def finish(self, publication_id):
        """Confirm the file, or restore the old version if cancellation won."""
        row = self._read(publication_id)
        if row['status'] != 'PREPARED':
            if row['status'] in {'COMMITTED', 'ROLLED_BACK'}:
                self._cleanup(row, row['status'])
            return row['status']
        destination_hash, destination_stamp = _inspect(row['destination'])
        backup_hash, backup_stamp = _inspect(row['backup_path'])
        with closing(self._database._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            # Multiple cleanup callers can arrive after the same worker exit.
            status = connection.execute('SELECT status FROM queue_publications WHERE publication_id = ?',
                                        (publication_id,)).fetchone()[0]
            if status != 'PREPARED':
                return status
            if _stamp(row['destination']) != destination_stamp or _stamp(row['backup_path']) != backup_stamp:
                raise OSError('Archive changed during recovery')
            if self._eligible(connection, row) and destination_hash == row['new_sha256']:
                connection.execute(
                    "INSERT INTO artifacts(job_id, artifact_type, path, sha256, size_bytes, page_count, library_relative_path) "
                    "VALUES(?, 'CBZ', ?, ?, ?, ?, ?) ON CONFLICT(job_id, artifact_type) DO UPDATE SET "
                    "path = excluded.path, sha256 = excluded.sha256, size_bytes = excluded.size_bytes, "
                    "page_count = excluded.page_count, library_relative_path = excluded.library_relative_path",
                    (row['job_id'], row['destination'], row['new_sha256'], row['size_bytes'],
                     row['page_count'], row['library_relative_path']),
                )
                connection.execute("UPDATE download_jobs SET state = 'CONVERSION_COMPLETED', error_code = NULL, "
                    "error_message = NULL, details_json = json_patch(details_json, ?), updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (row['details_json'], row['job_id']))
                status = 'COMMITTED'
            elif destination_hash == row['old_sha256']:
                status = 'ROLLED_BACK'
            elif destination_hash == row['new_sha256'] and (row['old_sha256'] is None or backup_hash == row['old_sha256']):
                if row['old_sha256'] is None:
                    Path(row['destination']).unlink(missing_ok=True)
                else:
                    Path(row['backup_path']).replace(row['destination'])
                status = 'ROLLED_BACK'
            else:
                # Never overwrite an unrelated external change or discard the
                # only surviving copy when the old backup cannot be verified.
                status = 'RECOVERY_REQUIRED'
            connection.execute('UPDATE queue_publications SET status = ?, error_code = ? WHERE publication_id = ?',
                (status, 'PUBLICATION_RECOVERY_REQUIRED' if status == 'RECOVERY_REQUIRED' else None, publication_id))
            if status == 'RECOVERY_REQUIRED' and self._eligible(connection, row):
                connection.execute("UPDATE download_jobs SET state = 'CONVERSION_WAITING_PATH', "
                    "error_code = 'PUBLICATION_RECOVERY_REQUIRED', error_message = '文件发布需要核实，已保留现有文件和恢复副本' WHERE id = ?",
                    (row['job_id'],))
        if status in {'COMMITTED', 'ROLLED_BACK'}:
            self._cleanup(row, status)
        return status

    def _cleanup(self, row, status):
        try:
            if row['backup_path']:
                Path(row['backup_path']).unlink(missing_ok=True)
            Path(row['staged_path']).unlink(missing_ok=True)
            if status == 'COMMITTED' and not row['keep_original'] and not self._cleanup_original(row):
                return
            with closing(self._database._connect()) as connection, connection:
                connection.execute('UPDATE queue_publications SET cleanup_complete = 1 WHERE publication_id = ?',
                                   (row['publication_id'],))
        except (OSError, sqlite3.Error):
            logging.getLogger(__name__).warning('publication_cleanup_failed', extra={'error_code': 'ARCHIVE_CLEANUP_FAILED'})

    def _cleanup_original(self, row):
        with closing(self._database._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            in_use = connection.execute(
                "SELECT 1 FROM queue_tasks WHERE kind = 'PACK' AND task_id <> ? "
                "AND status NOT IN ('CONVERSION_COMPLETED', 'CONVERSION_FAILED', 'CANCELLED', 'REMOVED') "
                "AND json_extract(details_json, '$.input_artifact_id') = ?",
                (row['task_id'], row['input_artifact_id']),
            ).fetchone()
            if in_use is not None:
                return False
            if row['source_path'] and Path(row['source_path']).resolve() != Path(row['destination']).resolve():
                Path(row['source_path']).unlink(missing_ok=True)
        return True

    def recover(self, task_id=None, *, publication_id=None):
        """Resolve prepared records before reclaim or physical removal cleanup."""
        from app.conversion.relocation import Relocations
        if publication_id is None:
            Relocations(self._database).recover(task_id)
        with closing(self._database._connect()) as connection:
            ids = [row[0] for row in connection.execute(
                "SELECT publication_id FROM queue_publications WHERE (status IN ('PREPARED', 'RECOVERY_REQUIRED') "
                "OR (status IN ('COMMITTED', 'ROLLED_BACK') AND cleanup_complete = 0)) "
                'AND (? IS NULL OR task_id = ?) AND (? IS NULL OR publication_id = ?)',
                (task_id, task_id, publication_id, publication_id))]
        for publication_id in ids:
            row = self._read(publication_id)
            if row['status'] == 'RECOVERY_REQUIRED':
                digest, stamp = _inspect(row['destination'])
                backup, backup_stamp = _inspect(row['backup_path'])
                if digest != row['old_sha256'] and not (digest == row['new_sha256']
                        and (row['old_sha256'] is None or backup == row['old_sha256'])):
                    continue
                with closing(self._database._connect()) as connection, connection:
                    connection.execute('BEGIN IMMEDIATE')
                    if _stamp(row['destination']) != stamp or _stamp(row['backup_path']) != backup_stamp:
                        raise OSError('Archive changed during recovery')
                    connection.execute("UPDATE queue_publications SET status = 'PREPARED', error_code = NULL "
                        "WHERE publication_id = ? AND status = 'RECOVERY_REQUIRED'", (publication_id,))
                row = self._read(publication_id)
            if row['status'] == 'PREPARED':
                self._replace(publication_id)
            self.finish(publication_id)
        if task_id is not None:
            with closing(self._database._connect()) as connection:
                if connection.execute("SELECT 1 FROM queue_publications WHERE task_id = ? "
                                      "AND status = 'RECOVERY_REQUIRED'", (task_id,)).fetchone():
                    raise ConversionError('PUBLICATION_RECOVERY_REQUIRED', '文件发布尚待核实，不能清理恢复副本')
        return len(ids)

    def retry_required(self, candidate_id):
        from app.conversion.relocation import Relocations
        moves = Relocations(self._database)
        moves.recover(required_only=True)
        moves.check(candidate_id=candidate_id)
        with closing(self._database._connect()) as connection:
            tasks = [row[0] for row in connection.execute("SELECT DISTINCT task_id FROM queue_publications "
                "WHERE candidate_id = ? AND status = 'RECOVERY_REQUIRED'", (candidate_id,))]
        for task_id in tasks:
            self.recover(task_id)
