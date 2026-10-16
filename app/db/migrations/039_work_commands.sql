-- Stable work identity survives deletion of the legacy integer candidate row.
-- Retained versions and receipts must never attach to a later reused row id.
ALTER TABLE ingest_reference_changes ADD COLUMN command_guard_json TEXT;
CREATE TABLE work_versions (
    work_id TEXT PRIMARY KEY,
    candidate_id INTEGER UNIQUE REFERENCES candidates(id) ON DELETE SET NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0)
);
INSERT INTO work_versions(work_id, candidate_id)
SELECT lower(hex(randomblob(16))), id FROM candidates;

CREATE TRIGGER work_version_created AFTER INSERT ON candidates BEGIN
    INSERT INTO work_versions(work_id, candidate_id) VALUES(lower(hex(randomblob(16))), NEW.id);
END;
CREATE TRIGGER work_version_candidate AFTER UPDATE ON candidates
WHEN OLD.status IS NOT NEW.status OR OLD.ex_gid IS NOT NEW.ex_gid
    OR OLD.ex_gallery_token IS NOT NEW.ex_gallery_token
    OR OLD.filter_result IS NOT NEW.filter_result OR OLD.filter_reason IS NOT NEW.filter_reason
    OR OLD.preview_url IS NOT NEW.preview_url OR OLD.torrent_count IS NOT NEW.torrent_count
    OR OLD.torrent_hash IS NOT NEW.torrent_hash OR OLD.magnet_url IS NOT NEW.magnet_url BEGIN
    UPDATE work_versions SET revision = revision + 1 WHERE candidate_id = NEW.id;
END;

CREATE TRIGGER work_version_metadata_insert AFTER INSERT ON metadata_values BEGIN
    UPDATE work_versions SET revision = revision + 1 WHERE candidate_id = NEW.candidate_id;
END;
CREATE TRIGGER work_version_metadata_update AFTER UPDATE ON metadata_values
WHEN OLD.field_value IS NOT NEW.field_value OR OLD.is_manual IS NOT NEW.is_manual
    OR OLD.is_locked IS NOT NEW.is_locked OR OLD.confidence IS NOT NEW.confidence
    OR OLD.field_name IS NOT NEW.field_name OR OLD.value_source IS NOT NEW.value_source
    OR OLD.candidate_id IS NOT NEW.candidate_id BEGIN
    UPDATE work_versions SET revision = revision + 1 WHERE candidate_id IN (OLD.candidate_id, NEW.candidate_id);
END;
CREATE TRIGGER work_version_metadata_delete AFTER DELETE ON metadata_values BEGIN
    UPDATE work_versions SET revision = revision + 1 WHERE candidate_id = OLD.candidate_id;
END;

CREATE TRIGGER work_version_message_insert AFTER INSERT ON candidate_messages BEGIN
    UPDATE work_versions SET revision = revision + 1 WHERE candidate_id = NEW.candidate_id;
END;
CREATE TRIGGER work_version_message_delete AFTER DELETE ON candidate_messages BEGIN
    UPDATE work_versions SET revision = revision + 1 WHERE candidate_id = OLD.candidate_id;
END;
CREATE TRIGGER work_version_observation AFTER UPDATE ON source_messages
WHEN OLD.observation_revision IS NOT NEW.observation_revision BEGIN
    UPDATE work_versions SET revision = revision + 1 WHERE candidate_id IN
        (SELECT candidate_id FROM candidate_messages WHERE source_message_id = NEW.id);
END;

-- Progress/heartbeats/leases do not affect the version used by work commands.
CREATE TRIGGER work_version_job_insert AFTER INSERT ON download_jobs BEGIN
    UPDATE work_versions SET revision = revision + 1 WHERE candidate_id = NEW.candidate_id;
END;
CREATE TRIGGER work_version_job_update AFTER UPDATE OF state ON download_jobs
WHEN OLD.state IS NOT NEW.state BEGIN
    UPDATE work_versions SET revision = revision + 1 WHERE candidate_id = NEW.candidate_id;
END;
CREATE TRIGGER work_version_job_delete AFTER DELETE ON download_jobs BEGIN
    UPDATE work_versions SET revision = revision + 1 WHERE candidate_id = OLD.candidate_id;
END;
CREATE TRIGGER work_version_artifact_insert AFTER INSERT ON artifacts BEGIN
    UPDATE work_versions SET revision = revision + 1 WHERE candidate_id =
        (SELECT candidate_id FROM download_jobs WHERE id = NEW.job_id);
END;
CREATE TRIGGER work_version_artifact_update AFTER UPDATE ON artifacts
WHEN OLD.path IS NOT NEW.path OR OLD.sha256 IS NOT NEW.sha256 OR OLD.size_bytes IS NOT NEW.size_bytes BEGIN
    UPDATE work_versions SET revision = revision + 1 WHERE candidate_id IN
        (SELECT candidate_id FROM download_jobs WHERE id IN (OLD.job_id, NEW.job_id));
END;
CREATE TRIGGER work_version_artifact_delete AFTER DELETE ON artifacts BEGIN
    UPDATE work_versions SET revision = revision + 1 WHERE candidate_id =
        (SELECT candidate_id FROM download_jobs WHERE id = OLD.job_id);
END;

CREATE TRIGGER work_version_path_insert AFTER INSERT ON work_archive_paths BEGIN
    UPDATE work_versions SET revision = revision + 1 WHERE candidate_id = NEW.candidate_id;
END;
CREATE TRIGGER work_version_path_update AFTER UPDATE ON work_archive_paths
WHEN OLD.relative_path IS NOT NEW.relative_path OR OLD.is_manual IS NOT NEW.is_manual BEGIN
    UPDATE work_versions SET revision = revision + 1 WHERE candidate_id = NEW.candidate_id;
END;
CREATE TRIGGER work_version_path_delete AFTER DELETE ON work_archive_paths BEGIN
    UPDATE work_versions SET revision = revision + 1 WHERE candidate_id = OLD.candidate_id;
END;

CREATE TABLE work_command_receipts (
    idempotency_key TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES work_versions(work_id),
    payload_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- A confirmation executes this exact preview, never a newly ranked library.
CREATE TABLE work_dedup_plans (
    plan_id TEXT PRIMARY KEY,
    plan_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE manual_work_requests (
    operation_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    payload_hash TEXT NOT NULL,
    session_id TEXT,
    status TEXT NOT NULL DEFAULT 'RUNNING' CHECK(status IN ('RUNNING', 'COMPLETED', 'FAILED', 'UNKNOWN')),
    result_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Child receipts fix both successes and domain refusals before a batch result
-- is assembled. Interrupted requests resume without reclassifying old results.
CREATE TABLE work_review_batches (
    idempotency_key TEXT PRIMARY KEY,
    payload_hash TEXT NOT NULL,
    request_json TEXT NOT NULL,
    response_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE work_review_refusals (
    idempotency_key TEXT PRIMARY KEY,
    payload_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- The DB edit and its follow-up file synchronization are one accepted intent.
-- A process exit between them must not lose the request or reapply the edit.
CREATE TABLE work_metadata_syncs (
    sync_id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES work_versions(work_id),
    revision INTEGER NOT NULL,
    fields_json TEXT NOT NULL,
    fetched_fields_json TEXT NOT NULL,
    operator_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING', 'COMPLETED', 'SKIPPED')),
    result_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX work_metadata_sync_pending ON work_metadata_syncs(status, created_at);

-- Probe snapshots remain in the owning process. A restart cannot safely repeat
-- a potentially billed request; keep the durable admission and report UNKNOWN.
CREATE TABLE ai_network_operations (
    operation_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    payload_hash TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('fetch', 'model', 'provider', 'all')),
    target_json TEXT NOT NULL,
    session_id TEXT,
    status TEXT NOT NULL DEFAULT 'RUNNING' CHECK(status IN ('RUNNING', 'COMPLETED', 'FAILED', 'UNKNOWN')),
    result_json TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT
);
CREATE TABLE ai_network_claims (
    resource TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES ai_network_operations(operation_id)
);

CREATE TABLE work_metadata_fetches (
    operation_id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES work_versions(work_id),
    revision INTEGER NOT NULL,
    session_id TEXT,
    status TEXT NOT NULL DEFAULT 'RUNNING' CHECK(status IN ('RUNNING', 'COMPLETED', 'FAILED', 'UNKNOWN')),
    error_code TEXT,
    result_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX work_one_metadata_fetch ON work_metadata_fetches(work_id) WHERE status = 'RUNNING';

-- Independent preference forms have independent edit versions. Runtime counters
-- and unrelated internal settings do not participate in these edit versions.
CREATE TABLE settings_versions (
    scope TEXT PRIMARY KEY,
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0)
);
INSERT INTO settings_versions(scope) VALUES
    ('system'), ('parse'), ('admission'), ('archive-limits'), ('auto-pack'), ('template'), ('directories'), ('torrent'), ('ai-master'),
    ('path-ai'), ('path-model-source'), ('candidate-model-source');
CREATE TABLE settings_field_scopes (
    table_name TEXT NOT NULL,
    key TEXT NOT NULL,
    scope TEXT NOT NULL REFERENCES settings_versions(scope),
    PRIMARY KEY(table_name, key)
);
INSERT INTO settings_field_scopes(table_name, key, scope) VALUES
    ('system_settings', 'source_concurrency', 'system'),
    ('system_settings', 'poll_interval_ms', 'system'),
    ('system_settings', 'timezone', 'system'),
    ('system_settings', 'auto_approval_interval_minutes', 'system'),
    ('system_settings', 'mobile_access_ttl_seconds', 'system'),
    ('system_settings', 'mobile_refresh_ttl_seconds', 'system'),
    ('system_settings', 'log_level', 'system'),
    ('system_settings', 'download_source_priority', 'system'),
    ('system_settings', 'parse_rules_json', 'parse'),
    ('system_settings', 'ai_enabled', 'ai-master'),
    ('system_settings', 'ai_candidate_enabled', 'admission'),
    ('system_settings', 'ai_candidate_override_parse_rules', 'admission'),
    ('system_settings', 'ai_candidate_prompt', 'admission'),
    ('system_settings', 'ai_candidate_fallback', 'admission'),
    ('system_settings', 'ai_candidate_model_source', 'candidate-model-source'),
    ('archive_settings', 'path_source', 'path-ai'),
    ('archive_settings', 'ai_prompt', 'path-ai'),
    ('archive_settings', 'ai_batch_size', 'path-ai'),
    ('archive_settings', 'ai_concurrency', 'path-ai'),
    ('archive_settings', 'ai_fallback_to_rules', 'path-ai'),
    ('archive_settings', 'ai_stream', 'path-ai'),
    ('archive_settings', 'ai_default_include_current', 'path-ai'),
    ('archive_settings', 'auto_series', 'path-ai'),
    ('archive_settings', 'ai_model_source', 'path-model-source'),
    ('archive_settings', 'max_members', 'archive-limits'),
    ('archive_settings', 'max_total_bytes', 'archive-limits'),
    ('archive_settings', 'max_member_bytes', 'archive-limits'),
    ('archive_settings', 'max_compression_ratio', 'archive-limits'),
    ('archive_settings', 'max_depth', 'archive-limits'),
    ('archive_settings', 'keep_original', 'archive-limits'),
    ('archive_settings', 'image_quality', 'archive-limits'),
    ('archive_settings', 'auto_pack_after_download', 'auto-pack'),
    ('archive_settings', 'library_template', 'template'),
    ('archive_settings', 'library_title_source', 'template'),
    ('archive_settings', 'library_path', 'directories'),
    ('archive_settings', 'work_path', 'directories'),
    ('archive_settings', 'torrent_client_url', 'torrent'),
    ('archive_settings', 'torrent_client_username', 'torrent'),
    ('archive_settings', 'torrent_client_password', 'torrent'),
    ('archive_settings', 'torrent_category', 'torrent'),
    ('archive_settings', 'torrent_save_path', 'torrent'),
    ('archive_settings', 'torrent_local_save_path', 'torrent'),
    ('archive_settings', 'torrent_keep_seeding', 'torrent'),
    ('archive_settings', 'torrent_auto_pack', 'torrent');
CREATE TABLE settings_command_receipts (
    idempotency_key TEXT PRIMARY KEY,
    payload_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TRIGGER settings_system_insert AFTER INSERT ON system_settings BEGIN
    UPDATE settings_versions SET revision = revision + 1 WHERE scope =
        (SELECT scope FROM settings_field_scopes WHERE table_name = 'system_settings' AND key = NEW.key);
END;
CREATE TRIGGER settings_system_update AFTER UPDATE ON system_settings WHEN OLD.value IS NOT NEW.value BEGIN
    UPDATE settings_versions SET revision = revision + 1 WHERE scope =
        (SELECT scope FROM settings_field_scopes WHERE table_name = 'system_settings' AND key = NEW.key);
END;
CREATE TRIGGER settings_system_delete AFTER DELETE ON system_settings BEGIN
    UPDATE settings_versions SET revision = revision + 1 WHERE scope =
        (SELECT scope FROM settings_field_scopes WHERE table_name = 'system_settings' AND key = OLD.key);
END;
CREATE TRIGGER settings_archive_insert AFTER INSERT ON archive_settings BEGIN
    UPDATE settings_versions SET revision = revision + 1 WHERE scope =
        (SELECT scope FROM settings_field_scopes WHERE table_name = 'archive_settings' AND key = NEW.key);
END;
CREATE TRIGGER settings_archive_update AFTER UPDATE ON archive_settings WHEN OLD.value IS NOT NEW.value BEGIN
    UPDATE settings_versions SET revision = revision + 1 WHERE scope =
        (SELECT scope FROM settings_field_scopes WHERE table_name = 'archive_settings' AND key = NEW.key);
END;
CREATE TRIGGER settings_archive_delete AFTER DELETE ON archive_settings BEGIN
    UPDATE settings_versions SET revision = revision + 1 WHERE scope =
        (SELECT scope FROM settings_field_scopes WHERE table_name = 'archive_settings' AND key = OLD.key);
END;

-- Registered tool profiles have independent identities and edit versions.
ALTER TABLE archive_tool_profiles ADD COLUMN profile_uuid TEXT NOT NULL DEFAULT '';
ALTER TABLE archive_tool_profiles ADD COLUMN revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0);
UPDATE archive_tool_profiles SET profile_uuid = lower(hex(randomblob(16)));
CREATE UNIQUE INDEX tool_profile_uuid ON archive_tool_profiles(profile_uuid);
CREATE TRIGGER tool_profile_identity AFTER INSERT ON archive_tool_profiles WHEN NEW.profile_uuid = '' BEGIN
    UPDATE archive_tool_profiles SET profile_uuid = lower(hex(randomblob(16))) WHERE id = NEW.id;
END;
CREATE TRIGGER tool_profile_identity_immutable BEFORE UPDATE OF profile_uuid, id, name ON archive_tool_profiles
WHEN OLD.profile_uuid != '' AND (OLD.profile_uuid IS NOT NEW.profile_uuid OR OLD.id IS NOT NEW.id OR OLD.name IS NOT NEW.name) BEGIN
    SELECT RAISE(ABORT, 'Tool profile identity is immutable');
END;
CREATE TRIGGER tool_profile_edit_version AFTER UPDATE ON archive_tool_profiles
WHEN OLD.revision = NEW.revision AND (OLD.backend IS NOT NEW.backend OR OLD.kind IS NOT NEW.kind
    OR OLD.executable_path IS NOT NEW.executable_path OR OLD.supported_formats IS NOT NEW.supported_formats
    OR OLD.timeout_seconds IS NOT NEW.timeout_seconds OR OLD.capabilities IS NOT NEW.capabilities
    OR OLD.enabled IS NOT NEW.enabled) BEGIN
    UPDATE archive_tool_profiles SET revision = revision + 1 WHERE id = NEW.id;
END;

-- Rules retain their own edit version; unrelated rules never share a CAS.
ALTER TABLE auto_approval_rules ADD COLUMN rule_uuid TEXT NOT NULL DEFAULT '';
ALTER TABLE archive_path_rules ADD COLUMN rule_uuid TEXT NOT NULL DEFAULT '';
UPDATE auto_approval_rules SET rule_uuid = lower(hex(randomblob(16)));
UPDATE archive_path_rules SET rule_uuid = lower(hex(randomblob(16)));
CREATE UNIQUE INDEX auto_rule_uuid ON auto_approval_rules(rule_uuid);
CREATE UNIQUE INDEX path_rule_uuid ON archive_path_rules(rule_uuid);
CREATE TRIGGER auto_rule_identity AFTER INSERT ON auto_approval_rules WHEN NEW.rule_uuid = '' BEGIN
    UPDATE auto_approval_rules SET rule_uuid = lower(hex(randomblob(16))) WHERE id = NEW.id;
END;
CREATE TRIGGER path_rule_identity AFTER INSERT ON archive_path_rules WHEN NEW.rule_uuid = '' BEGIN
    UPDATE archive_path_rules SET rule_uuid = lower(hex(randomblob(16))) WHERE id = NEW.id;
END;
CREATE TRIGGER auto_rule_identity_immutable BEFORE UPDATE OF rule_uuid, id ON auto_approval_rules
WHEN OLD.rule_uuid != '' AND (OLD.rule_uuid IS NOT NEW.rule_uuid OR OLD.id IS NOT NEW.id) BEGIN
    SELECT RAISE(ABORT, 'Rule identity is immutable');
END;
CREATE TRIGGER path_rule_identity_immutable BEFORE UPDATE OF rule_uuid, id ON archive_path_rules
WHEN OLD.rule_uuid != '' AND (OLD.rule_uuid IS NOT NEW.rule_uuid OR OLD.id IS NOT NEW.id) BEGIN
    SELECT RAISE(ABORT, 'Rule identity is immutable');
END;
CREATE TRIGGER auto_rule_edit_version AFTER UPDATE ON auto_approval_rules
WHEN OLD.version = NEW.version AND (OLD.name IS NOT NEW.name OR OLD.enabled IS NOT NEW.enabled
    OR OLD.priority IS NOT NEW.priority OR OLD.condition_json IS NOT NEW.condition_json
    OR OLD.dsl_snapshot IS NOT NEW.dsl_snapshot OR OLD.case_sensitive IS NOT NEW.case_sensitive
    OR OLD.action IS NOT NEW.action) BEGIN
    UPDATE auto_approval_rules SET version = version + 1 WHERE id = NEW.id;
END;
CREATE TRIGGER path_rule_edit_version AFTER UPDATE ON archive_path_rules
WHEN OLD.version = NEW.version AND (OLD.name IS NOT NEW.name OR OLD.enabled IS NOT NEW.enabled
    OR OLD.priority IS NOT NEW.priority OR OLD.condition_json IS NOT NEW.condition_json
    OR OLD.dsl_snapshot IS NOT NEW.dsl_snapshot OR OLD.case_sensitive IS NOT NEW.case_sensitive
    OR OLD.path_template IS NOT NEW.path_template) BEGIN
    UPDATE archive_path_rules SET version = version + 1 WHERE id = NEW.id;
END;
CREATE TABLE rule_command_receipts (
    idempotency_key TEXT PRIMARY KEY,
    payload_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Tool installation is a durable operation, separate from profile edits/health.
CREATE TABLE toolchain_version (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0)
);
INSERT INTO toolchain_version(singleton) VALUES(1);
CREATE TABLE toolchain_operations (
    operation_id TEXT PRIMARY KEY,
    version TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    payload_hash TEXT NOT NULL,
    revision INTEGER NOT NULL,
    session_id TEXT REFERENCES worker_sessions(session_id),
    status TEXT NOT NULL CHECK(status IN ('RUNNING', 'COMPLETED', 'FAILED', 'UNKNOWN')),
    error_code TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT
);
CREATE UNIQUE INDEX toolchain_single_install ON toolchain_operations(status) WHERE status = 'RUNNING';

-- AI edit versions exclude health, verification and usage writes.
ALTER TABLE ai_providers ADD COLUMN provider_uuid TEXT NOT NULL DEFAULT '';
ALTER TABLE ai_providers ADD COLUMN revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0);
ALTER TABLE ai_providers ADD COLUMN keys_revision INTEGER NOT NULL DEFAULT 1 CHECK(keys_revision > 0);
ALTER TABLE ai_providers ADD COLUMN models_revision INTEGER NOT NULL DEFAULT 1 CHECK(models_revision > 0);
ALTER TABLE ai_provider_keys ADD COLUMN key_uuid TEXT NOT NULL DEFAULT '';
ALTER TABLE ai_provider_keys ADD COLUMN revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0);
ALTER TABLE ai_provider_keys ADD COLUMN health_revision INTEGER NOT NULL DEFAULT 1 CHECK(health_revision > 0);
ALTER TABLE ai_provider_models ADD COLUMN model_uuid TEXT NOT NULL DEFAULT '';
ALTER TABLE ai_provider_models ADD COLUMN revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0);
UPDATE ai_providers SET provider_uuid = lower(hex(randomblob(16)));
UPDATE ai_provider_keys SET key_uuid = lower(hex(randomblob(16)));
UPDATE ai_provider_models SET model_uuid = lower(hex(randomblob(16)));
CREATE UNIQUE INDEX ai_provider_uuid ON ai_providers(provider_uuid);
CREATE UNIQUE INDEX ai_key_uuid ON ai_provider_keys(key_uuid);
CREATE UNIQUE INDEX ai_model_uuid ON ai_provider_models(model_uuid);
CREATE TRIGGER ai_provider_identity AFTER INSERT ON ai_providers WHEN NEW.provider_uuid = '' BEGIN
    UPDATE ai_providers SET provider_uuid = lower(hex(randomblob(16))) WHERE id = NEW.id;
END;
CREATE TRIGGER ai_key_identity AFTER INSERT ON ai_provider_keys WHEN NEW.key_uuid = '' BEGIN
    UPDATE ai_provider_keys SET key_uuid = lower(hex(randomblob(16))) WHERE id = NEW.id;
END;
CREATE TRIGGER ai_model_identity AFTER INSERT ON ai_provider_models WHEN NEW.model_uuid = '' BEGIN
    UPDATE ai_provider_models SET model_uuid = lower(hex(randomblob(16))) WHERE id = NEW.id;
END;
CREATE TRIGGER ai_provider_identity_immutable BEFORE UPDATE OF provider_uuid, id ON ai_providers
WHEN OLD.provider_uuid != '' AND (OLD.provider_uuid IS NOT NEW.provider_uuid OR OLD.id IS NOT NEW.id) BEGIN
    SELECT RAISE(ABORT, 'AI provider identity is immutable');
END;
CREATE TRIGGER ai_key_identity_immutable BEFORE UPDATE OF key_uuid, id, provider_id ON ai_provider_keys
WHEN OLD.key_uuid != '' AND (OLD.key_uuid IS NOT NEW.key_uuid OR OLD.id IS NOT NEW.id OR OLD.provider_id IS NOT NEW.provider_id) BEGIN
    SELECT RAISE(ABORT, 'AI key identity is immutable');
END;
CREATE TRIGGER ai_model_identity_immutable BEFORE UPDATE OF model_uuid, id, provider_id ON ai_provider_models
WHEN OLD.model_uuid != '' AND (OLD.model_uuid IS NOT NEW.model_uuid OR OLD.id IS NOT NEW.id OR OLD.provider_id IS NOT NEW.provider_id) BEGIN
    SELECT RAISE(ABORT, 'AI model identity is immutable');
END;
CREATE TRIGGER ai_provider_edit_version AFTER UPDATE OF name, code, base_url, timeout_seconds, max_retries,
    enabled, custom_headers, default_params ON ai_providers
WHEN OLD.name IS NOT NEW.name OR OLD.code IS NOT NEW.code OR OLD.base_url IS NOT NEW.base_url
    OR OLD.timeout_seconds IS NOT NEW.timeout_seconds OR OLD.max_retries IS NOT NEW.max_retries
    OR OLD.enabled IS NOT NEW.enabled OR OLD.custom_headers IS NOT NEW.custom_headers
    OR OLD.default_params IS NOT NEW.default_params BEGIN
    UPDATE ai_providers SET revision = revision + 1 WHERE id = NEW.id;
    UPDATE ai_provider_models SET last_verified_at = NULL, last_verify_ok = NULL, last_verify_error = NULL
        WHERE provider_id = NEW.id;
END;
CREATE TRIGGER ai_key_edit_version AFTER UPDATE OF label, cipher, enabled ON ai_provider_keys
WHEN OLD.label IS NOT NEW.label OR OLD.cipher IS NOT NEW.cipher OR OLD.enabled IS NOT NEW.enabled BEGIN
    UPDATE ai_provider_keys SET revision = revision + 1 WHERE id = NEW.id;
    UPDATE ai_providers SET keys_revision = keys_revision + 1 WHERE id = NEW.provider_id;
    UPDATE ai_provider_models SET last_verified_at = NULL, last_verify_ok = NULL, last_verify_error = NULL
        WHERE provider_id = NEW.provider_id;
END;
CREATE TRIGGER ai_key_health_version AFTER UPDATE OF failures, cooldown_until, last_used_at ON ai_provider_keys
WHEN OLD.failures IS NOT NEW.failures OR OLD.cooldown_until IS NOT NEW.cooldown_until
    OR OLD.last_used_at IS NOT NEW.last_used_at BEGIN
    UPDATE ai_provider_keys SET health_revision = health_revision + 1 WHERE id = NEW.id;
END;
CREATE TRIGGER ai_key_added AFTER INSERT ON ai_provider_keys BEGIN
    UPDATE ai_providers SET keys_revision = keys_revision + 1 WHERE id = NEW.provider_id;
    UPDATE ai_provider_models SET last_verified_at = NULL, last_verify_ok = NULL, last_verify_error = NULL
        WHERE provider_id = NEW.provider_id;
END;
CREATE TRIGGER ai_key_removed AFTER DELETE ON ai_provider_keys BEGIN
    UPDATE ai_providers SET keys_revision = keys_revision + 1 WHERE id = OLD.provider_id;
    UPDATE ai_provider_models SET last_verified_at = NULL, last_verify_ok = NULL, last_verify_error = NULL
        WHERE provider_id = OLD.provider_id;
END;
CREATE TRIGGER ai_model_edit_version AFTER UPDATE OF name, enabled, params ON ai_provider_models
WHEN OLD.name IS NOT NEW.name OR OLD.enabled IS NOT NEW.enabled OR OLD.params IS NOT NEW.params BEGIN
    UPDATE ai_provider_models SET revision = revision + 1, last_verified_at = NULL,
        last_verify_ok = NULL, last_verify_error = NULL WHERE id = NEW.id;
END;
CREATE TRIGGER ai_provider_model_added AFTER INSERT ON ai_provider_models BEGIN
    UPDATE ai_providers SET models_revision = models_revision + 1 WHERE id = NEW.provider_id;
END;
CREATE TRIGGER ai_provider_model_removed AFTER DELETE ON ai_provider_models BEGIN
    UPDATE ai_providers SET models_revision = models_revision + 1 WHERE id = OLD.provider_id;
END;
CREATE TRIGGER ai_provider_model_edited AFTER UPDATE OF revision ON ai_provider_models
WHEN OLD.revision != NEW.revision BEGIN
    UPDATE ai_providers SET models_revision = models_revision + 1 WHERE id = NEW.provider_id;
END;

INSERT INTO settings_versions(scope) VALUES('ai-providers'), ('ai-catalogue');
CREATE TRIGGER ai_catalogue_provider_added AFTER INSERT ON ai_providers BEGIN
    UPDATE settings_versions SET revision = revision + 1 WHERE scope IN ('ai-providers', 'ai-catalogue');
END;
CREATE TRIGGER ai_catalogue_provider_removed AFTER DELETE ON ai_providers BEGIN
    UPDATE settings_versions SET revision = revision + 1 WHERE scope IN ('ai-providers', 'ai-catalogue');
END;
CREATE TRIGGER ai_catalogue_provider_edited AFTER UPDATE OF revision, keys_revision, models_revision ON ai_providers
WHEN OLD.revision != NEW.revision OR OLD.keys_revision != NEW.keys_revision OR OLD.models_revision != NEW.models_revision BEGIN
    UPDATE settings_versions SET revision = revision + 1 WHERE scope = 'ai-catalogue';
END;
CREATE TRIGGER ai_catalogue_model_added AFTER INSERT ON ai_provider_models BEGIN
    UPDATE settings_versions SET revision = revision + 1 WHERE scope = 'ai-catalogue';
END;
CREATE TRIGGER ai_catalogue_model_removed AFTER DELETE ON ai_provider_models BEGIN
    UPDATE settings_versions SET revision = revision + 1 WHERE scope = 'ai-catalogue';
END;
CREATE TRIGGER ai_catalogue_model_edited AFTER UPDATE OF revision ON ai_provider_models
WHEN OLD.revision != NEW.revision BEGIN
    UPDATE settings_versions SET revision = revision + 1 WHERE scope = 'ai-catalogue';
END;
CREATE TRIGGER ai_catalogue_chain_added AFTER INSERT ON ai_model_chain BEGIN
    UPDATE settings_versions SET revision = revision + 1 WHERE scope = 'ai-catalogue';
END;
CREATE TRIGGER ai_catalogue_chain_removed AFTER DELETE ON ai_model_chain BEGIN
    UPDATE settings_versions SET revision = revision + 1 WHERE scope = 'ai-catalogue';
END;
CREATE TRIGGER ai_catalogue_chain_edited AFTER UPDATE ON ai_model_chain
WHEN OLD.scope IS NOT NEW.scope OR OLD.position IS NOT NEW.position OR OLD.provider_model_id IS NOT NEW.provider_model_id BEGIN
    UPDATE settings_versions SET revision = revision + 1 WHERE scope = 'ai-catalogue';
END;

INSERT INTO settings_versions(scope) VALUES
    ('ai-chain:default'), ('ai-chain:archive_path'), ('ai-chain:candidate_admission');
CREATE TRIGGER ai_chain_scope_added AFTER INSERT ON ai_model_chain BEGIN
    UPDATE settings_versions SET revision = revision + 1 WHERE scope = 'ai-chain:' || NEW.scope;
END;
CREATE TRIGGER ai_chain_scope_removed AFTER DELETE ON ai_model_chain BEGIN
    UPDATE settings_versions SET revision = revision + 1 WHERE scope = 'ai-chain:' || OLD.scope;
END;
CREATE TRIGGER ai_chain_scope_edited AFTER UPDATE ON ai_model_chain
WHEN OLD.scope IS NOT NEW.scope OR OLD.position IS NOT NEW.position OR OLD.provider_model_id IS NOT NEW.provider_model_id BEGIN
    UPDATE settings_versions SET revision = revision + 1
        WHERE scope IN ('ai-chain:' || OLD.scope, 'ai-chain:' || NEW.scope);
END;

-- Credential configuration versions exclude authentication use and extraction health.
INSERT INTO settings_versions(scope) VALUES('vault'), ('api-key');
ALTER TABLE archive_passwords ADD COLUMN password_uuid TEXT NOT NULL DEFAULT '';
ALTER TABLE archive_passwords ADD COLUMN revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0);
UPDATE archive_passwords SET password_uuid = lower(hex(randomblob(16)));
CREATE UNIQUE INDEX archive_password_identity ON archive_passwords(password_uuid) WHERE password_uuid != '';
CREATE TRIGGER archive_password_identity_created AFTER INSERT ON archive_passwords BEGIN
    UPDATE archive_passwords SET password_uuid = lower(hex(randomblob(16))) WHERE id = NEW.id AND password_uuid = '';
    UPDATE settings_versions SET revision = revision + 1 WHERE scope = 'vault';
END;
CREATE TRIGGER archive_password_identity_immutable BEFORE UPDATE OF password_uuid ON archive_passwords
WHEN OLD.password_uuid != '' AND OLD.password_uuid IS NOT NEW.password_uuid BEGIN
    SELECT RAISE(ABORT, 'archive password identity is immutable');
END;
CREATE TRIGGER archive_password_edit_version AFTER UPDATE OF name, secret_json, priority, enabled ON archive_passwords
WHEN OLD.name IS NOT NEW.name OR OLD.secret_json IS NOT NEW.secret_json
    OR OLD.priority IS NOT NEW.priority OR OLD.enabled IS NOT NEW.enabled BEGIN
    UPDATE archive_passwords SET revision = revision + 1 WHERE id = NEW.id;
    UPDATE settings_versions SET revision = revision + 1 WHERE scope = 'vault';
END;
CREATE TRIGGER archive_password_removed AFTER DELETE ON archive_passwords BEGIN
    UPDATE settings_versions SET revision = revision + 1 WHERE scope = 'vault';
END;
CREATE TRIGGER api_key_created AFTER INSERT ON api_credentials WHEN NEW.kind = 'api_key' BEGIN
    UPDATE settings_versions SET revision = revision + 1 WHERE scope = 'api-key';
END;
CREATE TRIGGER api_key_configuration AFTER UPDATE ON api_credentials
WHEN (OLD.kind = 'api_key' OR NEW.kind = 'api_key') AND
    (OLD.kind IS NOT NEW.kind OR OLD.revoked_at IS NOT NEW.revoked_at OR OLD.public_id IS NOT NEW.public_id
     OR OLD.secret_hash IS NOT NEW.secret_hash OR OLD.label IS NOT NEW.label OR OLD.expires_at IS NOT NEW.expires_at) BEGIN
    UPDATE settings_versions SET revision = revision + 1 WHERE scope = 'api-key';
END;
CREATE TRIGGER api_key_removed AFTER DELETE ON api_credentials WHEN OLD.kind = 'api_key' BEGIN
    UPDATE settings_versions SET revision = revision + 1 WHERE scope = 'api-key';
END;

-- Whole-library maintenance observes a work set and path policy, while cache
-- invalidation has its own version. Heartbeats/progress never advance either.
INSERT INTO settings_versions(scope, revision) VALUES('rearchive', 1), ('path-cache', 1);
CREATE TRIGGER rearchive_work_insert AFTER INSERT ON work_versions BEGIN
    UPDATE settings_versions SET revision = revision + 1 WHERE scope = 'rearchive';
END;
CREATE TRIGGER rearchive_work_update AFTER UPDATE ON work_versions BEGIN
    UPDATE settings_versions SET revision = revision + 1 WHERE scope = 'rearchive';
END;
CREATE TRIGGER rearchive_work_delete AFTER DELETE ON work_versions BEGIN
    UPDATE settings_versions SET revision = revision + 1 WHERE scope = 'rearchive';
END;
CREATE TRIGGER path_cache_insert AFTER INSERT ON ai_path_suggestions BEGIN
    UPDATE settings_versions SET revision = revision + 1 WHERE scope = 'path-cache';
END;
CREATE TRIGGER path_cache_update AFTER UPDATE ON ai_path_suggestions BEGIN
    UPDATE settings_versions SET revision = revision + 1 WHERE scope = 'path-cache';
END;
CREATE TRIGGER path_cache_delete AFTER DELETE ON ai_path_suggestions BEGIN
    UPDATE settings_versions SET revision = revision + 1 WHERE scope = 'path-cache';
END;
CREATE TRIGGER rearchive_policy_version AFTER UPDATE ON settings_versions
WHEN OLD.scope IN ('template', 'directories', 'path-ai', 'path-model-source', 'ai-master',
                  'ai-chain:default', 'ai-chain:archive_path', 'ai-catalogue', 'path-cache')
     AND OLD.revision IS NOT NEW.revision BEGIN
    UPDATE settings_versions SET revision = revision + 1 WHERE scope = 'rearchive';
END;
CREATE TRIGGER rearchive_rule_insert AFTER INSERT ON archive_path_rules BEGIN
    UPDATE settings_versions SET revision = revision + 1 WHERE scope = 'rearchive';
END;
CREATE TRIGGER rearchive_rule_update AFTER UPDATE ON archive_path_rules BEGIN
    UPDATE settings_versions SET revision = revision + 1 WHERE scope = 'rearchive';
END;
CREATE TRIGGER rearchive_rule_delete AFTER DELETE ON archive_path_rules BEGIN
    UPDATE settings_versions SET revision = revision + 1 WHERE scope = 'rearchive';
END;
