"""Real command transactions for AI keys/models, including secret-safe replay."""
import asyncio
import json

import pytest

from app.archive.vault import decrypt_password
from app.review.commands import WorkCommandError
from app.settings.ai_commands import AiCatalogueCommands, AiProviderCommands
from app.settings.commands import SettingsCommands
from tests.integration.test_settings_commands import setup


async def catalogue(tmp_path):
    database, _, archive, _ = await setup(tmp_path)
    provider = await AiProviderCommands(database, archive).execute('save', {
        'name': 'fixture', 'base_url': 'https://fixture.invalid/v1', 'enabled': True,
        'expected_revision': 1, 'idempotency_key': 'provider'})
    return database, archive, provider, AiCatalogueCommands(database, archive)


async def observed(database, provider, action, *, key='operation', item_id=None, **values):
    current = await database.get_ai_provider(provider['provider_id'])
    if action in ('keys-add', 'models-add'):
        return dict(provider_uuid=current.provider_uuid, expected_revision=(current.keys_revision
                    if action == 'keys-add' else current.models_revision), idempotency_key=key, **values)
    if action.startswith('key-'):
        item = next(row for row in await database.list_ai_provider_keys(current.provider_id) if row.key_id == item_id)
        return dict(key_uuid=item.key_uuid, expected_revision=item.revision,
                    expected_health_revision=item.health_revision, idempotency_key=key, **values)
    item = await database.get_ai_provider_model(item_id)
    return dict(model_uuid=item.model_uuid, expected_revision=item.revision, idempotency_key=key, **values)


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['keys-add', 'models-add'])
async def test_addition_concurrency_fixed_replay_and_safe_receipts(tmp_path, action):
    database, archive, provider, commands = await catalogue(tmp_path)
    values = {'api_keys': 'primary:fixture-secret\nbackup:other-fixture'} if action == 'keys-add' else {'model_name': ['one', 'two', 'one']}
    body = await observed(database, provider, action, **values)
    results = await asyncio.gather(*(commands.execute(action, provider['provider_id'], {**body, 'idempotency_key': key})
                                    for key in ('device-a', 'device-b')), return_exceptions=True)
    assert sum(isinstance(item, dict) for item in results) == 1
    assert [item.code for item in results if isinstance(item, WorkCommandError)] == ['REVISION_CONFLICT']
    winner = next(index for index, item in enumerate(results) if isinstance(item, dict))
    request = {**body, 'idempotency_key': ('device-a', 'device-b')[winner]}
    assert len(results[winner]['entries']) == 2
    assert await commands.execute(action, provider['provider_id'], request) == results[winner]
    if action == 'keys-add':
        keys = await database.list_ai_provider_keys(provider['provider_id'])
        assert len(keys) == 2
        assert decrypt_password(await archive.master_key(), await database.ai_provider_key_cipher(keys[0].key_id)) == 'fixture-secret'
    else:
        assert len(await database.list_ai_provider_models(provider['provider_id'])) == 2
    with database.connection() as connection:
        receipts = connection.execute('SELECT payload_hash, result_json FROM settings_command_receipts').fetchall()
    assert 'fixture-secret' not in json.dumps(receipts) and 'other-fixture' not in json.dumps(receipts)
    assert 'cipher' not in json.dumps(receipts) and 'base_url' not in json.dumps(receipts)


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['key-toggle', 'key-delete', 'model-toggle', 'model-params', 'model-delete'])
async def test_item_cas_replay_and_reused_numeric_identity(tmp_path, action):
    database, _, provider, commands = await catalogue(tmp_path)
    if action.startswith('key-'):
        item = await database.add_ai_provider_key(provider['provider_id'], label='', cipher='fixture-envelope')
        item_id, kind = item.key_id, 'key'
    else:
        item = await database.add_ai_provider_model(provider['provider_id'], 'fixture-model')
        item_id, kind = item.model_id, 'model'
    body = await observed(database, provider, action, item_id=item_id,
                          enabled=False, params='{"max_tokens":64}')
    results = await asyncio.gather(*(commands.execute(action, item_id, {**body, 'idempotency_key': key})
                                    for key in ('a', 'b')), return_exceptions=True)
    assert sum(isinstance(item, dict) for item in results) == 1
    assert all(item.status_code in (409, 410) for item in results if isinstance(item, WorkCommandError))
    winner = next(index for index, item in enumerate(results) if isinstance(item, dict))
    request = {**body, 'idempotency_key': ('a', 'b')[winner]}
    if not action.endswith('-delete'):
        if kind == 'key':
            await database.delete_ai_provider_key(item_id)
        else:
            await database.delete_ai_provider_model(item_id)
    if kind == 'key':
        replacement = await database.add_ai_provider_key(provider['provider_id'], label='', cipher='other-envelope')
        assert replacement.key_id == item_id and replacement.key_uuid != item.key_uuid
    else:
        replacement = await database.add_ai_provider_model(provider['provider_id'], 'replacement')
        assert replacement.model_id == item_id and replacement.model_uuid != item.model_uuid
    assert await commands.execute(action, item_id, request) == results[winner]
    with pytest.raises(WorkCommandError) as failure:
        await commands.execute(action, item_id, {**request, 'idempotency_key': 'stale-original'})
    assert failure.value.status_code == 410


@pytest.mark.asyncio
async def test_key_reset_health_cas_does_not_consume_configuration_version(tmp_path):
    database, _, provider, commands = await catalogue(tmp_path)
    item = await database.add_ai_provider_key(provider['provider_id'], label='', cipher='fixture-envelope')
    stale = await observed(database, provider, 'key-reset', item_id=item.key_id)
    before = await database.get_ai_provider(provider['provider_id'])
    await database.record_ai_key_failure(item.key_id, cooldown_minutes=10)
    with pytest.raises(WorkCommandError) as failure:
        await commands.execute('key-reset', item.key_id, stale)
    assert failure.value.code == 'HEALTH_REVISION_CONFLICT'
    body = await observed(database, provider, 'key-reset', item_id=item.key_id, key='reset')
    first = await commands.execute('key-reset', item.key_id, body)
    current = (await database.list_ai_provider_keys(provider['provider_id']))[0]
    assert current.cooldown_until is None and current.revision == item.revision
    assert current.health_revision == first['health_revision'] > body['expected_health_revision']
    await database.record_ai_key_failure(item.key_id, cooldown_minutes=10)
    assert await commands.execute('key-reset', item.key_id, body) == first
    assert (await database.list_ai_provider_keys(provider['provider_id']))[0].cooldown_until is not None
    assert await database.get_ai_provider(provider['provider_id']) == before


@pytest.mark.asyncio
@pytest.mark.parametrize('action', AiCatalogueCommands.ACTIONS)
async def test_receipt_failure_rolls_back_all_catalogue_changes(tmp_path, monkeypatch, action):
    database, _, provider, commands = await catalogue(tmp_path)
    item_key = await database.add_ai_provider_key(provider['provider_id'], label='', cipher='fixture-envelope')
    model = await database.add_ai_provider_model(provider['provider_id'], 'fixture-model')
    await database.record_ai_key_failure(item_key.key_id, cooldown_minutes=10)
    await database.save_ai_model_chain([model.model_id])
    item_id = item_key.key_id if action.startswith('key-') else model.model_id
    target = provider['provider_id'] if action in ('keys-add', 'models-add') else item_id
    body = await observed(database, provider, action, item_id=item_id, api_keys='fixture-secret',
                          model_name=['new-model'], enabled=False, params='{"max_tokens":64}')
    original_provider = await database.get_ai_provider(provider['provider_id'])
    original_keys = await database.list_ai_provider_keys(provider['provider_id'])
    original_models = await database.list_ai_provider_models(provider['provider_id'])
    original_chain = await database.list_ai_model_chain()

    def fail(*args):
        raise RuntimeError('fixture receipt failure')

    monkeypatch.setattr(SettingsCommands, '_save_receipt', staticmethod(fail))
    with pytest.raises(RuntimeError):
        await commands.execute(action, target, body)
    assert await database.get_ai_provider(provider['provider_id']) == original_provider
    assert await database.list_ai_provider_keys(provider['provider_id']) == original_keys
    assert await database.list_ai_provider_models(provider['provider_id']) == original_models
    assert await database.list_ai_model_chain() == original_chain
