"""Read explicit credential edit conditions from the public snapshot."""
from uuid import uuid4


def observed_credentials(client, action, password_id=None):
    response = client.get('/api/v1/settings/passwords')
    assert response.status_code == 200, response.text
    snapshot = response.json()
    scope = 'api-key' if action.startswith('api-key-') else 'vault'
    result = {'expected_revision': snapshot['settings_versions'][scope], 'idempotency_key': uuid4().hex}
    if action == 'delete' and password_id is not None:
        item = next(row for row in snapshot['passwords'] if row['password_id'] == password_id)
        result.update(password_uuid=item['password_uuid'], expected_revision=item['revision'])
    return result
