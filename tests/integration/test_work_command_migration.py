"""Real 038 upgrades preserve work identity and roll back failed 039 DDL."""
import asyncio
from pathlib import Path
import shutil
import sqlite3

import pytest

import app.db.database as database_module
from app.db.database import Database


def test_upgrade_from_038_is_atomic_and_keeps_work_uuids_after_repeat_initialize(tmp_path, monkeypatch):
    real_migrations = Path(database_module.__file__).with_name('migrations')
    fixture_module = tmp_path / 'legacy' / 'database.py'
    migrations = fixture_module.with_name('migrations')
    migrations.mkdir(parents=True)
    for path in real_migrations.glob('*.sql'):
        if int(path.name.split('_')[0]) <= 38:
            shutil.copy2(path, migrations / path.name)
    monkeypatch.setattr(database_module, '__file__', str(fixture_module))
    database = Database(tmp_path / 'works.db')
    asyncio.run(database.initialize())
    with database.connection() as connection:
        assert connection.execute('SELECT max(version) FROM schema_migrations').fetchone() == (38,)
        connection.execute("INSERT INTO candidates(id, status) VALUES(1, 'PENDING_REVIEW')")
        connection.execute("INSERT INTO ingest_reference_changes(operation_id, candidate_id, gallery_id, gallery_token, target_key) "
                           "VALUES('legacy-reference', 1, 123, 'fixture', 'eh:123')")
        connection.execute("INSERT INTO system_settings(key, value) VALUES('timezone', 'Asia/Tokyo')")
        connection.execute("INSERT INTO system_settings(key, value) VALUES('ai_enabled', '0')")
        connection.execute("INSERT INTO system_settings(key, value) VALUES('ai_candidate_model_source', 'custom')")
        for key, value in {'path_source': 'ai', 'ai_prompt': 'legacy prompt', 'ai_batch_size': '31',
                           'ai_concurrency': '3', 'ai_fallback_to_rules': '1', 'ai_stream': '1',
                           'ai_default_include_current': '1', 'auto_series': '1', 'ai_model_source': 'custom'}.items():
            connection.execute('INSERT INTO archive_settings(key, value) VALUES(?, ?)', (key, value))
        connection.execute("INSERT INTO archive_settings(key, value) VALUES('library_template', '{title}')")
        connection.execute("INSERT INTO archive_settings(key, value) VALUES('torrent_client_url', 'http://legacy.invalid')")
        connection.execute("INSERT INTO ai_providers(id, name, base_url) VALUES(7, 'legacy-ai', 'https://legacy.invalid/v1')")
        connection.execute("INSERT INTO ai_provider_keys(id, provider_id, label, cipher, failures) VALUES(7, 7, 'legacy-key', 'fixture-envelope', 3)")
        connection.execute("INSERT INTO ai_provider_models(id, provider_id, name, last_verify_ok) VALUES(7, 7, 'legacy-model', 1)")
        connection.execute("INSERT INTO ai_model_chain(scope, position, provider_model_id) VALUES('default', 0, 7)")
        connection.execute("INSERT INTO archive_passwords(id, name, secret_json, priority, enabled, last_success_at) "
                           "VALUES(7, 'legacy-password', 'fixture-password-envelope', 9, 1, '2026-01-01')")
        connection.execute("INSERT INTO api_credentials(kind, public_id, secret_hash, label) "
                           "VALUES('api_key', 'legacy-public', 'fixture-key-hash', 'legacy')")
        connection.execute("INSERT INTO ai_path_suggestions(candidate_id, fingerprint, prompt_hash, relative_path, directory, filename, model_name) "
                           "VALUES(1, 'legacy-f', 'legacy-p', 'Legacy.cbz', '', 'Legacy', 'legacy-model')")
        for table in ('auto_approval_rules', 'archive_path_rules'):
            path_column = ', path_template' if table == 'archive_path_rules' else ''
            path_value = ", 'Books/{title}'" if path_column else ''
            connection.execute(f"INSERT INTO {table}(id, name, enabled, version, condition_json, dsl_snapshot{path_column}) "
                               f"VALUES(7, 'legacy', 0, 4, '{{}}', 'legacy'{path_value})")
    source = real_migrations / '039_work_commands.sql'
    migration = migrations / source.name
    migration.write_text(source.read_text(encoding='utf-8') + '\nSELECT missing_fixture_table FROM missing_fixture_table;\n',
                         encoding='utf-8')
    with pytest.raises(sqlite3.OperationalError):
        asyncio.run(database.initialize())
    with database.connection() as connection:
        assert connection.execute('SELECT max(version) FROM schema_migrations').fetchone() == (38,)
        assert connection.execute("SELECT name FROM sqlite_master WHERE name IN "
            "('work_versions', 'work_review_batches', 'work_review_refusals', 'work_version_created', "
            "'settings_versions', 'settings_field_scopes', 'settings_command_receipts', 'settings_system_insert', "
            "'toolchain_version', 'toolchain_operations', 'toolchain_single_install', "
            "'ai_network_operations', 'ai_network_claims')").fetchall() == []
        assert connection.execute('SELECT status FROM candidates WHERE id = 1').fetchone() == ('PENDING_REVIEW',)
        assert 'command_guard_json' not in [row[1] for row in connection.execute('PRAGMA table_info(ingest_reference_changes)')]
        assert connection.execute('SELECT gallery_id, status FROM ingest_reference_changes').fetchone() == (123, 'PENDING')
        for table in ('auto_approval_rules', 'archive_path_rules'):
            assert 'rule_uuid' not in [row[1] for row in connection.execute(f'PRAGMA table_info({table})')]
            assert connection.execute(f'SELECT version, enabled FROM {table} WHERE id = 7').fetchone() == (4, 0)
        assert 'profile_uuid' not in [row[1] for row in connection.execute('PRAGMA table_info(archive_tool_profiles)')]
        for table in ('ai_providers', 'ai_provider_keys', 'ai_provider_models'):
            assert 'revision' not in [row[1] for row in connection.execute(f'PRAGMA table_info({table})')]
        assert 'health_revision' not in [row[1] for row in connection.execute('PRAGMA table_info(ai_provider_keys)')]
        assert 'password_uuid' not in [row[1] for row in connection.execute('PRAGMA table_info(archive_passwords)')]
        assert connection.execute('SELECT secret_json, last_success_at FROM archive_passwords WHERE id = 7').fetchone() == ('fixture-password-envelope', '2026-01-01')
        assert connection.execute('SELECT failures, cipher FROM ai_provider_keys WHERE id = 7').fetchone() == (3, 'fixture-envelope')
    shutil.copy2(source, migration)
    asyncio.run(database.initialize())
    with database.connection() as connection:
        first = connection.execute('SELECT work_id, revision FROM work_versions WHERE candidate_id = 1').fetchone()
        assert first[0] and first[1] == 1
        assert connection.execute('SELECT gallery_id, status, command_guard_json FROM ingest_reference_changes').fetchone() == (123, 'PENDING', None)
        command_guard = '{"target": null}'
        connection.execute('UPDATE ingest_reference_changes SET command_guard_json = ? WHERE operation_id = ?',
                           (command_guard, 'legacy-reference'))
        assert connection.execute("SELECT value FROM system_settings WHERE key = 'timezone'").fetchone() == ('Asia/Tokyo',)
        assert connection.execute("SELECT revision FROM settings_versions WHERE scope = 'system'").fetchone() == (1,)
        assert connection.execute("SELECT revision FROM settings_versions WHERE scope = 'ai-master'").fetchone() == (1,)
        assert connection.execute("SELECT value FROM system_settings WHERE key = 'ai_enabled'").fetchone() == ('0',)
        assert connection.execute("SELECT value FROM system_settings WHERE key = 'ai_candidate_model_source'").fetchone() == ('custom',)
        for scope in ('path-ai', 'path-model-source', 'candidate-model-source'):
            assert connection.execute('SELECT revision FROM settings_versions WHERE scope = ?', (scope,)).fetchone() == (1,)
        for key, value in {'path_source': 'ai', 'ai_prompt': 'legacy prompt', 'ai_batch_size': '31',
                           'ai_concurrency': '3', 'ai_fallback_to_rules': '1', 'ai_stream': '1',
                           'ai_default_include_current': '1', 'auto_series': '1', 'ai_model_source': 'custom'}.items():
            assert connection.execute('SELECT value FROM archive_settings WHERE key = ?', (key,)).fetchone() == (value,)
        assert connection.execute("SELECT value FROM archive_settings WHERE key = 'library_template'").fetchone() == ('{title}',)
        assert connection.execute("SELECT value FROM archive_settings WHERE key = 'torrent_client_url'").fetchone() == ('http://legacy.invalid',)
        assert connection.execute("SELECT revision FROM settings_versions WHERE scope = 'torrent'").fetchone() == (1,)
        assert connection.execute('PRAGMA foreign_key_check').fetchall() == []
        assert connection.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert connection.execute('SELECT revision FROM toolchain_version').fetchone() == (1,)
        assert connection.execute('SELECT count(*) FROM toolchain_operations').fetchone() == (0,)
        assert connection.execute('SELECT count(*) FROM ai_network_operations').fetchone() == (0,)
        assert connection.execute('SELECT count(*) FROM ai_network_claims').fetchone() == (0,)
        password_identity = connection.execute('SELECT password_uuid, revision FROM archive_passwords WHERE id = 7').fetchone()
        assert password_identity[0] and password_identity[1] == 1
        assert connection.execute('SELECT name, secret_json, priority, enabled, last_success_at FROM archive_passwords WHERE id = 7').fetchone() == (
            'legacy-password', 'fixture-password-envelope', 9, 1, '2026-01-01')
        assert connection.execute("SELECT public_id, secret_hash, revoked_at FROM api_credentials WHERE kind = 'api_key'").fetchone() == (
            'legacy-public', 'fixture-key-hash', None)
        for scope in ('vault', 'api-key', 'rearchive', 'path-cache'):
            assert connection.execute('SELECT revision FROM settings_versions WHERE scope = ?', (scope,)).fetchone() == (1,)
        assert connection.execute('SELECT fingerprint, prompt_hash, relative_path FROM ai_path_suggestions').fetchone() == ('legacy-f', 'legacy-p', 'Legacy.cbz')
        connection.execute("UPDATE archive_passwords SET last_success_at = '2026-10-16' WHERE id = 7")
        connection.execute("UPDATE api_credentials SET last_used_at = '2026-10-16' WHERE kind = 'api_key'")
        ai_ids = {table: connection.execute(f'SELECT {column}, revision FROM {table} WHERE id = 7').fetchone()
                  for table, column in [('ai_providers', 'provider_uuid'), ('ai_provider_keys', 'key_uuid'),
                                        ('ai_provider_models', 'model_uuid')]}
        assert all(row[0] and row[1] == 1 for row in ai_ids.values())
        assert connection.execute('SELECT keys_revision FROM ai_providers WHERE id = 7').fetchone() == (1,)
        assert connection.execute("SELECT scope, position, provider_model_id FROM ai_model_chain").fetchall() == [('default', 0, 7)]
        assert connection.execute("SELECT scope, revision FROM settings_versions WHERE scope LIKE 'ai-chain:%' ORDER BY scope").fetchall() == [
            ('ai-chain:archive_path', 1), ('ai-chain:candidate_admission', 1), ('ai-chain:default', 1)]
        assert connection.execute('SELECT last_verify_ok FROM ai_provider_models WHERE id = 7').fetchone() == (1,)
        assert connection.execute('SELECT failures, cipher FROM ai_provider_keys WHERE id = 7').fetchone() == (3, 'fixture-envelope')
        rule_ids = {table: connection.execute(f'SELECT rule_uuid, version, enabled FROM {table} WHERE id = 7').fetchone()
                    for table in ('auto_approval_rules', 'archive_path_rules')}
        assert all(row[0] and row[1:] == (4, 0) for row in rule_ids.values())
        profile_ids = connection.execute('SELECT name, profile_uuid, revision FROM archive_tool_profiles ORDER BY id').fetchall()
        assert all(row[1] and row[2] == 1 for row in profile_ids)
        assert connection.execute('SELECT health_revision FROM ai_provider_keys WHERE id = 7').fetchone() == (1,)
        connection.execute('UPDATE ai_provider_keys SET failures = failures + 1 WHERE id = 7')
        assert connection.execute('SELECT revision, health_revision FROM ai_provider_keys WHERE id = 7').fetchone() == (1, 2)
        connection.execute("INSERT INTO ai_network_operations(operation_id, idempotency_key, payload_hash, action, target_json) "
                           "VALUES('fixture-probe', 'fixture-probe', 'fixture-hash', 'model', '{}')")
        connection.execute("INSERT INTO ai_network_claims(resource, operation_id) VALUES('model:fixture', 'fixture-probe')")
    asyncio.run(database.initialize())
    with database.connection() as connection:
        assert connection.execute('SELECT work_id, revision FROM work_versions WHERE candidate_id = 1').fetchone() == first
        assert connection.execute('SELECT max(version) FROM schema_migrations').fetchone() == (39,)
        assert connection.execute('SELECT gallery_id, status, command_guard_json FROM ingest_reference_changes').fetchone() == (123, 'PENDING', command_guard)
        assert connection.execute('SELECT password_uuid, revision FROM archive_passwords WHERE id = 7').fetchone() == password_identity
        for scope in ('vault', 'api-key', 'rearchive', 'path-cache'):
            assert connection.execute('SELECT revision FROM settings_versions WHERE scope = ?', (scope,)).fetchone() == (1,)
        assert connection.execute('SELECT revision FROM toolchain_version').fetchone() == (1,)
        assert connection.execute("SELECT status FROM ai_network_operations WHERE operation_id = 'fixture-probe'").fetchone() == ('RUNNING',)
        assert connection.execute('SELECT resource, operation_id FROM ai_network_claims').fetchall() == [('model:fixture', 'fixture-probe')]
        assert connection.execute('SELECT health_revision FROM ai_provider_keys WHERE id = 7').fetchone() == (2,)
        for scope in ('path-ai', 'path-model-source', 'candidate-model-source'):
            assert connection.execute('SELECT revision FROM settings_versions WHERE scope = ?', (scope,)).fetchone() == (1,)
        for table, row in rule_ids.items():
            assert connection.execute(f'SELECT rule_uuid, version, enabled FROM {table} WHERE id = 7').fetchone() == row
        assert connection.execute('SELECT name, profile_uuid, revision FROM archive_tool_profiles ORDER BY id').fetchall() == profile_ids
        for table, column in [('ai_providers', 'provider_uuid'), ('ai_provider_keys', 'key_uuid'),
                              ('ai_provider_models', 'model_uuid')]:
            assert connection.execute(f'SELECT {column}, revision FROM {table} WHERE id = 7').fetchone() == ai_ids[table]
    asyncio.run(database.delete_ai_provider_model(7))
    with database.connection() as connection:
        assert connection.execute("SELECT revision FROM settings_versions WHERE scope = 'ai-chain:default'").fetchone() == (2,)
        assert connection.execute("SELECT revision FROM settings_versions WHERE scope = 'ai-chain:archive_path'").fetchone() == (1,)
