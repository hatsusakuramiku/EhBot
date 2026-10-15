"""Actual 037 upgrade preserves remote occupancy and diagnoses interrupted reads."""
from pathlib import Path

import pytest

from app.downloads.worker import WorkerSession
from tests.integration.test_artifact_migration import schema036


@pytest.mark.asyncio
async def test_upgrade_037_retains_unknown_push_and_reconciles_read_operations(tmp_path):
    db = schema036(tmp_path)
    path = Path('app/db/migrations/037_artifact_pipeline.sql')
    with db.connection() as c:
        c.executescript('BEGIN IMMEDIATE;\n' + path.read_text(encoding='utf-8'))
        c.execute('INSERT INTO schema_migrations VALUES(37, CURRENT_TIMESTAMP)')
        c.commit()
        for work_id, state in ((1, 'COMPLETED'), (2, 'FAILED')):
            c.execute("INSERT INTO candidates(id, status) VALUES(?, 'APPROVED')", (work_id,))
            c.execute("INSERT INTO download_jobs(id, candidate_id, idempotency_key, provider, state) "
                "VALUES(?, ?, ?, 'EH_TORRENT', ?)", (work_id, work_id, f'fixture-{work_id}', state))
            task_id = c.execute('SELECT task_id FROM download_jobs WHERE id = ?', (work_id,)).fetchone()[0]
            c.execute('INSERT INTO queue_remote_resources(resource_id, task_id, attempt_id, legacy_job_id, instance_url, '
                "digest, ownership_tag, ownership, status) VALUES(?, ?, ?, ?, 'https://qb.invalid', ?, 'fixture', 'UNKNOWN', 'INTENDED')",
                (f'resource-{work_id}', task_id, f'legacy-attempt-{work_id}', work_id, str(work_id) * 40))
        c.execute("INSERT INTO eh_metadata_operations(operation_id, gid, candidates_json, status) VALUES('read', 123, '[]', 'RUNNING')")
        c.execute("INSERT INTO account_command_receipts(idempotency_key, payload_hash, response_json) "
            "VALUES('interrupted-test', 'fixture-hash', '{\"command_id\":\"read\",\"status\":\"PENDING\"}')")
    await db.initialize()
    with db.connection() as c:
        assert c.execute('SELECT download_finished FROM queue_remote_resources ORDER BY legacy_job_id').fetchall() == [(1,), (0,)]
        assert c.execute('SELECT recovery_count FROM download_jobs').fetchall() == [(0,), (0,)]
        assert c.execute('SELECT status FROM eh_metadata_operations').fetchone()[0] == 'RUNNING'
        assert c.execute('PRAGMA foreign_key_check').fetchall() == []
    session = WorkerSession(db)
    await session.start()
    try:
        with db.connection() as c:
            assert c.execute('SELECT status, error_code FROM eh_metadata_operations').fetchone() == ('UNKNOWN', 'EH_METADATA_INTERRUPTED')
            assert c.execute("SELECT json_extract(response_json, '$.status') FROM account_command_receipts").fetchone()[0] == 'UNKNOWN'
            assert c.execute("SELECT status FROM queue_remote_resources WHERE legacy_job_id = 2").fetchone()[0] == 'INTENDED'
    finally:
        await session.stop()
