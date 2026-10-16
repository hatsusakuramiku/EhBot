"""Real per-rule SQLite races, identity reuse and durable command receipts."""
import asyncio
import sqlite3

import pytest

from app.db.database import Database
from app.review.commands import WorkCommandError
from app.settings.rule_commands import RuleCommands


CONDITION = {'kind': 'condition', 'field': 'Title', 'operator': 'LIKE', 'value': '%example%'}


def creation(domain, name='one', key='create'):
    return {'name': name, 'priority': 100, 'enabled': True, 'condition': CONDITION,
            'path_template': 'Books/{title}', 'expected_revision': 0, 'idempotency_key': key}


def observed(result, key, **values):
    return {'rule_uuid': result['rule_uuid'], 'expected_revision': result['revision'], 'idempotency_key': key, **values}


async def setup(tmp_path):
    database = Database(tmp_path / 'rules.db')
    await database.initialize()
    return database, RuleCommands(database)


@pytest.mark.asyncio
@pytest.mark.parametrize('domain', ['auto-approval', 'paths'])
async def test_competing_edit_and_toggle_have_one_winner_and_unrelated_rule_can_save(tmp_path, domain):
    database, commands = await setup(tmp_path)
    first = await commands.execute(domain, 'save', creation(domain))
    other = await commands.execute(domain, 'save', creation(domain, 'two', 'create-other'))
    edit = {**creation(domain, 'edited', 'edit'), **observed(first, 'edit'),
            ('rule_id' if domain == 'auto-approval' else 'path_rule_id'): first['rule_id']}
    results = await asyncio.gather(commands.execute(domain, 'save', edit),
        commands.execute(domain, 'toggle', observed(first, 'toggle', enabled=False), rule_id=first['rule_id']),
        return_exceptions=True)
    assert sum(isinstance(item, dict) for item in results) == 1
    assert [item.code for item in results if isinstance(item, WorkCommandError)] == ['REVISION_CONFLICT']
    await commands.execute(domain, 'toggle', observed(other, 'other', enabled=False), rule_id=other['rule_id'])


@pytest.mark.asyncio
@pytest.mark.parametrize('domain', ['auto-approval', 'paths'])
async def test_concurrent_same_key_and_restart_replay_after_delete_and_number_reuse(tmp_path, domain):
    database, commands = await setup(tmp_path)
    payload = creation(domain)
    first, replay = await asyncio.gather(*(commands.execute(domain, 'save', payload) for _ in range(2)))
    assert first == replay
    deletion = observed(first, 'delete')
    deleted = await commands.execute(domain, 'delete', deletion, rule_id=first['rule_id'])
    new = await commands.execute(domain, 'save', creation(domain, 'new', 'new'))
    assert new['rule_id'] == first['rule_id'] and new['rule_uuid'] != first['rule_uuid']
    database, commands = await setup(tmp_path)
    assert await commands.execute(domain, 'delete', deletion, rule_id=first['rule_id']) == deleted
    assert await commands.execute(domain, 'save', payload) == first
    with pytest.raises(WorkCommandError) as failure:
        await commands.execute(domain, 'toggle', observed(first, 'stale', enabled=False), rule_id=first['rule_id'])
    assert failure.value.status_code == 410
    with pytest.raises(WorkCommandError) as failure:
        await commands.execute(domain, 'save', {**payload, 'name': 'changed'})
    assert failure.value.code == 'IDEMPOTENCY_CONFLICT'


@pytest.mark.asyncio
@pytest.mark.parametrize('domain', ['auto-approval', 'paths'])
async def test_receipt_failure_rolls_back_rule_version_and_identity(tmp_path, domain, monkeypatch):
    database, commands = await setup(tmp_path)
    first = await commands.execute(domain, 'save', creation(domain))
    save = RuleCommands._save_receipt

    def fail(*args):
        save(*args)
        raise RuntimeError('Fixture failure after receipt')

    monkeypatch.setattr(RuleCommands, '_save_receipt', staticmethod(fail))
    with pytest.raises(RuntimeError):
        await commands.execute(domain, 'toggle', observed(first, 'toggle', enabled=False), rule_id=first['rule_id'])
    table = 'auto_approval_rules' if domain == 'auto-approval' else 'archive_path_rules'
    with database.connection() as connection:
        assert connection.execute(f'SELECT enabled, version, rule_uuid FROM {table}').fetchone() == (
            1, first['revision'], first['rule_uuid'])
        assert connection.execute('SELECT count(*) FROM rule_command_receipts').fetchone() == (1,)


@pytest.mark.asyncio
@pytest.mark.parametrize('domain', ['auto-approval', 'paths'])
async def test_internal_toggle_advances_edit_version_and_uuid_is_immutable(tmp_path, domain):
    database, commands = await setup(tmp_path)
    first = await commands.execute(domain, 'save', creation(domain))
    toggle = database.set_auto_approval_rule_enabled if domain == 'auto-approval' else database.set_archive_path_rule_enabled
    await toggle(first['rule_id'], False)
    with pytest.raises(WorkCommandError) as failure:
        await commands.execute(domain, 'delete', observed(first, 'delete'), rule_id=first['rule_id'])
    assert failure.value.code == 'REVISION_CONFLICT'
    table = 'auto_approval_rules' if domain == 'auto-approval' else 'archive_path_rules'
    with pytest.raises(sqlite3.IntegrityError), database.connection() as connection:
        connection.execute(f"UPDATE {table} SET rule_uuid = 'replacement'")


@pytest.mark.asyncio
async def test_missing_identity_revision_or_key_and_invalid_template_write_nothing(tmp_path):
    database, commands = await setup(tmp_path)
    for data in ({}, {'expected_revision': True, 'idempotency_key': 'bad'}, {'expected_revision': 0}):
        with pytest.raises(WorkCommandError) as failure:
            await commands.execute('paths', 'save', {**creation('paths'), **data,
                'expected_revision': data.get('expected_revision'), 'idempotency_key': data.get('idempotency_key')})
        assert failure.value.status_code == 428
    with pytest.raises(WorkCommandError):
        await commands.execute('paths', 'save', {**creation('paths'), 'path_template': '../{title}'})
    assert await database.list_archive_path_rules() == ()


@pytest.mark.asyncio
@pytest.mark.parametrize('domain', ['auto-approval', 'paths'])
async def test_unchanged_toggle_consumes_revision_and_deletion_receipt_failure_restores_rule(tmp_path, domain, monkeypatch):
    database, commands = await setup(tmp_path)
    first = await commands.execute(domain, 'save', creation(domain))
    payload = observed(first, 'unchanged', enabled=True)
    second, replay = await asyncio.gather(*(commands.execute(domain, 'toggle', payload, rule_id=first['rule_id']) for _ in range(2)))
    assert second == replay and second['revision'] == first['revision'] + 1
    with pytest.raises(WorkCommandError) as failure:
        await commands.execute(domain, 'delete', observed(first, 'stale'), rule_id=first['rule_id'])
    assert failure.value.code == 'REVISION_CONFLICT'
    save = RuleCommands._save_receipt

    def fail(*args):
        save(*args)
        raise RuntimeError('Fixture deletion receipt failure')

    monkeypatch.setattr(RuleCommands, '_save_receipt', staticmethod(fail))
    with pytest.raises(RuntimeError):
        await commands.execute(domain, 'delete', observed(second, 'delete'), rule_id=first['rule_id'])
    get = database.get_auto_approval_rule if domain == 'auto-approval' else database.get_archive_path_rule
    current = await get(first['rule_id'])
    assert current.rule_uuid == first['rule_uuid'] and current.version == second['revision']
