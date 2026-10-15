"""Downloads and explicit packs keep their input across missed wakeups/restarts."""
import json
import subprocess
import sys
from types import SimpleNamespace
import zipfile

import pytest

from app.archive.service import ArchiveSettingsService
from app.conversion.service import ConversionService
from app.db.database import Database
from app.downloads.archived import ArchivedWorkService
from app.downloads.service import DownloadService
from tests.integration.test_queue_control import observed
from tests.integration.test_archive_workflow import seed_downloaded_archive, write_zip


async def prepare(tmp_path):
    db = Database(tmp_path / "queue.db")
    old = tmp_path / "work" / "downloads" / "old.zip"
    write_zip(old, ("01.jpg",))
    work_id = await seed_downloaded_archive(db, old)
    settings = ArchiveSettingsService(db, tmp_path / "data")
    await settings.save_keep_original(True)
    with db.connection() as connection:
        connection.execute("UPDATE download_jobs SET details_json = ? WHERE id = 1",
                           (json.dumps({"file_id": "a", "file_name": "new.zip"}),))
    return db, old, work_id


def packer(db, tmp_path):
    return ConversionService(db, tmp_path / "work", tmp_path / "library", data_path=tmp_path / "data")


def downloader(db, tmp_path):
    class Api:
        async def get_file(self, file_id):
            return SimpleNamespace(file_unique_id="opaque-identity", file_path="document/new.zip")

        async def download_file(self, remote, destination):
            write_zip(destination, ("01.jpg", "02.jpg"))
            return destination.stat().st_size

    async def client():
        return Api()

    service = DownloadService(db, tmp_path / "work")
    service._telegram_api = client
    return service


@pytest.mark.asyncio
async def test_redownload_repack_waits_for_b_and_survives_missing_notification(tmp_path):
    db, old, work_id = await prepare(tmp_path)
    pack = packer(db, tmp_path)
    await pack.enqueue_for_candidate(work_id)
    assert await pack._process_one()
    published = tmp_path / "library" / "Archive Title.cbz"
    previous = published.read_bytes()
    archive = ArchivedWorkService(db, tmp_path / "library", tmp_path / "work",
                                  conversion_enqueue=pack.enqueue_for_candidate)
    await archive.redownload_work(work_id, repack=True)
    assert not await pack._process_one()
    assert published.read_bytes() == previous
    # Auto-pack stays off. There is deliberately no in-memory callback.
    assert await downloader(db, tmp_path)._process_one()
    with db.connection() as connection:
        intent = connection.execute("SELECT p.input_artifact_id, p.status, a.path FROM queue_pack_intents p "
            "JOIN download_jobs j ON j.task_id = p.task_id JOIN artifact_versions a "
            "ON a.artifact_id = p.input_artifact_id WHERE j.id = 1").fetchone()
    assert intent[1] == "READY" and intent[2] != str(old)
    restarted = packer(Database(db.path), tmp_path)
    assert await restarted._process_one()
    with zipfile.ZipFile(published) as result:
        assert len([name for name in result.namelist() if name.endswith(".jpg")]) == 2
    with db.connection() as connection:
        details = json.loads(connection.execute("SELECT details_json FROM queue_attempts "
            "WHERE kind = 'PACK' ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()[0])
    assert details["input_artifact_id"] == intent[0]


@pytest.mark.asyncio
async def test_explicit_pack_keeps_exact_a_when_a_new_download_finishes(tmp_path):
    db, old, work_id = await prepare(tmp_path)
    pack = packer(db, tmp_path)
    job_id = await pack.enqueue_for_candidate(work_id)
    archive = ArchivedWorkService(db, tmp_path / "library", tmp_path / "work")
    await archive.redownload_work(work_id, repack=False)
    assert await downloader(db, tmp_path)._process_one()
    assert await pack._process_one()
    with db.connection() as connection:
        row = connection.execute("SELECT a.path FROM queue_attempts q JOIN artifact_versions a "
            "ON a.artifact_id = json_extract(q.details_json, '$.input_artifact_id') "
            "WHERE q.task_id = (SELECT task_id FROM download_jobs WHERE id = ?)", (job_id,)).fetchone()
    assert row[0] == str(old)
    with zipfile.ZipFile(tmp_path / "library" / "Archive Title.cbz") as result:
        assert len([name for name in result.namelist() if name.endswith(".jpg")]) == 1


def crash_pack(db, tmp_path, boundary):
    code = '''
import asyncio, os, sys
from pathlib import Path
from app.db.database import Database
from app.conversion.service import ConversionService
root = Path(sys.argv[1])
pack = ConversionService(Database(Path(sys.argv[2])), root / 'work', root / 'library', data_path=root / 'data')
name = {'prepare': 'prepare', 'replace': '_replace', 'confirm': 'finish', 'cleanup': '_cleanup'}[sys.argv[3]]
original = getattr(pack._publications, name)
def power_loss(*args):
    if sys.argv[3] == 'cleanup':
        os._exit(73)
    original(*args)
    os._exit(73)
setattr(pack._publications, name, power_loss)
asyncio.run(pack._process_one())
'''
    result = subprocess.run([sys.executable, '-c', code, str(tmp_path), str(db.path), boundary],
                            capture_output=True, timeout=20)
    assert result.returncode == 73, result.stderr.decode(errors='replace')


async def queued_b(tmp_path):
    db, old, work_id = await prepare(tmp_path)
    pack = packer(db, tmp_path)
    await pack.enqueue_for_candidate(work_id)
    await pack._process_one()
    target = tmp_path / 'library' / 'Archive Title.cbz'
    previous = target.read_bytes()
    archive = ArchivedWorkService(db, tmp_path / 'library', tmp_path / 'work')
    await archive.redownload_work(work_id, repack=True)
    assert await downloader(db, tmp_path)._process_one()
    assert await pack.dispatch_pending_packs() == 1
    return db, pack, target, previous


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['prepare', 'replace', 'confirm', 'cleanup'])
async def test_real_process_exit_recovers_publication_without_repacking(tmp_path, boundary):
    db, _, target, previous = await queued_b(tmp_path)
    if boundary == 'cleanup':
        await ArchiveSettingsService(db, tmp_path / 'data').save_keep_original(False)
    crash_pack(db, tmp_path, boundary)
    if boundary == 'prepare':
        assert target.read_bytes() == previous
    restarted = packer(Database(db.path), tmp_path)
    await restarted.start()
    await restarted.stop()
    with db.connection() as connection:
        assert connection.execute("SELECT status FROM queue_publications ORDER BY rowid DESC LIMIT 1").fetchone() == ('COMMITTED',)
        assert connection.execute("SELECT count(*) FROM queue_attempts WHERE kind = 'PACK'").fetchone() == (2,)
    with zipfile.ZipFile(target) as result:
        assert len([name for name in result.namelist() if name.endswith('.jpg')]) == 2
    assert not list((tmp_path / 'library').rglob('*.previous'))
    if boundary == 'cleanup':
        with db.connection() as connection:
            source, cleaned = connection.execute('SELECT source_path, cleanup_complete FROM queue_publications ORDER BY rowid DESC LIMIT 1').fetchone()
        from pathlib import Path
        assert cleaned == 1 and not Path(source).exists()


@pytest.mark.asyncio
async def test_cancel_after_process_exit_restores_a_before_stop_is_confirmed(tmp_path):
    db, pack, target, previous = await queued_b(tmp_path)
    crash_pack(db, tmp_path, 'replace')
    with db.connection() as connection:
        job_id = connection.execute("SELECT id FROM download_jobs WHERE provider = 'CONVERSION'").fetchone()[0]
    downloads = DownloadService(db, tmp_path / 'work', stop_pack=pack.stop_attempt)
    await downloads.command(job_id, 'cancel', **observed(db, job_id))
    assert await downloads.process_operations() == 1
    assert target.read_bytes() == previous
    with db.connection() as connection:
        assert connection.execute("SELECT status FROM queue_publications ORDER BY rowid DESC LIMIT 1").fetchone() == ('ROLLED_BACK',)
        assert connection.execute("SELECT state FROM download_jobs WHERE id = ?", (job_id,)).fetchone() == ('CANCELLED',)


@pytest.mark.asyncio
async def test_recovery_preserves_unrecognized_destination_and_backup(tmp_path):
    db, pack, target, previous = await queued_b(tmp_path)
    crash_pack(db, tmp_path, 'replace')
    target.write_bytes(b'external edit')
    pack._publications.recover()
    assert target.read_bytes() == b'external edit'
    with db.connection() as connection:
        assert connection.execute("SELECT status, error_code FROM queue_publications ORDER BY rowid DESC LIMIT 1").fetchone() == (
            'RECOVERY_REQUIRED', 'PUBLICATION_RECOVERY_REQUIRED')
    assert list((tmp_path / 'library').rglob('*.previous'))
    from app.conversion.convert import ConversionError
    with pytest.raises(ConversionError) as error:
        await pack.enqueue_for_candidate(1)
    assert error.value.code == 'PUBLICATION_RECOVERY_REQUIRED'
    # The operator restores a verified preimage. Retry reconciles that record
    # before producing another version; it never overwrites unrecognized bytes.
    target.write_bytes(previous)
    await pack.enqueue_for_candidate(1)
    assert await pack._process_one()
    with zipfile.ZipFile(target) as result:
        assert len([name for name in result.namelist() if name.endswith('.jpg')]) == 2


@pytest.mark.asyncio
async def test_auto_pack_default_is_captured_at_enqueue(tmp_path):
    db, _, work_id = await prepare(tmp_path)
    pack = packer(db, tmp_path)
    enabled = True

    async def setting():
        return enabled

    downloads = downloader(db, tmp_path)
    downloads._auto_pack_enabled = setting
    downloads._auto_pack = pack.dispatch_pending_packs
    await downloads.enqueue_telegram_download(work_id, {'file_id': 'a', 'file_unique_id': 'a', 'file_name': 'new.zip'}, redownload=True)
    enabled = False
    assert await downloads._process_one()
    assert await pack._process_one()
    with db.connection() as connection:
        assert connection.execute("SELECT pack_requested, status FROM queue_pack_intents p JOIN download_jobs j "
            "ON j.task_id = p.task_id WHERE j.id = 1").fetchone() == (1, 'QUEUED')


@pytest.mark.asyncio
async def test_manual_publication_retains_download_intent_when_enqueue_never_runs(tmp_path):
    from app.candidates.manual import ingest_manual_link, enqueue_manual_candidate
    from tests.integration.test_manual_ingest_identity import Gallery

    class PowerLoss(BaseException):
        pass

    db = Database(tmp_path / 'queue.db')
    await db.initialize()
    gallery = Gallery(db)
    gallery.release.set()
    publish = gallery.publish_manual_gallery

    async def interrupted(*args, **kwargs):
        await publish(*args, **kwargs)
        raise PowerLoss()

    gallery.publish_manual_gallery = interrupted
    router = SimpleNamespace(route_source=lambda candidate: SimpleNamespace(provider='EXHENTAI'))
    with pytest.raises(PowerLoss):
        await ingest_manual_link('https://e-hentai.org/g/123/token/', database=db,
            exhentai_service=gallery, orchestrator=router, download_service=None)
    downloads = DownloadService(Database(db.path), tmp_path / 'work')
    downloads._manual_enqueue = lambda candidate_id: enqueue_manual_candidate(candidate_id,
        database=downloads._database, orchestrator=router, download_service=downloads)
    assert await downloads._process_one()
    assert not await downloads._process_one()
    with db.connection() as connection:
        assert connection.execute('SELECT count(*) FROM manual_download_intents').fetchone() == (0,)
        assert connection.execute('SELECT count(*) FROM candidates').fetchone() == (1,)
        assert connection.execute('SELECT count(*) FROM download_jobs').fetchone() == (1,)
    assert gallery.calls == 1


@pytest.mark.asyncio
async def test_artifact_content_facts_survive_owner_merge_and_record_removal(tmp_path):
    import sqlite3

    db, _, work_id = await prepare(tmp_path)
    with db.connection() as connection:
        artifact_id, path = connection.execute('SELECT artifact_id, path FROM artifacts').fetchone()
        connection.execute("INSERT INTO candidates(id, status) VALUES(2, 'APPROVED')")
        connection.execute('UPDATE download_jobs SET candidate_id = 2 WHERE candidate_id = ?', (work_id,))
    pack = packer(db, tmp_path)
    await pack.enqueue_for_candidate(2, input_artifact_id=artifact_id)
    assert await pack._process_one()
    with db.connection() as connection:
        with pytest.raises(sqlite3.IntegrityError, match='ARTIFACT_VERSION_IMMUTABLE'):
            connection.execute("UPDATE artifact_versions SET path = 'wrong' WHERE artifact_id = ?", (artifact_id,))
        connection.execute('DELETE FROM artifacts WHERE job_id = 1')
        connection.execute('DELETE FROM download_jobs WHERE id = 1')
        assert connection.execute('SELECT candidate_id, path FROM artifact_versions WHERE artifact_id = ?',
                                  (artifact_id,)).fetchone() == (None, path)


@pytest.mark.asyncio
async def test_restart_preserves_requested_pack_and_supersedes_unclaimed_followup(tmp_path):
    db, _, work_id = await prepare(tmp_path)
    settings = ArchiveSettingsService(db, tmp_path / 'data')
    await settings.save_auto_pack_after_download(True)
    downloads = downloader(db, tmp_path)
    first = await downloads.enqueue_telegram_download(work_id, {'file_id': 'a', 'file_unique_id': 'a'}, redownload=True)
    assert await downloads._process_one()
    await settings.save_auto_pack_after_download(False)
    downloads._queue_control.internal_command(1, 'restart')
    with db.connection() as connection:
        assert connection.execute('SELECT status FROM queue_pack_intents WHERE task_id = ?', (first.task_id,)).fetchone() == ('CANCELLED',)
        assert connection.execute('SELECT p.pack_requested, p.status FROM queue_pack_intents p '
            'JOIN download_jobs j ON j.task_id = p.task_id WHERE j.id = 1').fetchone() == (1, 'WAITING')
