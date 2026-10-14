-- Completed execution facts must survive later task-control and remote cleanup
-- transitions. The active task and cleanup operation carry the new intent.
DROP TRIGGER queue_job_update;
CREATE TRIGGER queue_job_update AFTER UPDATE OF state, priority, details_json, candidate_id ON download_jobs
WHEN NOT (OLD.state IN ('COMPLETED', 'FAILED', 'CANCELLED', 'CONVERSION_COMPLETED', 'CONVERSION_FAILED')
          AND NEW.state IN ('PENDING', 'CONVERSION_PENDING')) BEGIN
    INSERT OR IGNORE INTO candidate_approvals(candidate_id, evidence)
    SELECT NEW.candidate_id, evidence FROM candidate_approvals
    WHERE candidate_id = OLD.candidate_id AND NEW.candidate_id <> OLD.candidate_id;
    UPDATE queue_tasks SET status = NEW.state, priority = NEW.priority,
        candidate_id = NEW.candidate_id, details_json = NEW.details_json,
        revision = revision + CASE WHEN OLD.state <> NEW.state OR OLD.priority <> NEW.priority THEN 1 ELSE 0 END,
        generation = generation + CASE WHEN OLD.state <> NEW.state AND NEW.state IN ('STOPPING', 'CANCELLED') THEN 1 ELSE 0 END,
        updated_at = CURRENT_TIMESTAMP WHERE task_id = NEW.task_id;
    UPDATE queue_attempts SET status = NEW.state, details_json = NEW.details_json,
        error_code = NEW.error_code, error_message = NEW.error_message,
        finished_at = CASE WHEN NEW.state IN ('COMPLETED', 'FAILED', 'CANCELLED', 'CONVERSION_COMPLETED', 'CONVERSION_FAILED',
            'CONVERSION_WAITING_PASSWORD', 'CONVERSION_WAITING_VOLUMES', 'CONVERSION_WAITING_PATH')
            THEN CURRENT_TIMESTAMP ELSE finished_at END
    WHERE attempt_id = (SELECT current_attempt_id FROM queue_tasks WHERE task_id = NEW.task_id)
      AND finished_at IS NULL;
END;
