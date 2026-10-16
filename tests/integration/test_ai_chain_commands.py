"""Scope-local ordering CAS and receipts in real SQLite transactions."""
import asyncio

import pytest

from app.ai.errors import AiError
from app.ai.models import CHAIN_SCOPES
from app.ai.service import AiProviderService
from app.review.commands import WorkCommandError
from app.settings.ai_commands import AiChainCommands
from app.settings.commands import SettingsCommands
from tests.integration.test_ai_catalogue_commands import catalogue


async def setup(tmp_path):
    database, _, provider, _ = await catalogue(tmp_path)
    models = await database.add_ai_provider_models(provider['provider_id'], ['one', 'two', 'three'])
    return database, models, AiChainCommands(database)


def observed(database, scope, model, action='append', key='operation', **values):
    with database.connection() as connection:
        version = connection.execute('SELECT revision FROM settings_versions WHERE scope = ?', ('ai-chain:' + scope,)).fetchone()[0]
    return dict(model_id=model.model_id, model_uuid=model.model_uuid, expected_revision=version,
                action=action, idempotency_key=key, **values)


@pytest.mark.asyncio
@pytest.mark.parametrize('scope', CHAIN_SCOPES)
async def test_chain_cas_has_one_winner_and_replay_after_restart(tmp_path, scope):
    database, models, commands = await setup(tmp_path)
    bodies = [observed(database, scope, model, key='device-' + str(index)) for index, model in enumerate(models[:2])]
    results = await asyncio.gather(*(commands.execute(scope, body) for body in bodies), return_exceptions=True)
    assert sum(isinstance(result, dict) for result in results) == 1
    assert [result.code for result in results if isinstance(result, WorkCommandError)] == ['REVISION_CONFLICT']
    winner = next(index for index, result in enumerate(results) if isinstance(result, dict))
    await database.initialize()
    assert await AiChainCommands(database).execute(scope, bodies[winner]) == results[winner]
    assert [entry.model.model_id for entry in await database.list_ai_model_chain(scope)] == [models[winner].model_id]


@pytest.mark.asyncio
@pytest.mark.parametrize('action,model_index,delta,expected', [
    ('primary', 2, 0, [2, 0, 1]), ('append', 0, 0, [0, 1, 2]),
    ('remove', 1, 0, [0, 2]), ('shift', 0, 999, [1, 2, 0]), ('shift', 2, -999, [2, 0, 1]),
])
async def test_ordering_reuses_original_rules_and_unchanged_commands_consume_version(tmp_path, action, model_index, delta, expected):
    database, models, commands = await setup(tmp_path)
    await database.save_ai_model_chain([model.model_id for model in models], 'default')
    body = observed(database, 'default', models[model_index], action=action, delta=delta)
    result = await commands.execute('default', body)
    assert result['revision'] > body['expected_revision']
    assert [entry.model.model_id for entry in await database.list_ai_model_chain('default')] == [models[index].model_id for index in expected]
    assert await commands.execute('default', body) == result
    with pytest.raises(WorkCommandError, match='此模型链'):
        await commands.execute('default', {**body, 'idempotency_key': 'another'})


@pytest.mark.asyncio
async def test_independent_scopes_health_and_deleted_member_versions(tmp_path):
    database, models, commands = await setup(tmp_path)
    bodies = {scope: observed(database, scope, models[0], key=scope) for scope in CHAIN_SCOPES}
    for scope in CHAIN_SCOPES:
        await commands.execute(scope, bodies[scope])
    old = observed(database, 'default', models[1], key='before-delete')
    await database.mark_ai_model_verified(models[0].model_id, ok=True, error=None)
    await database.set_ai_provider_model_params(models[0].model_id, AiProviderService.parse_params('{"max_tokens":64}'))
    assert observed(database, 'default', models[1])['expected_revision'] == old['expected_revision']
    await database.delete_ai_provider_model(models[0].model_id)
    for scope in CHAIN_SCOPES:
        assert await database.list_ai_model_chain(scope) == ()
        assert observed(database, scope, models[1])['expected_revision'] > bodies[scope]['expected_revision']
    with pytest.raises(WorkCommandError) as failure:
        await commands.execute('default', old)
    assert failure.value.code == 'REVISION_CONFLICT'


@pytest.mark.asyncio
async def test_reused_model_id_cannot_adopt_old_intent_but_original_receipt_replays(tmp_path):
    database, models, commands = await setup(tmp_path)
    original = models[-1]
    body = observed(database, 'default', original)
    result = await commands.execute('default', body)
    await database.delete_ai_provider_model(original.model_id)
    replacement = await database.add_ai_provider_model(original.provider_id, 'replacement')
    assert replacement.model_id == original.model_id and replacement.model_uuid != original.model_uuid
    assert await commands.execute('default', body) == result
    with pytest.raises(WorkCommandError) as failure:
        await commands.execute('default', {**body, 'idempotency_key': 'stale-new-operation'})
    assert failure.value.status_code == 410
    assert await database.list_ai_model_chain('default') == ()


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['primary', 'append', 'remove', 'shift'])
async def test_receipt_failure_rolls_back_order_and_version(tmp_path, monkeypatch, action):
    database, models, commands = await setup(tmp_path)
    await database.save_ai_model_chain([model.model_id for model in models], 'default')
    body = observed(database, 'default', models[1], action=action, delta=-1)
    def reject(*args):
        raise RuntimeError('fixture receipt failure')
    monkeypatch.setattr(SettingsCommands, '_save_receipt', staticmethod(reject))
    with pytest.raises(RuntimeError, match='fixture receipt failure'):
        await commands.execute('default', body)
    assert observed(database, 'default', models[1])['expected_revision'] == body['expected_revision']
    assert [entry.model.model_id for entry in await database.list_ai_model_chain('default')] == [model.model_id for model in models]


@pytest.mark.asyncio
async def test_missing_entry_and_invalid_shift_leave_chain_unchanged(tmp_path):
    database, models, commands = await setup(tmp_path)
    for action in ('remove', 'shift'):
        with pytest.raises(AiError):
            await commands.execute('default', observed(database, 'default', models[0], action=action, delta=1))
    for delta in (True, 1.5, 'wrong', None):
        with pytest.raises(WorkCommandError) as failure:
            await commands.execute('default', observed(database, 'default', models[0], action='shift', delta=delta))
        assert failure.value.code == 'CHAIN_SHIFT_INVALID'
    assert await database.list_ai_model_chain('default') == ()
