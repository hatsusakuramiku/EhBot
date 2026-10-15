"""Recover a filesystem rename whose database transaction never committed."""
from contextlib import closing
from pathlib import Path
import sqlite3

from app.conversion.convert import ConversionError
from app.conversion.publication import _inspect, _stamp


class Relocations:
    def __init__(self, database):
        self._database = database

    def recover(self, task_id=None, *, required_only=False, relocation_id=None):
        """Restore uncommitted moves after startup or the owning worker exits.

        required_only=True examines only previously blocked records, so an
        ordinary enqueue cannot reverse an in-flight manual rename. False is
        for startup and physical-stop recovery, after the owner has exited.
        """
        with closing(self._database._connect()) as connection:
            connection.row_factory = sqlite3.Row
            rows = [dict(row) for row in connection.execute(
                "SELECT * FROM queue_relocations WHERE status IN ('PREPARED', 'RECOVERY_REQUIRED') "
                "AND (? IS NULL OR task_id = ?) AND (? = 0 OR status = 'RECOVERY_REQUIRED') "
                'AND (? IS NULL OR relocation_id = ?)',
                (task_id, task_id, int(required_only), relocation_id, relocation_id),
            )]
        for row in rows:
            source_hash, source_stamp = _inspect(row['source_path'])
            target_hash, target_stamp = _inspect(row['destination'])
            with closing(self._database._connect()) as connection, connection:
                connection.execute('BEGIN IMMEDIATE')
                current = connection.execute('SELECT status FROM queue_relocations WHERE relocation_id = ?',
                                             (row['relocation_id'],)).fetchone()[0]
                if current not in {'PREPARED', 'RECOVERY_REQUIRED'}:
                    continue
                if _stamp(row['source_path']) != source_stamp or _stamp(row['destination']) != target_stamp:
                    raise OSError('Archive changed during relocation recovery')
                if source_hash == row['sha256'] and target_hash is None:
                    status = 'ROLLED_BACK'
                elif source_hash is None and target_hash == row['sha256']:
                    Path(row['source_path']).parent.mkdir(parents=True, exist_ok=True)
                    Path(row['destination']).replace(row['source_path'])
                    status = 'ROLLED_BACK'
                else:
                    # Both paths, missing bytes, or unknown bytes need a human
                    # decision. Neither path is overwritten or deleted.
                    status = 'RECOVERY_REQUIRED'
                connection.execute('UPDATE queue_relocations SET status = ? WHERE relocation_id = ?',
                                   (status, row['relocation_id']))
        if task_id is not None:
            self.check(task_id=task_id)
        return len(rows)

    def check(self, *, candidate_id=None, task_id=None, connection=None):
        """Reject new mutations/cleanup while the old path is unresolved."""
        if connection is None:
            with closing(self._database._connect()) as connection:
                return self.check(candidate_id=candidate_id, task_id=task_id, connection=connection)
        if connection.execute(
            "SELECT 1 FROM queue_relocations WHERE status IN ('PREPARED', 'RECOVERY_REQUIRED') "
            'AND (? IS NULL OR candidate_id = ?) AND (? IS NULL OR task_id = ?)',
            (candidate_id, candidate_id, task_id, task_id),
        ).fetchone():
            raise ConversionError('PUBLICATION_RECOVERY_REQUIRED', '文件移动尚未结束或需要核实，请稍后重试')
