-- Internal account replacement resumes the same logical task only after the
-- existing operation processor has confirmed physical exit. Explicit retries
-- retain their existing new-task semantics; migrations 027-030 stay frozen.
DROP TRIGGER queue_stopping_guard;
CREATE TRIGGER queue_stopping_guard BEFORE UPDATE OF state ON download_jobs
WHEN OLD.state = 'STOPPING' AND NEW.state NOT IN ('STOPPING', 'CANCELLED')
  AND NOT (NEW.state IN ('PENDING', 'FAILED') AND EXISTS (
      SELECT 1 FROM queue_operations o JOIN queue_tasks t ON t.task_id = o.task_id
      WHERE o.task_id = OLD.task_id AND o.legacy_job_id = OLD.id
        AND o.attempt_id = t.current_attempt_id AND o.generation + 1 = t.generation
        AND o.action = 'replace-account' AND o.status = 'COMPLETED'
  )) BEGIN
    SELECT RAISE(ABORT, 'QUEUE_STOP_NOT_CONFIRMED');
END;
