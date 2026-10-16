"""Versioned renames keep the journal, task and receipt in one commit."""
from pathlib import Path

import pytest

from app.review.application import rename_work
from app.review.commands import WorkCommandError, WorkCommands
from tests.integration.test_downloaded_commands import environment, payload


async def packed(tmp_path):
    database, archived, pack, candidate_id = await environment(tmp_path)
    await pack.enqueue_for_candidate(candidate_id)
    assert await pack._process_one()
    return database, archived, pack, candidate_id


@pytest.mark.asyncio
async def test_rename_repack_and_receipt_commit_once_even_after_task_finishes(tmp_path):
    database, archived, pack, candidate_id = await packed(tmp_path)
    observed = {**await payload(database, candidate_id), 'filename': 'Renamed', 'directory': 'Chosen', 'repack': True}
    old = Path((await database.downloaded_work(candidate_id)).cbz_path)
    result = await rename_work(database, archived, pack, candidate_id, observed, 'admin')
    target = tmp_path / 'library' / 'Chosen' / 'Renamed.cbz'
    assert target.exists() and not old.exists() and result['relative_path'] == 'Chosen/Renamed.cbz'
    assert await pack._process_one()
    assert await rename_work(database, archived, pack, candidate_id, observed, 'admin') == result
    with database.connection() as connection:
        assert connection.execute('SELECT count(*) FROM work_command_receipts').fetchone() == (1,)
        assert connection.execute("SELECT status FROM queue_relocations").fetchall() == [('COMMITTED',)]
        assert connection.execute('SELECT relative_path, is_manual FROM work_archive_paths').fetchone() == ('Chosen/Renamed.cbz', 1)


@pytest.mark.asyncio
@pytest.mark.parametrize('repack', [False, True])
async def test_rename_receipt_failure_restores_file_path_version_and_task(tmp_path, monkeypatch, repack):
    database, archived, pack, candidate_id = await packed(tmp_path)
    observed = {**await payload(database, candidate_id), 'filename': 'MustRollback', 'repack': repack}
    old = Path((await database.downloaded_work(candidate_id)).cbz_path)
    contents = old.read_bytes()

    def fail(*args):
        raise RuntimeError('Fixture receipt failure')

    monkeypatch.setattr(WorkCommands, '_save_receipt', staticmethod(fail))
    with pytest.raises(RuntimeError):
        await rename_work(database, archived, pack, candidate_id, observed, 'admin')
    assert old.read_bytes() == contents and not (old.parent / 'MustRollback.cbz').exists()
    assert (await payload(database, candidate_id))['expected_revision'] == observed['expected_revision']
    with database.connection() as connection:
        assert connection.execute('SELECT count(*) FROM work_command_receipts').fetchone() == (0,)
        assert connection.execute('SELECT status FROM queue_relocations').fetchall() == [('ROLLED_BACK',)]
        assert connection.execute("SELECT state FROM download_jobs WHERE provider = 'CONVERSION'").fetchall() == [('CONVERSION_COMPLETED',)]


@pytest.mark.asyncio
async def test_version_change_during_file_inspection_prevents_move(tmp_path, monkeypatch):
    database, archived, pack, candidate_id = await packed(tmp_path)
    observed = {**await payload(database, candidate_id), 'filename': 'Stale'}
    old = Path((await database.downloaded_work(candidate_id)).cbz_path)
    import app.downloads.archived as module
    original = module._inspect

    def change(path):
        with database.connection() as connection:
            connection.execute("UPDATE candidates SET status = 'REJECTED' WHERE id = ?", (candidate_id,))
        return original(path)

    monkeypatch.setattr(module, '_inspect', change)
    with pytest.raises(WorkCommandError) as error:
        await rename_work(database, archived, pack, candidate_id, observed, 'admin')
    assert error.value.code == 'REVISION_CONFLICT'
    assert old.exists() and not (old.parent / 'Stale.cbz').exists()
    with database.connection() as connection:
        assert connection.execute('SELECT count(*) FROM queue_relocations').fetchone() == (0,)


@pytest.mark.asyncio
async def test_rename_noop_is_fixed_and_changed_options_reject_key_reuse(tmp_path):
    database, archived, pack, candidate_id = await packed(tmp_path)
    observed = await payload(database, candidate_id)
    first = await rename_work(database, archived, pack, candidate_id, observed, 'admin')
    assert not first['moved']
    assert await rename_work(database, archived, pack, candidate_id, observed, 'admin') == first
    with pytest.raises(WorkCommandError) as error:
        await rename_work(database, archived, pack, candidate_id, {**observed, 'repack': True}, 'admin')
    assert error.value.code == 'IDEMPOTENCY_CONFLICT'


@pytest.mark.asyncio
@pytest.mark.parametrize('already_packed', [False, True])
async def test_exact_path_saves_pin_and_optional_pack_atomically(tmp_path, already_packed):
    database, archived, pack, candidate_id = await (packed(tmp_path) if already_packed else environment(tmp_path))
    observed = {**await payload(database, candidate_id), 'filename': 'Exact', 'directory': 'Folder', 'repack': True}
    result = await rename_work(database, archived, pack, candidate_id, observed, 'admin', strict=True)
    assert result['relative_path'] == 'Folder/Exact.cbz' and result['moved'] == already_packed
    assert await pack._process_one()
    assert (tmp_path / 'library' / 'Folder' / 'Exact.cbz').exists()
    assert await rename_work(database, archived, pack, candidate_id, observed, 'admin', strict=True) == result


@pytest.mark.asyncio
async def test_exact_path_does_not_suffix_or_overwrite_an_occupied_file(tmp_path):
    database, archived, pack, candidate_id = await packed(tmp_path)
    observed = {**await payload(database, candidate_id), 'filename': 'Taken'}
    target = tmp_path / 'library' / 'Taken.cbz'
    target.write_bytes(b'other-book')
    from app.downloads.archived import ArchivedWorkError
    with pytest.raises(ArchivedWorkError) as error:
        await rename_work(database, archived, pack, candidate_id, observed, 'admin', strict=True)
    assert error.value.code == 'PATH_TAKEN_ON_DISK'
    assert target.read_bytes() == b'other-book' and not (target.parent / 'Taken (2).cbz').exists()


@pytest.mark.asyncio
async def test_first_pack_pin_failure_does_not_leave_a_task_or_path(tmp_path, monkeypatch):
    database, archived, pack, candidate_id = await environment(tmp_path)
    observed = {**await payload(database, candidate_id), 'filename': 'Fail', 'repack': True}

    def fail(*args):
        raise RuntimeError('Fixture receipt failure')

    monkeypatch.setattr(WorkCommands, '_save_receipt', staticmethod(fail))
    with pytest.raises(RuntimeError):
        await rename_work(database, archived, pack, candidate_id, observed, 'admin', strict=True)
    with database.connection() as connection:
        assert connection.execute('SELECT count(*) FROM work_archive_paths').fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM download_jobs WHERE provider = 'CONVERSION'").fetchone() == (0,)


@pytest.mark.asyncio
async def test_real_process_exit_before_receipt_recovers_move_then_retries_original_command(tmp_path):
    import json
    import subprocess
    import sys
    from app.conversion.relocation import Relocations
    database, archived, pack, candidate_id = await packed(tmp_path)
    observed = {**await payload(database, candidate_id), 'filename': 'Crash', 'directory': 'AfterCrash', 'repack': True}
    original = Path((await database.downloaded_work(candidate_id)).cbz_path)
    original_bytes = original.read_bytes()
    code = '''
import asyncio, json, os, sys
from pathlib import Path
from app.db.database import Database
from app.downloads.archived import ArchivedWorkService
from app.conversion.service import ConversionService
from app.review.application import rename_work
from app.review.commands import WorkCommands
root, observed = Path(sys.argv[1]), json.loads(sys.argv[2])
database = Database(root / 'queue.db')
archived = ArchivedWorkService(database, root / 'library', root / 'work')
pack = ConversionService(database, root / 'work', root / 'library', data_path=root / 'data')
def crash(*args):
    os._exit(73)
WorkCommands._save_receipt = staticmethod(crash)
asyncio.run(rename_work(database, archived, pack, 1, observed, 'admin'))
'''
    process = subprocess.run([sys.executable, '-c', code, str(tmp_path), json.dumps(observed)],
                             capture_output=True, timeout=20)
    assert process.returncode == 73, process.stderr.decode(errors='replace')
    target = tmp_path / 'library' / 'AfterCrash' / 'Crash.cbz'
    assert target.exists() and not original.exists()
    with database.connection() as connection:
        assert connection.execute('SELECT count(*) FROM work_command_receipts').fetchone() == (0,)
        assert connection.execute('SELECT status FROM queue_relocations').fetchone() == ('PREPARED',)
    assert Relocations(database).recover() == 1
    assert original.read_bytes() == original_bytes and not target.exists()
    result = await rename_work(database, archived, pack, candidate_id, observed, 'admin')
    assert result['relative_path'] == 'AfterCrash/Crash.cbz'
    assert await rename_work(database, archived, pack, candidate_id, observed, 'admin') == result
