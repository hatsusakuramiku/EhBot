"""Legacy behavior tests explicitly read rule preconditions before a write."""
from uuid import uuid4


def observed_rule(client, domain, rule_id=None):
    if rule_id is None:
        return {'expected_revision': 0, 'idempotency_key': str(uuid4())}
    response = client.get('/api/v1/settings/' + domain)
    assert response.status_code == 200, response.text
    row = next(row for row in response.json()['rules' if domain == 'auto-approval' else 'path_rules']
               if row['rule_id'] == rule_id)
    return {'rule_uuid': row['rule_uuid'], 'expected_revision': row['revision'], 'idempotency_key': str(uuid4())}
