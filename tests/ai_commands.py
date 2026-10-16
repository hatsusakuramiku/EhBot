"""Existing catalogue behavior tests explicitly read command preconditions."""
from uuid import uuid4


def observed_network(client, action, target_id=None):
    response = client.get('/api/v1/settings/ai')
    assert response.status_code == 200, response.text
    snapshot = response.json()
    body = {'idempotency_key': uuid4().hex}
    if action == 'all':
        return {**body, 'expected_revision': snapshot['settings_versions']['ai-catalogue']}
    provider = next(row for row in snapshot['providers'] if (
        any(model['model_id'] == int(target_id) for model in row['models']) if action == 'model'
        else row['provider_id'] == int(target_id)))
    body.update(provider_uuid=provider['provider_uuid'], keys_revision=provider['keys_revision'])
    if action == 'model':
        model = next(row for row in provider['models'] if row['model_id'] == int(target_id))
        body.update(model_uuid=model['model_uuid'], expected_revision=model['revision'], provider_revision=provider['revision'])
    else:
        body['expected_revision'] = provider['revision']
        if action == 'provider':
            body['models_revision'] = provider['models_revision']
    return body


def admit_network_and_read_page(client, action, path, target_id=None, **kwargs):
    from tests.integration.test_toolchain_commands import settled
    kwargs['data'] = {**observed_network(client, action, target_id), **kwargs.get('data', {})}
    response = client.post(path, follow_redirects=False, **kwargs)
    assert response.status_code == 303, response.text
    client.portal.call(settled, client.app.state.ai_network_commands)
    return client.get(response.headers['location'])


def observed_provider(client, provider_id=None):
    response = client.get('/api/v1/settings/ai')
    assert response.status_code == 200, response.text
    snapshot = response.json()
    if provider_id is None:
        return {'expected_revision': snapshot['settings_versions']['ai-providers'], 'idempotency_key': uuid4().hex}
    provider = next(row for row in snapshot['providers'] if row['provider_id'] == int(provider_id))
    return {'provider_uuid': provider['provider_uuid'], 'expected_revision': provider['revision'],
            'keys_revision': provider['keys_revision'], 'models_revision': provider['models_revision'],
            'idempotency_key': uuid4().hex}


def save_then_read_provider(client, method, path, **kwargs):
    field = 'json' if path.startswith('/api/') else 'data'
    values = kwargs.get(field, {})
    provider_id = values.get('provider_id')
    if method.upper() == 'DELETE' or path.endswith('/delete'):
        provider_id = path.rstrip('/').split('/')[-2 if path.endswith('/delete') else -1]
    kwargs[field] = {**observed_provider(client, provider_id), **values}
    response = client.request(method, path, **kwargs)
    if field == 'json' and response.status_code == 200:
        assert response.json()['status'] == 'COMPLETED'
        return client.get('/api/v1/settings/ai')
    return response


def observed_catalogue(client, action, target_id):
    response = client.get('/api/v1/settings/ai')
    assert response.status_code == 200, response.text
    providers = response.json()['providers']
    if action.endswith('-add'):
        item = next(row for row in providers if row['provider_id'] == int(target_id))
        revision = item['keys_revision' if action == 'keys-add' else 'models_revision']
        return {'provider_uuid': item['provider_uuid'], 'expected_revision': revision, 'idempotency_key': uuid4().hex}
    kind = action.split('-')[0]
    item = next(row for provider in providers for row in provider['api_keys' if kind == 'key' else 'models']
                if row[kind + '_id'] == int(target_id))
    result = {kind + '_uuid': item[kind + '_uuid'], 'expected_revision': item['revision'], 'idempotency_key': uuid4().hex}
    if action == 'key-reset':
        result['expected_health_revision'] = item['health_revision']
    return result


def save_then_read_catalogue(client, method, path, **kwargs):
    """Legacy behavior fixtures explicitly read before saving, then read the snapshot."""
    parts = path.split('/ai/', 1)[1].split('/')
    target_id = int(parts[1])
    if parts[0] == 'providers':
        action = 'keys-add' if parts[2] == 'keys' else 'models-add'
    else:
        action = parts[0][:-1] + '-' + (parts[2] if len(parts) > 2 else 'delete')
    field = 'json' if path.startswith('/api/') else 'data'
    kwargs[field] = {**observed_catalogue(client, action, target_id), **kwargs.get(field, {})}
    response = client.request(method, path, **kwargs)
    if field == 'json' and response.status_code == 200:
        assert response.json()['status'] == 'COMPLETED'
        return client.get('/api/v1/settings/ai')
    return response


def observed_chain(client, scope, model_id):
    section = {'default': 'ai', 'archive_path': 'paths', 'candidate_admission': 'parse'}[scope]
    response = client.get('/api/v1/settings/' + section)
    assert response.status_code == 200, response.text
    snapshot = response.json()
    catalogue = snapshot if scope == 'default' else snapshot['ai' if scope == 'archive_path' else 'candidate_ai']
    model = next(row for row in catalogue['selectable_models'] if row['model_id'] == int(model_id))
    return {'model_uuid': model['model_uuid'], 'expected_revision': snapshot['settings_versions']['ai-chain:' + scope],
            'idempotency_key': uuid4().hex}


def save_then_read_chain(client, method, path, **kwargs):
    field = 'json' if path.startswith('/api/') else 'data'
    values = kwargs.get(field, {})
    scope = path.rsplit('/', 1)[-1] if field == 'json' else (
        'archive_path' if '/paths/' in path else 'candidate_admission' if '/parse/' in path else 'default')
    kwargs[field] = {**observed_chain(client, scope, values['model_id']), **values}
    response = client.request(method, path, **kwargs)
    if field == 'json' and response.status_code == 200:
        assert response.json()['status'] == 'COMPLETED'
        return client.get('/api/v1/settings/' + {'default': 'ai', 'archive_path': 'paths', 'candidate_admission': 'parse'}[scope])
    return response
