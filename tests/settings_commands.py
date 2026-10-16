"""Explicit read/save/read client flow for existing preference behavior tests.

The new command-contract tests call endpoints directly to assert conflicts and
fixed receipts. Older preference tests use this client flow because successful
saves now return receipts, while the values they test live in GET snapshots.
"""
from uuid import uuid4


PREFERENCE_PATHS = {
    '/archive-settings/paths/ai': ('paths', 'path-ai'),
    '/archive-settings/paths/ai/prompt-default': ('paths', 'path-ai'),
    '/archive-settings/paths/ai/models': ('paths', 'path-model-source'),
    '/settings/parse/model-source': ('parse', 'candidate-model-source'),
    '/api/v1/settings/paths/ai': ('paths', 'path-ai'),
    '/api/v1/settings/paths/ai/prompt': ('paths', 'path-ai'),
    '/api/v1/settings/paths/ai/models': ('paths', 'path-model-source'),
    '/api/v1/settings/parse/model-source': ('parse', 'candidate-model-source'),
    '/settings/ai/master': ('ai', 'ai-master'),
    '/api/v1/settings/ai/master': ('ai', 'ai-master'),
    '/settings/system': ('system', 'system'),
    '/settings/parse': ('parse', 'parse'),
    '/settings/parse/reset': ('parse', 'parse'),
    '/settings/parse/ai': ('parse', 'admission'),
    '/archive-settings/limits': ('archive', 'archive-limits'),
    '/archive-settings/torrent': ('archive', 'torrent'),
    '/archive-settings/auto-pack': ('archive', 'auto-pack'),
    '/archive-settings/paths/template': ('paths', 'template'),
    '/archive-settings/paths': ('paths', 'directories'),
    '/api/v1/settings/system': ('system', 'system'),
    '/api/v1/settings/parse': ('parse', 'parse'),
    '/api/v1/settings/parse/reset': ('parse', 'parse'),
    '/api/v1/settings/parse/ai': ('parse', 'admission'),
    '/api/v1/settings/archive/limits': ('archive', 'archive-limits'),
    '/api/v1/settings/archive/torrent': ('archive', 'torrent'),
    '/api/v1/settings/archive/auto-pack': ('archive', 'auto-pack'),
    '/api/v1/settings/paths/template': ('paths', 'template'),
    '/api/v1/settings/paths/directories': ('paths', 'directories'),
}


def observed_preferences(client, path):
    section, scope = PREFERENCE_PATHS[path]
    read = client.get('/api/v1/settings/' + section)
    assert read.status_code == 200, read.text
    return {'expected_revision': read.json()['settings_versions'][scope], 'idempotency_key': uuid4().hex}


def observed_profile(client, name):
    read = client.get('/api/v1/settings/archive')
    assert read.status_code == 200
    profile = next(row for row in read.json()['profiles'] if row['name'] == name)
    return {'profile_uuid': profile['profile_uuid'], 'expected_revision': profile['revision'],
            'idempotency_key': uuid4().hex}


def save_then_read_preferences(client, method, path, **kwargs):
    field = 'json' if path.startswith('/api/') else 'data'
    kwargs[field] = {**observed_preferences(client, path), **kwargs.get(field, {})}
    response = client.request(method, path, **kwargs)
    if field == 'json' and response.status_code == 200:
        section, scope = PREFERENCE_PATHS[path]
        assert response.json()['status'] == 'COMPLETED' and response.json()['scope'] == scope
        return client.get('/api/v1/settings/' + section)
    return response
