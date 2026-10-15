"""Commit automatic review, queue admission and rule evidence as one result."""
import json

from app.auto_approval.service import AutomaticApprovalService
from app.review.models import AUTO_OPERATOR, REVIEW_APPROVE, REVIEW_AUTO_APPROVE, REVIEW_AUTO_REJECT
from app.review.service import ReviewError


def begin_decision(database, connection, candidate_id, match):
    """Re-evaluate the matched identity/rules/fields inside the final write lock."""
    fresh = AutomaticApprovalService(database).matching_rule_in_transaction(connection, candidate_id)
    changing = connection.execute(
        "SELECT 1 FROM ingest_reference_changes WHERE candidate_id = ? AND status = 'PENDING' "
        "UNION ALL SELECT 1 FROM ingest_observation_changes WHERE candidate_id = ? AND status = 'PENDING' LIMIT 1",
        (candidate_id, candidate_id),
    ).fetchone()
    if fresh != match or changing:
        raise ReviewError('AUTOMATIC_DECISION_CHANGED', '候选或规则已变化，自动判定将在后续重新核对')
    approve = match.rule.action == REVIEW_APPROVE
    database._transition_candidate_status_sync(candidate_id, AUTO_OPERATOR, match.rule.action,
        'APPROVED' if approve else 'REJECTED', None if approve else f'命中规则「{match.rule.name}」', connection)


def record_decision(connection, candidate_id, match, job_ids):
    """The audit is inseparable from the applied status and any queued job."""
    connection.execute(
        'INSERT INTO review_actions(candidate_id, action, operator_name, details_json) VALUES(?, ?, ?, ?)',
        (candidate_id, REVIEW_AUTO_APPROVE if match.rule.action == REVIEW_APPROVE else REVIEW_AUTO_REJECT,
         AUTO_OPERATOR, json.dumps({'rule_id': match.rule.rule_id, 'rule_name': match.rule.name,
             'rule_version': match.rule.version, 'dsl_snapshot': match.rule.dsl_snapshot,
             'condition': match.rule.condition, 'conditions': match.conditions, 'metadata': match.metadata,
             'download_job_ids': job_ids})),
    )


def reject_automatically(database, candidate_id, match):
    with database.connection() as connection:
        connection.execute('BEGIN IMMEDIATE')
        begin_decision(database, connection, candidate_id, match)
        record_decision(connection, candidate_id, match, [])
