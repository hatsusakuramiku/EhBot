"""Explicit client-side reads for tests of versioned work commands."""
from uuid import uuid4


def observed_work(client, candidate_id=1):
    response = client.get(f'/api/v1/works/{candidate_id}')
    assert response.status_code == 200, response.text
    editor = response.json()['metadata_editor']
    return {'work_id': editor['work_id'], 'expected_revision': editor['revision'],
            'idempotency_key': uuid4().hex}


def observed_review(client, candidate_id=1):
    response = client.get(f'/api/v1/works/{candidate_id}')
    assert response.status_code == 200, response.text
    snapshot = response.json()
    return {'work_id': snapshot['work_id'], 'expected_revision': snapshot['revision'],
            'idempotency_key': uuid4().hex}


def observed_review_batch(client, candidate_ids):
    return {'idempotency_key': uuid4().hex,
            'versions': {str(candidate_id): observed_review(client, candidate_id) for candidate_id in candidate_ids}}


def observed_review_form(client, candidate_ids):
    payload = observed_review_batch(client, candidate_ids)
    return {'idempotency_key': payload['idempotency_key'], **{
        field + '_' + candidate_id: value for candidate_id, version in payload['versions'].items()
        for field, value in version.items()}}
