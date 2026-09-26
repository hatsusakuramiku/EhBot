-- AI provider management, AstrBot-style: an endpoint (「供应商」) is configured
-- once with its credentials, and the models on it are a list an operator picks
-- from -- with one global default chain that every feature inherits unless it
-- declares its own.
--
-- Three additions, no restructuring:
--
--  * `custom_headers` -- a gateway that wants `X-Api-Key` or an org header is
--    not a second protocol adapter. Stored as a JSON object; the page refuses
--    to put a credential here, because a real secret belongs in the key list
--    where it is encrypted and never rendered back.
--  * `default_params` / `params` -- the request body beyond `{model, messages}`.
--    「不填就不发」 is the default because a fixed body cannot talk to a model that
--    rejects `temperature` (OpenAI's reasoning models) or an endpoint that 400s
--    on any field it does not know.
--  * `ai_model_chain.scope` -- one chain per feature, so 「路径用哪个模型」 is a
--    per-page choice while the AI page still owns the global default. Rows that
--    predate this migration become the default chain; the archive-path feature
--    inherits it until somebody opts into a custom list.
--
-- The JSON columns are TEXT rather than a child table on purpose: they are
-- edited as one box, read as one value, and never queried by field.

ALTER TABLE ai_providers ADD COLUMN custom_headers TEXT NOT NULL DEFAULT '{}';
ALTER TABLE ai_providers ADD COLUMN default_params TEXT NOT NULL DEFAULT '{}';
ALTER TABLE ai_provider_models ADD COLUMN params TEXT NOT NULL DEFAULT '{}';

CREATE TABLE ai_model_chain_scoped (
    scope TEXT NOT NULL DEFAULT 'default',
    position INTEGER NOT NULL,
    provider_model_id INTEGER NOT NULL
        REFERENCES ai_provider_models(id) ON DELETE CASCADE,
    PRIMARY KEY (scope, position)
);

INSERT INTO ai_model_chain_scoped (scope, position, provider_model_id)
    SELECT 'default', position, provider_model_id FROM ai_model_chain;

DROP TABLE ai_model_chain;

ALTER TABLE ai_model_chain_scoped RENAME TO ai_model_chain;

CREATE INDEX idx_ai_model_chain_scope ON ai_model_chain (scope, position);
