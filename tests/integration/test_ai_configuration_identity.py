"""Late supplier answers must retain the configuration they actually used."""
import asyncio
import sqlite3

import httpx
import pytest

from app.ai.client import AI_AUTH, AiClientError
from app.ai.models import AiRequestParams
from app.ai.service import AiProviderService
from app.archive.service import ArchiveSettingsService
from app.db.database import Database


async def _catalogue(tmp_path, client_factory):
    database = Database(tmp_path / 'catalogue.db')
    await database.initialize()
    settings = ArchiveSettingsService(database, tmp_path / 'work',
        default_library_path=tmp_path / 'library', default_work_path=tmp_path / 'work')
    http = httpx.AsyncClient()
    service = AiProviderService(database, settings, http_client=http,
                                client_factory=client_factory)
    values = dict(name='original', base_url='https://original.invalid/v1', enabled=True)
    provider = await service.save_provider(values)
    key = await service.add_key(provider.provider_id, label='', api_key='fixture-key')
    model = await service.add_model(provider.provider_id, 'fixture-model')
    return database, service, http, values, provider, key, model


@pytest.mark.parametrize('change', ['model-replaced', 'model-params', 'provider-edited',
                                    'provider-replaced', 'key-replaced', 'key-disabled'])
def test_verification_does_not_certify_changed_configuration(tmp_path, change):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        class Client:
            async def complete(self, *args, **kwargs):
                entered.set()
                await release.wait()
                return '{"ok":true}'

        database, service, http, values, provider, key, model = await _catalogue(
            tmp_path, lambda *args, **kwargs: Client())
        try:
            pending = asyncio.create_task(service.verify_model(model.model_id))
            await entered.wait()
            if change == 'model-replaced':
                await service.delete_model(model.model_id)
                replacement = await service.add_model(provider.provider_id, model.name)
                assert replacement.model_id == model.model_id
            elif change == 'model-params':
                await database.set_ai_provider_model_params(model.model_id, AiRequestParams(max_tokens=128))
            elif change == 'provider-edited':
                await service.save_provider(dict(values, provider_id=provider.provider_id,
                                                base_url='https://replacement.invalid/v1'))
            elif change == 'provider-replaced':
                await service.delete_provider(provider.provider_id)
                replacement = await service.save_provider(values)
                assert replacement.provider_id == provider.provider_id
                replacement_model = await service.add_model(replacement.provider_id, model.name)
                assert replacement_model.model_id == model.model_id
            elif change == 'key-replaced':
                await service.delete_key(key.key_id)
                replacement = await service.add_key(provider.provider_id, label='', api_key='replacement-key')
                assert replacement.key_id == key.key_id
            else:
                await service.set_key_enabled(key.key_id, False)
            release.set()
            result = await pending
            assert not result.ok and result.code == 'AI_CONFIGURATION_CHANGED'
            current = await service.model(model.model_id)
            assert current.last_verify_ok is None
            with database.connection() as connection:
                assert connection.execute('PRAGMA foreign_key_check').fetchall() == []
        finally:
            release.set()
            await http.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize('fault', [False, True])
def test_late_key_health_does_not_modify_reused_key_id(tmp_path, fault):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        class Client:
            async def complete(self, *args, **kwargs):
                entered.set()
                await release.wait()
                if fault:
                    raise AiClientError(AI_AUTH, 'fixture refusal', key_fault=True)
                return '{"ok":true}'

        database, service, http, _, provider, key, model = await _catalogue(
            tmp_path, lambda *args, **kwargs: Client())
        try:
            pending = asyncio.create_task(service._ask(provider, model, []))
            await entered.wait()
            await service.delete_key(key.key_id)
            replacement = await service.add_key(provider.provider_id, label='', api_key='replacement-key')
            assert replacement.key_id == key.key_id
            release.set()
            await asyncio.gather(pending, return_exceptions=True)
            current = (await service.keys(provider.provider_id))[0]
            assert current.failures == 0
            assert current.last_used_at is None and current.cooldown_until is None
        finally:
            release.set()
            await http.aclose()

    asyncio.run(scenario())


def test_health_is_independent_of_edits_and_chain_identity(tmp_path):
    async def scenario():
        class Client:
            async def complete(self, *args, **kwargs):
                return '{"ok":true}'

        database, service, http, values, provider, key, model = await _catalogue(
            tmp_path, lambda *args, **kwargs: Client())
        try:
            provider = await service.provider(provider.provider_id)
            assert (await service.verify_model(model.model_id)).ok
            await database.record_ai_key_failure(key.key_id, cooldown_minutes=1)
            await database.reset_ai_key_cooldown(key.key_id)
            await database.mark_ai_key_used(key.key_id)
            assert await service.provider(provider.provider_id) == provider
            assert (await service.keys(provider.provider_id))[0].revision == key.revision
            verified = await service.model(model.model_id)
            assert verified.revision == model.revision
            chain = await service.save_chain([model.model_id])
            assert chain[0].provider == provider and chain[0].model == verified
            assert await service.chain() == chain
            await service.save_provider(dict(values, provider_id=provider.provider_id))
            assert await service.provider(provider.provider_id) == provider
            assert (await service.model(model.model_id)).last_verify_ok is True
            await database.set_ai_provider_model_params(model.model_id, AiRequestParams(max_tokens=64))
            edited = await service.model(model.model_id)
            assert edited.revision == model.revision + 1 and edited.last_verify_ok is None
            assert (await service.verify_model(model.model_id)).ok
            await service.set_key_enabled(key.key_id, False)
            edited_provider = await service.provider(provider.provider_id)
            assert edited_provider.revision == provider.revision
            assert edited_provider.keys_revision == provider.keys_revision + 1
            assert (await service.keys(provider.provider_id))[0].revision == key.revision + 1
            assert (await service.model(model.model_id)).last_verify_ok is None
            for table, column in [('ai_providers', 'provider_uuid'),
                                  ('ai_provider_keys', 'key_uuid'),
                                  ('ai_provider_models', 'model_uuid')]:
                with pytest.raises(sqlite3.IntegrityError), database.connection() as connection:
                    connection.execute(f'UPDATE {table} SET {column} = ?', ('replacement',))
        finally:
            await http.aclose()

    asyncio.run(scenario())


def test_key_selection_cannot_read_replacement_credentials(tmp_path, monkeypatch):
    async def scenario():
        called = []

        class Client:
            async def complete(self, *args, **kwargs):
                called.append(True)
                return '{"ok":true}'

        database, service, http, _, provider, key, model = await _catalogue(
            tmp_path, lambda *args, **kwargs: Client())
        try:
            ordered = service._ordered_keys

            async def replaced(provider_id):
                keys = await ordered(provider_id)
                await service.delete_key(key.key_id)
                new = await service.add_key(provider_id, label='', api_key='replacement-key')
                assert new.key_id == key.key_id
                return keys

            monkeypatch.setattr(service, '_ordered_keys', replaced)
            result = await service.verify_model(model.model_id)
            assert result.code == 'AI_CONFIGURATION_CHANGED' and not called
            assert await service._key_plaintext(key.key_id, expected=key, provider=provider) is None
        finally:
            await http.aclose()

    asyncio.run(scenario())
