"""Whole-panel atomicity, scope independence and Series admission races."""
import asyncio

import pytest

from app.archive.service import ArchiveSettingsService
from app.review.commands import WorkCommandError
from app.settings.commands import SettingsCommands
from tests.integration.test_settings_commands import setup, observed


PANEL = {'path_source': 'ai', 'ai_prompt': 'draft {{metadata}}', 'ai_batch_size': 31,
         'ai_concurrency': 3, 'ai_stream': True, 'ai_fallback_to_rules': True,
         'ai_default_include_current': True, 'auto_series': False}


async def panel(commands, key='panel', **values):
    return await observed(commands, 'paths', 'path-ai', key=key, **{**PANEL, **values})


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', [{'ai_concurrency': 'bad'}, {'path_source': 'bad'}])
async def test_late_panel_validation_has_no_partial_write_or_receipt(tmp_path, invalid):
    database, _, _, commands = await setup(tmp_path)
    before = await database.archive_settings(), await commands.versions('paths')
    with pytest.raises(WorkCommandError):
        await commands.execute('path-ai', await panel(commands, **invalid))
    assert (await database.archive_settings(), await commands.versions('paths')) == before
    with database.connection() as connection:
        assert connection.execute('SELECT count(*) FROM settings_command_receipts').fetchone() == (0,)


@pytest.mark.asyncio
async def test_concurrent_panel_has_one_winner_and_fixed_replay_after_internal_prompt_change(tmp_path):
    database, _, archive, commands = await setup(tmp_path)
    body = await panel(commands)
    results = await asyncio.gather(*(commands.execute('path-ai', {**body, 'idempotency_key': key})
                                    for key in ('a', 'b')), return_exceptions=True)
    assert sum(isinstance(item, dict) for item in results) == 1
    assert [item.code for item in results if isinstance(item, WorkCommandError)] == ['REVISION_CONFLICT']
    winner = next(index for index, item in enumerate(results) if isinstance(item, dict))
    await archive.save_ai_prompt('later prompt')
    await database.initialize()
    assert await commands.execute('path-ai', {**body, 'idempotency_key': ('a', 'b')[winner]}) == results[winner]
    assert await archive.ai_prompt() == 'later prompt'
    assert await archive.ai_batch_size() == 31


@pytest.mark.asyncio
async def test_prompt_and_panel_share_version_but_model_sources_are_independent(tmp_path):
    database, system, archive, commands = await setup(tmp_path)
    body = await panel(commands)
    path_source = await observed(commands, 'paths', 'path-model-source', key='path-source', model_source='custom')
    candidate_source = await observed(commands, 'parse', 'candidate-model-source', key='candidate-source', model_source='custom')
    result = await commands.execute('path-ai', body)
    await asyncio.gather(commands.execute('path-model-source', path_source),
                         commands.execute('candidate-model-source', candidate_source))
    assert await archive.ai_model_source() == await system.ai_candidate_model_source() == 'custom'
    assert (await commands.versions('paths'))['path-ai'] == result['revision']
    with pytest.raises(WorkCommandError) as error:
        await commands.execute('path-ai-prompt', {**body, 'idempotency_key': 'stale-reset', 'prompt': ''})
    assert error.value.code == 'REVISION_CONFLICT'
    reset = await observed(commands, 'paths', 'path-ai', key='reset', prompt='')
    receipt = await commands.execute('path-ai-prompt', reset)
    assert (await database.archive_settings())['ai_prompt'] == ''
    await archive.save_ai_prompt('newer prompt')
    assert await commands.execute('path-ai-prompt', reset) == receipt
    assert await archive.ai_prompt() == 'newer prompt'
    with pytest.raises(WorkCommandError) as error:
        await commands.execute('path-ai', {**body, 'idempotency_key': 'stale-panel'})
    assert error.value.code == 'REVISION_CONFLICT'


@pytest.mark.asyncio
@pytest.mark.parametrize('action,section,scope,values', [
    ('path-ai', 'paths', 'path-ai', PANEL),
    ('path-ai-prompt', 'paths', 'path-ai', {'prompt': 'changed'}),
    ('path-model-source', 'paths', 'path-model-source', {'model_source': 'custom'}),
    ('candidate-model-source', 'parse', 'candidate-model-source', {'model_source': 'custom'}),
])
async def test_receipt_failure_rolls_back_entire_save(tmp_path, monkeypatch, action, section, scope, values):
    database, _, _, commands = await setup(tmp_path)
    body = await observed(commands, section, scope, **values)
    before = await database.archive_settings(), await database.system_settings(), await commands.versions(section)
    original = SettingsCommands._save_receipt
    def refuse(*args):
        original(*args)
        raise RuntimeError('fixture receipt failure')
    monkeypatch.setattr(SettingsCommands, '_save_receipt', staticmethod(refuse))
    with pytest.raises(RuntimeError):
        await commands.execute(action, body)
    assert (await database.archive_settings(), await database.system_settings(), await commands.versions(section)) == before
    with database.connection() as connection:
        assert connection.execute('SELECT count(*) FROM settings_command_receipts').fetchone() == (0,)


@pytest.mark.asyncio
@pytest.mark.parametrize('race', ['master', 'chain', 'source'])
async def test_series_checks_current_prerequisites_in_final_transaction(tmp_path, monkeypatch, race):
    database, system, archive, commands = await setup(tmp_path)
    provider = await database.save_ai_provider(provider_id=None, name='fixture', code='openai',
        base_url='https://fixture.invalid/v1', timeout_seconds=30, max_retries=0, enabled=True,
        custom_headers={}, default_params=None)
    model = await database.add_ai_provider_model(provider.provider_id, 'fixture')
    await database.save_ai_model_chain([model.model_id], 'default')
    await system.save_ai_enabled(True)
    body = await panel(commands, auto_series=True)
    original = ArchiveSettingsService.save_auto_series
    async def prepare(self, enabled):
        await original(self, enabled)
        # Independent writes can finish because preparation holds no DB write lock.
        if race == 'master':
            await system.save_ai_enabled(False)
        elif race == 'chain':
            await database.delete_ai_provider_model(model.model_id)
        else:
            await archive.save_ai_model_source('custom')
    monkeypatch.setattr(ArchiveSettingsService, 'save_auto_series', prepare)
    before = await database.archive_settings()
    with pytest.raises(WorkCommandError) as error:
        await commands.execute('path-ai', body)
    assert error.value.code == 'SERIES_AI_UNAVAILABLE'
    after = await database.archive_settings()
    if race == 'source':
        before['ai_model_source'] = 'custom'
    assert after == before
    assert (await commands.versions('paths'))['path-ai'] == body['expected_revision']


@pytest.mark.asyncio
async def test_series_accepts_current_effective_chain_and_replay_ignores_later_unavailability(tmp_path):
    from tests.integration.test_ai_chain_commands import setup as chain_setup
    database, models, _ = await chain_setup(tmp_path)
    from app.settings.service import SystemSettingsService
    system = SystemSettingsService(database)
    archive = ArchiveSettingsService(database, tmp_path)
    commands = SettingsCommands(database, system, archive)
    await system.save_ai_enabled(True)
    await archive.save_ai_model_source('custom')
    await database.save_ai_model_chain([models[0].model_id], 'archive_path')
    body = await panel(commands, auto_series=True)
    receipt = await commands.execute('path-ai', body)
    assert await archive.auto_series()
    await system.save_ai_enabled(False)
    assert await commands.execute('path-ai', body) == receipt


@pytest.mark.asyncio
@pytest.mark.parametrize('action,section', [('path-model-source', 'paths'), ('candidate-model-source', 'parse')])
async def test_model_source_cas_and_cross_channel_normalization(tmp_path, action, section):
    database, _, _, commands = await setup(tmp_path)
    body = await observed(commands, section, action, model_source='custom')
    results = await asyncio.gather(*(commands.execute(action, {**body, 'idempotency_key': key})
                                    for key in ('a', 'b')), return_exceptions=True)
    assert sum(isinstance(item, dict) for item in results) == 1
    assert [item.code for item in results if isinstance(item, WorkCommandError)] == ['REVISION_CONFLICT']
    winner = next(index for index, item in enumerate(results) if isinstance(item, dict))
    replay = {key: value for key, value in body.items() if key != 'model_source'}
    replay.update(ai_model_source=' CUSTOM ', idempotency_key=('a', 'b')[winner])
    assert await commands.execute(action, replay) == results[winner]
    version = await commands.versions(section)
    with pytest.raises(WorkCommandError) as error:
        await commands.execute(action, await observed(commands, section, action, key='invalid', model_source='bad'))
    assert error.value.code == 'AI_MODEL_SOURCE_INVALID'
    assert await commands.versions(section) == version
