-- AI-generated archive paths: the providers, keys and model chain that decide a
-- book's folder, plus a cache of what the model answered.
--
-- Why five tables rather than a few settings keys. Three of these are lists an
-- operator edits item by item -- several providers, several API keys per
-- provider (rotated), several model names per provider -- and a JSON blob would
-- have to be rewritten whole on every edit, which is how two operators' edits
-- (or one operator's two tabs) lose each other. Rotation state and verification
-- results are per row too: a key that just returned 401 is cooled down on its
-- own, not as part of a document.
--
-- The chain is a separate table on purpose. 「主力 + 备用」 is an ordering of
-- *(provider, model)* pairs and nothing else: a provider can appear twice with
-- different models, and a model can be tried on a fallback provider. Keeping the
-- order in one row per position is what makes 「第 0 位是主力」 checkable.
--
-- `ai_path_suggestions` is the answer cache, not an optimisation: the model is
-- not deterministic, so the detail page, the packer and the re-archive sweep
-- must all read the same stored answer, and the sweep's 「路径有变动」 test is
-- only meaningful against a recorded earlier answer. `fingerprint` covers the
-- metadata, the prompt, the model chain and the provider base URLs -- but not
-- the API keys, because rotating a key must not invalidate the whole library.

CREATE TABLE ai_providers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    -- Protocol adapter. Only 'openai' exists today; the column is here so a
    -- future adapter is a value rather than a schema change.
    code TEXT NOT NULL DEFAULT 'openai',
    base_url TEXT NOT NULL,
    timeout_seconds INTEGER NOT NULL DEFAULT 30,
    max_retries INTEGER NOT NULL DEFAULT 1,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE ai_provider_keys (
    id INTEGER PRIMARY KEY,
    provider_id INTEGER NOT NULL REFERENCES ai_providers(id) ON DELETE CASCADE,
    label TEXT NOT NULL DEFAULT '',
    -- `encrypt_password` envelope under the archive master key. The plaintext
    -- never leaves the process and is never rendered back into the page.
    cipher TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    failures INTEGER NOT NULL DEFAULT 0,
    -- Set when this key returned 401/403 (bad key) or 429 (rate limited); the
    -- rotation skips it until then, and the value survives a restart so the
    -- first request after a restart does not walk into the same wall.
    cooldown_until TEXT,
    last_used_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_ai_provider_keys_provider
    ON ai_provider_keys (provider_id, enabled);

CREATE TABLE ai_provider_models (
    id INTEGER PRIMARY KEY,
    provider_id INTEGER NOT NULL REFERENCES ai_providers(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    -- Connectivity verification from the settings page. A chain entry built on
    -- an unverified model is refused, because address/key/model typos are cheap
    -- to find here and expensive to find inside a packing job.
    last_verified_at TEXT,
    last_verify_ok INTEGER,
    last_verify_error TEXT,
    UNIQUE (provider_id, name)
);

CREATE TABLE ai_model_chain (
    position INTEGER PRIMARY KEY,
    provider_model_id INTEGER NOT NULL
        REFERENCES ai_provider_models(id) ON DELETE CASCADE
);

CREATE TABLE ai_path_suggestions (
    candidate_id INTEGER PRIMARY KEY REFERENCES candidates(id),
    fingerprint TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    directory TEXT NOT NULL,
    filename TEXT NOT NULL,
    provider_id INTEGER,
    model_name TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
