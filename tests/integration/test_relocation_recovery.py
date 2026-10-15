"""Hard process exits must not leave CBZ paths and bytes disagreeing."""
from pathlib import Path, PurePosixPath
import subprocess
import sys

import pytest

from app.conversion.convert import ConversionError
from app.downloads.archived import ArchivedWorkError
from app.downloads.service import DownloadService
from tests.integration.test_queue_control import observed
from tests.integration.test_refile_cancellation import packaged


def crash_move(db, root, boundary, mode):
    code = '''
import asyncio, os, sys
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
import app.downloads.archived as archived_module
from app.db.database import Database
from app.downloads.archived import ArchivedWorkService
from app.conversion.service import ConversionService
root, boundary, mode = Path(sys.argv[1]), sys.argv[3], sys.argv[4]
db = Database(Path(sys.argv[2]))
archive = ArchivedWorkService(db, root / 'library', root / 'work')
pack = ConversionService(db, root / 'work', root / 'library',
    refile=lambda cid, relative: archive.refile_work(cid, relative, allow_while_running=True))
async def path(*args, **kwargs):
    return PurePosixPath('Changed/Book.cbz')
pack.ai_path_for_refile = path
source = root / 'library' / 'Archive Title.cbz'
replace = Path.replace
def move(path, target):
    result = replace(path, target)
    if path == source and boundary == 'replace':
        os._exit(74)
    return result
Path.replace = move
connection = db.connection
@contextmanager
def journal_boundary():
    with connection() as c:
        yield c
    if boundary == 'prepare':
        with db._connect() as c:
            if c.execute("SELECT 1 FROM queue_relocations WHERE status = 'PREPARED'").fetchone():
                os._exit(74)
db.connection = journal_boundary
prune = archived_module._prune_empty_parents
def committed(*args):
    if boundary == 'commit':
        os._exit(74)
    return prune(*args)
archived_module._prune_empty_parents = committed
async def run():
    if mode == 'manual':
        await archive.rename_work(1, filename='Book', directory='Changed')
    else:
        await pack._process_one()
asyncio.run(run())
'''
    result = subprocess.run([sys.executable, '-c', code, str(root), str(db.path), boundary, mode],
                            capture_output=True, timeout=20)
    assert result.returncode == 74, result.stderr.decode(errors='replace')


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['manual', 'worker'])
@pytest.mark.parametrize('boundary', ['prepare', 'replace', 'commit'])
async def test_process_exit_recovers_move_and_exact_pin(tmp_path, mode, boundary):
    db, work_id, _, pack = await packaged(tmp_path)
    old = tmp_path / 'library' / 'Archive Title.cbz'
    target = tmp_path / 'library' / 'Changed' / 'Book.cbz'
    original = old.read_bytes()
    if mode == 'worker':
        await pack.enqueue_for_candidate(work_id, refile=True)
    crash_move(db, tmp_path, boundary, mode)
    # This is the startup order: recover journals before reclaiming workers.
    pack._publications.recover()
    committed = boundary == 'commit'
    assert (target if committed else old).read_bytes() == original
    assert not (old if committed else target).exists()
    with db.connection() as c:
        status = c.execute('SELECT status FROM queue_relocations').fetchone()[0]
        recorded = c.execute("SELECT path FROM artifacts WHERE artifact_type = 'CBZ'").fetchone()[0]
        assert status == ('COMMITTED' if committed else 'ROLLED_BACK')
        assert Path(recorded) == (target if committed else old)
        if committed and mode == 'worker':
            assert c.execute("SELECT state FROM download_jobs WHERE provider = 'CONVERSION'").fetchone()[0] == 'CONVERSION_COMPLETED'
    pin = await db.archive_path_pin(work_id)
    assert (pin['relative_path'] == 'Changed/Book.cbz') if committed else pin is None


@pytest.mark.asyncio
async def test_cancel_crashed_refile_restores_file_before_stop(tmp_path):
    db, work_id, _, pack = await packaged(tmp_path)
    job_id = await pack.enqueue_for_candidate(work_id, refile=True)
    crash_move(db, tmp_path, 'replace', 'worker')
    downloads = DownloadService(db, tmp_path / 'work', stop_pack=pack.stop_attempt)
    await downloads.command(job_id, 'cancel', **observed(db, job_id))
    assert await downloads.process_operations() == 1
    assert (tmp_path / 'library' / 'Archive Title.cbz').exists()
    assert not (tmp_path / 'library' / 'Changed' / 'Book.cbz').exists()
    with db.connection() as c:
        assert c.execute('SELECT status FROM queue_relocations').fetchone()[0] == 'ROLLED_BACK'
        assert c.execute('SELECT state FROM download_jobs WHERE id = ?', (job_id,)).fetchone()[0] == 'CANCELLED'


@pytest.mark.asyncio
async def test_unknown_move_bytes_block_overwrite_and_removal(tmp_path):
    db, work_id, archive, pack = await packaged(tmp_path)
    original = (tmp_path / 'library' / 'Archive Title.cbz').read_bytes()
    crash_move(db, tmp_path, 'replace', 'manual')
    target = tmp_path / 'library' / 'Changed' / 'Book.cbz'
    target.write_bytes(b'external edit')
    pack._publications.recover()
    assert target.read_bytes() == b'external edit'
    with pytest.raises(ConversionError, match='移动'):
        await pack.enqueue_for_candidate(work_id, refile=True)
    with pytest.raises(ArchivedWorkError) as blocked:
        await archive.purge_work(work_id, delete_files=True)
    assert blocked.value.code == 'PUBLICATION_RECOVERY_REQUIRED'
    with pytest.raises(ArchivedWorkError) as blocked:
        await archive.rename_work(work_id, filename='Another')
    assert blocked.value.code == 'PUBLICATION_RECOVERY_REQUIRED'
    assert target.read_bytes() == b'external edit'
    target.write_bytes(original)
    await archive.rename_work(work_id, filename='Archive Title')
    assert (tmp_path / 'library' / 'Archive Title.cbz').read_bytes() == original


@pytest.mark.asyncio
async def test_pack_and_refile_do_not_replace_each_others_pending_input(tmp_path):
    db, work_id, _, pack = await packaged(tmp_path)
    from tests.integration.test_archive_workflow import write_zip
    source = tmp_path / 'work' / 'new.zip'
    write_zip(source, ('01.jpg',))
    with db.connection() as c:
        c.execute("UPDATE artifacts SET path = ? WHERE artifact_type = 'ARCHIVE'", (str(source),))
    await pack.enqueue_for_candidate(work_id)
    with pytest.raises(ConversionError) as busy:
        await pack.enqueue_for_candidate(work_id, refile=True)
    assert busy.value.code == 'PACK_INPUT_CONFLICT'
    assert await pack._process_one()
    await pack.enqueue_for_candidate(work_id, refile=True)
    with pytest.raises(ConversionError) as busy:
        await pack.enqueue_for_candidate(work_id)
    assert busy.value.code == 'PACK_INPUT_CONFLICT'


@pytest.mark.asyncio
async def test_target_occupied_after_prepare_does_not_block_future_renames(tmp_path):
    from contextlib import contextmanager

    db, work_id, archive, _ = await packaged(tmp_path)
    target = tmp_path / 'library' / 'New.cbz'
    connection = db.connection

    @contextmanager
    def take_target():
        with connection() as c:
            yield c
        with db._connect() as c:
            prepared = c.execute("SELECT 1 FROM queue_relocations WHERE status = 'PREPARED'").fetchone()
        if prepared and not target.exists():
            target.write_bytes(b'another work')

    db.connection = take_target
    with pytest.raises(ArchivedWorkError) as busy:
        await archive.rename_work(work_id, filename='New')
    assert busy.value.code == 'WORK_PATH_CHANGED'
    assert target.read_bytes() == b'another work'
    assert (tmp_path / 'library' / 'Archive Title.cbz').exists()
    db.connection = connection
    moved = await archive.rename_work(work_id, filename='Other')
    assert moved['moved']
    with db.connection() as c:
        assert c.execute("SELECT count(*) FROM queue_relocations WHERE status IN ('PREPARED', 'RECOVERY_REQUIRED')").fetchone()[0] == 0
@pytest.mark.asyncio
async def test_refile_does_not_silently_follow_a_later_cbz_version(tmp_path):
    db, work_id, archive, pack = await packaged(tmp_path)
    job_id = await pack.enqueue_for_candidate(work_id, refile=True)
    with db.connection() as c:
        original_input = c.execute("SELECT json_extract(details_json, '$.input_artifact_id') "
                                  'FROM download_jobs WHERE id = ?', (job_id,)).fetchone()[0]
    await archive.rename_work(work_id, filename='Newer')
    async def path(*args, **kwargs):
        return PurePosixPath('Changed/Book.cbz')
    pack.ai_path_for_refile = path
    assert await pack._process_one()
    assert (tmp_path / 'library' / 'Newer.cbz').exists()
    assert not (tmp_path / 'library' / 'Changed' / 'Book.cbz').exists()
    with db.connection() as c:
        state, error = c.execute('SELECT state, error_code FROM download_jobs WHERE id = ?', (job_id,)).fetchone()
        assert (state, error) == ('CONVERSION_FAILED', 'PACK_INPUT_CONFLICT')
        assert c.execute("SELECT json_extract(details_json, '$.input_artifact_id') FROM queue_attempts "
                         "WHERE kind = 'PACK' ORDER BY rowid DESC LIMIT 1").fetchone()[0] == original_input
