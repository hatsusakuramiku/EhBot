"""Credential CAS, atomic receipts and exactly one nonpersistent mint response."""
import asyncio
import json
import sqlite3
from threading import Event

import pytest

from app.review.commands import WorkCommandError
from app.archive.service import ArchiveSettingsService
from app.db.database import Database
from app.settings.credential_commands import CredentialCommands
from tests.integration.test_settings_commands import setup


async def observed(database, action, key='credential', **values):
    scope = 'api-key' if action.startswith('api-key-') else 'vault'
    with database.connection() as connection:
        revision = connection.execute('SELECT revision FROM settings_versions WHERE scope = ?', (scope,)).fetchone()[0]
    if action == 'delete':
        item = (await database.list_archive_passwords())[0]
        values.update(password_id=item.password_id, password_uuid=item.password_uuid)
        revision = item.revision
    return dict(expected_revision=revision, idempotency_key=key, **values)


@pytest.mark.asyncio
async def test_same_mint_key_has_one_plaintext_return_and_restart_replay_never_rotates(tmp_path):
    database, _, archive, _ = await setup(tmp_path)
    a, b = CredentialCommands(database, archive), CredentialCommands(database, archive)
    body = await observed(database, 'api-key-generate')
    first, second = await asyncio.gather(a.execute('api-key-generate', body), b.execute('api-key-generate', body))
    assert first[0] == second[0] and sum(token is not None for _, token in (first, second)) == 1
    token = next(token for _, token in (first, second) if token)
    stored = await database.active_api_key()
    assert stored.public_id == token.split('.')[0]
    restarted = Database(tmp_path / 'settings.db')
    await restarted.initialize()
    replay = CredentialCommands(restarted, ArchiveSettingsService(restarted, tmp_path))
    assert await replay.execute('api-key-generate', body) == (first[0], None)
    with database.connection() as connection:
        receipts = str(connection.execute('SELECT payload_hash, result_json FROM settings_command_receipts').fetchall())
        assert token not in receipts and token.split('.')[1] not in receipts and stored.secret_hash not in receipts
        assert connection.execute("SELECT count(*) FROM api_credentials WHERE kind = 'api_key'").fetchone() == (1,)


@pytest.mark.asyncio
async def test_two_device_mints_have_one_winner_and_unrelated_vault_is_independent(tmp_path):
    database, _, archive, _ = await setup(tmp_path)
    commands = CredentialCommands(database, archive)
    body = await observed(database, 'api-key-generate')
    vault = await observed(database, 'add', key='password', name='fixture', password='fixture-password')
    result = await asyncio.gather(*(commands.execute('api-key-generate', {**body, 'idempotency_key': key})
                                    for key in ('first', 'second')), return_exceptions=True)
    assert sum(isinstance(row, tuple) for row in result) == 1
    assert [row.code for row in result if isinstance(row, WorkCommandError)] == ['REVISION_CONFLICT']
    await commands.execute('add', vault)
    assert [(entry.password_id, entry.password) for entry in await archive.password_attempts()] == [
        ((await database.list_archive_passwords())[0].password_id, 'fixture-password')]
    # Health and authentication timestamps must not change edit conditions.
    versions = await observed(database, 'add', key='later')
    item = (await database.list_archive_passwords())[0]
    await database.mark_archive_password_success(item.password_id, password_uuid=item.password_uuid, expected_revision=item.revision)
    assert (await observed(database, 'add', key='later')) == versions
    active = await database.active_api_key()
    key_version = await observed(database, 'api-key-revoke')
    await database.touch_api_credential(active.id)
    assert await observed(database, 'api-key-revoke') == key_version


@pytest.mark.asyncio
@pytest.mark.parametrize('action', CredentialCommands.ACTIONS)
async def test_receipt_failure_rolls_back_secret_write_and_version(tmp_path, action):
    database, _, archive, _ = await setup(tmp_path)
    await archive.add_password(name='fixture', password='original-password', priority=100)
    await database.replace_api_key(label='fixture', public_id='fixture-public', secret_hash='fixture-hash')
    commands = CredentialCommands(database, archive)
    body = await observed(database, action, name='fixture', password='replacement-password')
    with database.connection() as connection:
        previous = {table: connection.execute(f'SELECT * FROM {table}').fetchall()
                    for table in ('archive_passwords', 'api_credentials', 'settings_versions')}
        connection.execute("CREATE TRIGGER fail_credential BEFORE INSERT ON settings_command_receipts BEGIN SELECT RAISE(ABORT, 'fixture'); END")
    with pytest.raises(sqlite3.IntegrityError):
        await commands.execute(action, body)
    with database.connection() as connection:
        for table, rows in previous.items():
            assert connection.execute(f'SELECT * FROM {table}').fetchall() == rows


@pytest.mark.asyncio
async def test_delete_keeps_original_identity_and_cannot_remove_reused_number(tmp_path):
    database, _, archive, _ = await setup(tmp_path)
    commands = CredentialCommands(database, archive)
    await archive.add_password(name='fixture', password='original-password', priority=100)
    body = await observed(database, 'delete')
    await archive.delete_password(body['password_id'])
    replacement = await archive.add_password(name='fixture', password='replacement-password', priority=100)
    assert replacement == body['password_id']
    with pytest.raises(WorkCommandError) as error:
        await commands.execute('delete', body)
    assert error.value.status_code == 410
    fresh = await observed(database, 'delete', key='new-delete')
    receipt = await commands.execute('delete', fresh)
    await archive.add_password(name='fixture', password='next-password', priority=100)
    assert await commands.execute('delete', fresh) == receipt
    assert len(await archive.password_attempts()) == 1


@pytest.mark.asyncio
async def test_upsert_consumes_collection_version_and_does_not_replay_password(tmp_path):
    database, _, archive, _ = await setup(tmp_path)
    commands = CredentialCommands(database, archive)
    first = await observed(database, 'add', name='fixture', password='first-password', priority='5', enabled='on')
    original, secret = await commands.execute('add', first)
    assert secret is None
    second = await observed(database, 'add', key='update', name='fixture', password='second-password', priority=6, enabled=True)
    updated, _ = await commands.execute('add', second)
    assert original['password_uuid'] == updated['password_uuid'] and updated['password_revision'] > original['password_revision']
    assert await commands.execute('add', first) == (original, None)
    assert (await archive.password_attempts())[0].password == 'second-password'
    with pytest.raises(WorkCommandError) as error:
        await commands.execute('add', {**first, 'password': 'changed-password'})
    assert error.value.code == 'IDEMPOTENCY_CONFLICT'
    with database.connection() as connection:
        receipts = json.dumps(connection.execute('SELECT result_json FROM settings_command_receipts').fetchall())
        assert 'first-password' not in receipts and 'second-password' not in receipts


@pytest.mark.asyncio
async def test_preparation_outside_lock_rechecks_version_and_keeps_key_on_revoke_replay(tmp_path, monkeypatch):
    database, _, archive, _ = await setup(tmp_path)
    commands = CredentialCommands(database, archive)
    mint = await observed(database, 'api-key-generate')
    receipt, token = await commands.execute('api-key-generate', mint)
    revoke = await observed(database, 'api-key-revoke', key='revoke')
    revoked, _ = await commands.execute('api-key-revoke', revoke)
    newer, new_token = await commands.execute('api-key-generate', await observed(database, 'api-key-generate', key='new'))
    assert await commands.execute('api-key-revoke', revoke) == (revoked, None)
    assert (await database.active_api_key()).public_id == newer['public_id']
    original = commands._commit
    def late(*args):
        # A separate committed edit between preparation and final CAS.
        database._revoke_active_api_key_sync()
        return original(*args)
    monkeypatch.setattr(commands, '_commit', late)
    with pytest.raises(WorkCommandError) as error:
        await commands.execute('api-key-generate', await observed(database, 'api-key-generate', key='late'))
    assert error.value.code == 'REVISION_CONFLICT'


@pytest.mark.asyncio
@pytest.mark.parametrize('replacement', ['edit', 'delete-reuse'])
async def test_delayed_archive_success_does_not_promote_changed_password(tmp_path, replacement):
    database, _, archive, _ = await setup(tmp_path)
    original_id = await archive.add_password(name='fixture', password='old-password', priority=100)
    original = (await archive.password_attempts())[0]
    if replacement == 'delete-reuse':
        await archive.delete_password(original_id)
    current_id = await archive.add_password(name='fixture', password='new-password', priority=100)
    assert current_id == original_id
    await archive.mark_password_success(original)
    current = (await database.list_archive_passwords())[0]
    assert current.last_success_at is None
    attempts = await archive.password_attempts()
    assert attempts[0].password == 'new-password' and 'new-password' not in repr(attempts[0])
    await archive.mark_password_success(attempts[0])
    assert (await database.list_archive_passwords())[0].last_success_at is not None


@pytest.mark.asyncio
async def test_cancelled_mint_response_drains_commit_and_replay_only_returns_receipt(tmp_path, monkeypatch):
    database, _, archive, _ = await setup(tmp_path)
    commands = CredentialCommands(database, archive)
    body = await observed(database, 'api-key-generate')
    committed, release = Event(), Event()
    commit = commands._commit
    def delayed(*args):
        result = commit(*args)
        committed.set()
        assert release.wait(5)
        return result
    monkeypatch.setattr(commands, '_commit', delayed)
    task = asyncio.create_task(commands.execute('api-key-generate', body))
    try:
        assert await asyncio.to_thread(committed.wait, 3)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    active = await database.active_api_key()
    receipt, plaintext = await CredentialCommands(database, archive).execute('api-key-generate', body)
    assert plaintext is None and receipt['public_id'] == active.public_id
    assert (await database.active_api_key()).public_id == active.public_id


@pytest.mark.asyncio
async def test_conversion_passes_original_password_identity_to_success_write(tmp_path, monkeypatch):
    from dataclasses import replace
    from app.archive.processor import ArchiveProcessor
    from app.conversion.service import ConversionService
    from tests.integration.test_archive_workflow import seed_downloaded_archive, write_zip

    database = Database(tmp_path / 'conversion.db')
    source = tmp_path / 'work' / 'downloads' / 'fixture.zip'
    write_zip(source, ('01.jpg',))
    candidate_id = await seed_downloaded_archive(database, source)
    archive = ArchiveSettingsService(database, tmp_path / 'data')
    password_id = await archive.add_password(name='fixture', password='old-password', priority=100)
    original_process = ArchiveProcessor.process
    def delayed_success(processor, *args, **kwargs):
        result = original_process(processor, *args, **kwargs)
        # The unpacker used the captured password; an administrator edits it
        # before the asynchronous completion reaches the success write.
        with database.connection() as connection:
            connection.execute('UPDATE archive_passwords SET secret_json = ? WHERE id = ?', ('replacement-envelope', password_id))
        return replace(result, password_id=password_id)
    monkeypatch.setattr(ArchiveProcessor, 'process', delayed_success)
    conversion = ConversionService(database, tmp_path / 'work', tmp_path / 'library', data_path=tmp_path / 'data')
    await conversion.enqueue_for_candidate(candidate_id)
    assert await conversion._process_one()
    assert (await archive.passwords())[0].last_success_at is None
