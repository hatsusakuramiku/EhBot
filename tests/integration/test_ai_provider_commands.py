"""Provider CAS, fixed receipts and replacement identities on real SQLite."""
import asyncio
import json

import pytest

from app.review.commands import WorkCommandError
from app.settings.ai_commands import AiProviderCommands
from app.settings.commands import SettingsCommands
from tests.integration.test_settings_commands import setup


def values(key, revision=1, **extra):
    return dict(name='fixture-provider', base_url='https://fixture.invalid/v1', enabled=True,
                expected_revision=revision, idempotency_key=key, **extra)


@pytest.mark.asyncio
async def test_create_and_edit_concurrency_replay_independence_and_private_digest(tmp_path):
    database, _, archive, settings = await setup(tmp_path)
    commands = AiProviderCommands(database, archive)
    body = values('create')
    first, replay = await asyncio.gather(commands.execute('save', body), commands.execute('save', body))
    assert first == replay and first['revision'] == 1
    with pytest.raises(WorkCommandError) as failure:
        await commands.execute('save', values('stale-create'))
    assert failure.value.code == 'REVISION_CONFLICT'
    other_values = values('other', (await settings.versions('ai'))['ai-providers'])
    other_values['name'] = 'independent'
    other = await commands.execute('save', other_values)
    edit = {**body, 'provider_id': first['provider_id'], 'provider_uuid': first['provider_uuid']}
    edits = [{**edit, 'name': name, 'idempotency_key': name} for name in ('device-a', 'device-b')]
    results = await asyncio.gather(*(commands.execute('save', item) for item in edits), return_exceptions=True)
    assert sum(isinstance(item, dict) for item in results) == 1
    assert [item.code for item in results if isinstance(item, WorkCommandError)] == ['REVISION_CONFLICT']
    winner = next(index for index, item in enumerate(results) if isinstance(item, dict))
    independent = {**other_values, 'provider_id': other['provider_id'], 'provider_uuid': other['provider_uuid'],
                   'expected_revision': other['revision'], 'idempotency_key': 'independent-save'}
    assert (await commands.execute('save', independent))['revision'] == other['revision'] + 1
    assert await commands.execute('save', edits[winner]) == results[winner]
    with pytest.raises(WorkCommandError) as failure:
        await commands.execute('save', {**edits[winner], 'name': 'different'})
    assert failure.value.code == 'IDEMPOTENCY_CONFLICT'
    with database.connection() as connection:
        receipts = connection.execute('SELECT payload_hash, result_json FROM settings_command_receipts').fetchall()
    assert all(len(row[0]) == 64 and 'https://' not in row[1] for row in receipts)
    # Both real values are private to their respective vaults; receipts carry no config.
    assert all(set(json.loads(row[1])) == {'provider_id', 'provider_uuid', 'revision', 'status',
                                          'action', 'collection_revision', 'keys_revision', 'models_revision'} for row in receipts)


@pytest.mark.asyncio
async def test_deleted_provider_replay_never_edits_reused_id(tmp_path):
    database, _, archive, settings = await setup(tmp_path)
    commands = AiProviderCommands(database, archive)
    body = values('create')
    first = await commands.execute('save', body)
    delete = dict(provider_uuid=first['provider_uuid'], expected_revision=first['revision'],
                  keys_revision=first['keys_revision'], models_revision=first['models_revision'], idempotency_key='delete')
    removed = await commands.execute('delete', delete, provider_id=first['provider_id'])
    replacement = await commands.execute('save', values('replacement', (await settings.versions('ai'))['ai-providers']))
    assert replacement['provider_id'] == first['provider_id'] and replacement['provider_uuid'] != first['provider_uuid']
    assert await commands.execute('save', body) == first
    assert await commands.execute('delete', delete, provider_id=first['provider_id']) == removed
    for action, payload in [('delete', {**delete, 'idempotency_key': 'old-delete'}),
                            ('save', {**body, 'provider_id': first['provider_id'],
                                      'provider_uuid': first['provider_uuid'], 'idempotency_key': 'old-save'})]:
        with pytest.raises(WorkCommandError) as failure:
            await commands.execute(action, payload, provider_id=first['provider_id'])
        assert failure.value.status_code == 410
    assert (await database.get_ai_provider(first['provider_id'])).provider_uuid == replacement['provider_uuid']


@pytest.mark.asyncio
async def test_receipt_failure_rolls_back_create_edit_and_delete(tmp_path, monkeypatch):
    database, _, archive, _ = await setup(tmp_path)
    commands = AiProviderCommands(database, archive)
    first = await commands.execute('save', values('create'))
    original = await database.get_ai_provider(first['provider_id'])

    def fail(*args):
        raise RuntimeError('fixture receipt failure')

    monkeypatch.setattr(SettingsCommands, '_save_receipt', staticmethod(fail))
    edit = {**values('edit'), 'provider_id': first['provider_id'], 'provider_uuid': first['provider_uuid'],
            'name': 'changed'}
    create = {**values('new', first['collection_revision']), 'name': 'second'}
    for action, payload, provider_id in [('save', edit, None), ('save', create, None),
                                       ('delete', {**edit, 'keys_revision': 1, 'models_revision': 1,
                                                   'idempotency_key': 'delete'}, first['provider_id'])]:
        with pytest.raises(RuntimeError):
            await commands.execute(action, payload, provider_id=provider_id)
        assert await database.list_ai_providers() == (original,)
    with database.connection() as connection:
        assert connection.execute("SELECT revision FROM settings_versions WHERE scope = 'ai-providers'").fetchone()[0] == first['collection_revision']


@pytest.mark.asyncio
@pytest.mark.parametrize('child', ['key', 'model'])
async def test_stale_delete_cannot_remove_new_child_configuration(tmp_path, child):
    database, _, archive, _ = await setup(tmp_path)
    commands = AiProviderCommands(database, archive)
    first = await commands.execute('save', values('create'))
    deletion = {**first, 'expected_revision': first['revision'], 'idempotency_key': 'delete'}
    if child == 'key':
        await database.add_ai_provider_key(first['provider_id'], label='new key', cipher='fixture-envelope')
    else:
        model = await database.add_ai_provider_model(first['provider_id'], 'new model')
        await database.set_ai_provider_model_enabled(model.model_id, False)
    provider = await database.get_ai_provider(first['provider_id'])
    assert provider.revision == first['revision']
    with pytest.raises(WorkCommandError) as failure:
        await commands.execute('delete', deletion, provider_id=provider.provider_id)
    assert failure.value.code == 'REVISION_CONFLICT'
    assert await database.get_ai_provider(provider.provider_id) == provider
    assert (await commands.execute('save', {**values('independent-metadata-edit'),
            'provider_id': provider.provider_id, 'provider_uuid': provider.provider_uuid}))['status'] == 'COMPLETED'
