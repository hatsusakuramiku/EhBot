"""Upgrade the actual 036 schema, preserving legacy files and pending intent."""
from pathlib import Path

import pytest

from app.candidates.migration import backfill_ingestion
from app.conversion.service import ConversionService
from app.db.database import Database
from tests.integration.test_ingest_migration import legacy_database
from tests.integration.test_artifact_pipeline import downloader, packer
from tests.integration.test_archive_workflow import write_zip


def schema036(tmp_path):
    db = legacy_database(tmp_path)
    with db.connection() as connection:
        for path in sorted(Path('app/db/migrations').glob('*.sql')):
            version = int(path.name.split('_', 1)[0])
            if not 31 <= version <= 36:
                continue
            connection.executescript('BEGIN IMMEDIATE;\n' + path.read_text(encoding='utf-8'))
            if version == 31:
                backfill_ingestion(connection)
            connection.execute('INSERT INTO schema_migrations VALUES(?, CURRENT_TIMESTAMP)', (version,))
            connection.commit()
    return db


@pytest.mark.asyncio
async def test_upgrade_preserves_legacy_files_and_does_not_schedule_completed_books(tmp_path):
    db = schema036(tmp_path)
    original, cbz = tmp_path / 'old.zip', tmp_path / 'old.cbz'
    write_zip(original, ('01.jpg',))
    write_zip(cbz, ('01.jpg',))
    before = original.read_bytes(), cbz.read_bytes()
    with db.connection() as connection:
        connection.execute("INSERT INTO candidates(id, status) VALUES(1, 'APPROVED')")
        connection.execute("INSERT INTO download_jobs(id, candidate_id, idempotency_key, provider, state) "
            "VALUES(1, 1, 'old', 'TELEGRAM', 'COMPLETED')")
        for kind, path in (('ARCHIVE', original), ('CBZ', cbz)):
            connection.execute('INSERT INTO artifacts(job_id, artifact_type, path) VALUES(1, ?, ?)', (kind, str(path)))
        connection.execute("INSERT INTO archive_settings(key, value) VALUES('auto_pack_after_download', '1')")
    await db.initialize()
    assert (original.read_bytes(), cbz.read_bytes()) == before
    with db.connection() as connection:
        versions = connection.execute('SELECT artifact_id, producer_attempt_id FROM artifact_versions').fetchall()
        assert len(versions) == 2 and all(aid and producer is None for aid, producer in versions)
        assert connection.execute('SELECT count(*) FROM queue_pack_intents').fetchone() == (0,)
        assert connection.execute('PRAGMA foreign_key_check').fetchall() == []
    assert not await packer(db, tmp_path)._process_one()


@pytest.mark.asyncio
async def test_upgrade_pending_legacy_pack_waits_for_active_b(tmp_path):
    db = schema036(tmp_path)
    original = tmp_path / 'old.zip'
    write_zip(original, ('01.jpg',))
    with db.connection() as connection:
        connection.execute("INSERT INTO candidates(id, status) VALUES(1, 'APPROVED')")
        connection.execute("INSERT INTO download_jobs(id, candidate_id, idempotency_key, provider, state, details_json) "
            "VALUES(1, 1, 'old', 'TELEGRAM', 'COMPLETED', '{\"file_id\":\"a\",\"file_name\":\"new.zip\"}')")
        connection.execute("INSERT INTO artifacts(job_id, artifact_type, path) VALUES(1, 'ARCHIVE', ?)", (str(original),))
        connection.execute("UPDATE download_jobs SET state = 'PENDING' WHERE id = 1")
        connection.execute("INSERT INTO download_jobs(candidate_id, idempotency_key, provider, state) "
            "VALUES(1, 'convert:1', 'CONVERSION', 'CONVERSION_PENDING')")
    await db.initialize()
    pack = packer(db, tmp_path)
    assert not await pack._process_one()
    assert await downloader(db, tmp_path)._process_one()
    assert await pack._process_one()
    with db.connection() as connection:
        assert connection.execute("SELECT a.page_count FROM artifacts a WHERE a.artifact_type = 'CBZ'").fetchone() == (2,)
        assert connection.execute("SELECT p.pack_requested, p.status FROM queue_pack_intents p JOIN download_jobs j "
            "ON j.task_id = p.task_id WHERE j.id = 1").fetchone() == (1, 'QUEUED')
