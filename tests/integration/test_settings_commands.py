"""Settings races exercise real validators, SQLite triggers and receipt rollback."""
import asyncio

import pytest

from app.archive.service import ArchiveSettingsService
from app.db.database import Database
from app.review.commands import WorkCommandError
from app.settings.commands import SettingsCommands
from app.settings.service import SystemSettingsService


async def setup(tmp_path):
    database = Database(tmp_path / 'settings.db')
    await database.initialize()
    system = SystemSettingsService(database)
    archive = ArchiveSettingsService(database, tmp_path)
    return database, system, archive, SettingsCommands(database, system, archive)


async def observed(commands, section='system', scope='system', key='settings', **values):
    return {'expected_revision': (await commands.versions(section))[scope], 'idempotency_key': key, **values}


@pytest.mark.asyncio
async def test_simultaneous_edit_has_one_winner_and_fixed_replay_after_later_edits(tmp_path):
    database, system, archive, commands = await setup(tmp_path)
    original = await observed(commands, timezone='Asia/Shanghai')
    results = await asyncio.gather(*(commands.execute('system', {**original, 'idempotency_key': key})
                                    for key in ('a', 'b')), return_exceptions=True)
    assert sum(isinstance(item, dict) for item in results) == 1
    assert [item.code for item in results if isinstance(item, WorkCommandError)] == ['REVISION_CONFLICT']
    winner = next(index for index, item in enumerate(results) if isinstance(item, dict))
    request = {**original, 'idempotency_key': ('a', 'b')[winner]}
    await system.save({'timezone': 'UTC'})
    assert await commands.execute('system', request) == results[winner]
    assert await system.timezone() == 'UTC'
    with pytest.raises(WorkCommandError) as failure:
        await commands.execute('system', {**request, 'timezone': 'Europe/Paris'})
    assert failure.value.code == 'IDEMPOTENCY_CONFLICT'


@pytest.mark.asyncio
async def test_same_key_concurrent_and_independent_regions(tmp_path):
    database, system, archive, commands = await setup(tmp_path)
    initial = await observed(commands, timezone='Asia/Shanghai')
    first, second = await asyncio.gather(*(commands.execute('system', initial) for _ in range(2)))
    assert first == second
    a = await observed(commands, 'archive', 'archive-limits', key='limits', max_members='50')
    b = await observed(commands, 'archive', 'auto-pack', key='pack', enabled=True)
    await asyncio.gather(commands.execute('limits', a), commands.execute('auto-pack', b))
    assert (await archive.limits()).max_members == 50
    assert await archive.auto_pack_after_download()


@pytest.mark.asyncio
async def test_late_validation_refusal_keeps_all_values_and_version(tmp_path):
    database, system, archive, commands = await setup(tmp_path)
    original = await observed(commands, 'paths', 'template', title_source='english', library_template='../{title}')
    version = await commands.versions('paths')
    with pytest.raises(WorkCommandError):
        await commands.execute('template', original)
    assert await archive.title_source() == 'japanese'
    assert await commands.versions('paths') == version
    request = await observed(commands, 'archive', 'archive-limits', max_members='50', image_quality='invalid')
    with pytest.raises(WorkCommandError):
        await commands.execute('limits', request)
    assert (await archive.limits()).max_members != 50
    with database.connection() as connection:
        assert connection.execute('SELECT count(*) FROM settings_command_receipts').fetchone() == (0,)


@pytest.mark.asyncio
async def test_receipt_failure_rolls_back_all_settings_and_versions(tmp_path, monkeypatch):
    database, system, archive, commands = await setup(tmp_path)
    original = await observed(commands, 'paths', 'template', title_source='english', library_template='{title}')
    version = await commands.versions('paths')
    save = SettingsCommands._save_receipt

    def fail(*args):
        save(*args)
        raise RuntimeError('Fixture failure after receipt')

    monkeypatch.setattr(SettingsCommands, '_save_receipt', staticmethod(fail))
    with pytest.raises(RuntimeError):
        await commands.execute('template', original)
    assert await archive.title_source() == 'japanese'
    assert await commands.versions('paths') == version
    with database.connection() as connection:
        assert connection.execute('SELECT count(*) FROM settings_command_receipts').fetchone() == (0,)


@pytest.mark.asyncio
async def test_final_cas_after_path_preparation_and_no_write_lock(tmp_path, monkeypatch):
    database, system, archive, commands = await setup(tmp_path)
    original = await observed(commands, 'paths', 'directories', library_path=str(tmp_path / 'library'))
    prepare = ArchiveSettingsService.save_paths

    async def race(self, values):
        result = await prepare(self, values)
        # This independent writer would deadlock if preparation held a write lock.
        await database.save_archive_settings({'library_path': str(tmp_path / 'other')})
        return result

    monkeypatch.setattr(ArchiveSettingsService, 'save_paths', race)
    with pytest.raises(WorkCommandError) as failure:
        await commands.execute('paths', original)
    assert failure.value.code == 'REVISION_CONFLICT'
    assert (await archive.paths())['library'] == str(tmp_path / 'other')


@pytest.mark.asyncio
async def test_unchanged_save_consumes_version_but_runtime_settings_do_not(tmp_path):
    database, system, archive, commands = await setup(tmp_path)
    await system.save({'timezone': 'UTC'})
    original = await observed(commands, timezone='UTC')
    result = await commands.execute('system', original)
    assert result['revision'] > original['expected_revision']
    await database.save_system_settings({'title_maintenance_v1': 'fixture'})
    assert (await commands.versions('system'))['system'] == result['revision']
    with pytest.raises(WorkCommandError) as failure:
        await commands.execute('system', {**original, 'idempotency_key': 'other'})
    assert failure.value.code == 'REVISION_CONFLICT'


@pytest.mark.asyncio
async def test_parse_and_admission_forms_map_to_separate_versions(tmp_path):
    database, system, archive, commands = await setup(tmp_path)
    before = await commands.versions('parse')
    reset = {'expected_revision': before['parse'], 'idempotency_key': 'reset'}
    await commands.execute('parse-reset', reset)
    assert (await commands.versions('parse'))['admission'] == before['admission']
    await commands.execute('admission', {'expected_revision': before['admission'], 'idempotency_key': 'admission',
        'ai_candidate_enabled': True, 'ai_candidate_fallback': 'reject'})
    assert (await system.candidate_admission())['enabled']


@pytest.mark.asyncio
async def test_missing_versions_keys_and_false_revision_never_write(tmp_path):
    database, system, archive, commands = await setup(tmp_path)
    for fields in ({}, {'expected_revision': True, 'idempotency_key': 'bool'}, {'expected_revision': 1}):
        with pytest.raises(WorkCommandError) as error:
            await commands.execute('system', {**fields, 'timezone': 'Asia/Shanghai'})
        assert error.value.status_code == 428
    assert await system.timezone() == 'UTC'


@pytest.mark.asyncio
async def test_restart_keeps_receipt_while_internal_write_invalidates_original_revision(tmp_path):
    database, system, archive, commands = await setup(tmp_path)
    payload = await observed(commands, timezone='Asia/Tokyo')
    first = await commands.execute('system', payload)
    database, system, archive, commands = await setup(tmp_path)
    await system.save({'timezone': 'UTC'})
    assert await commands.execute('system', payload) == first
    assert await system.timezone() == 'UTC'
    with pytest.raises(WorkCommandError) as error:
        await commands.execute('system', {**payload, 'idempotency_key': 'new-intent'})
    assert error.value.code == 'REVISION_CONFLICT'
