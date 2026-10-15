"""Initial queue bounds, checked in the same transaction as reservation."""


REMOTE_INFLIGHT = 1
MAX_AUTO_RECOVERIES = 3


def record_interruption(connection, job_id):
    connection.execute('UPDATE download_jobs SET recovery_count = recovery_count + 1 WHERE id = ?', (job_id,))
    return connection.execute('SELECT recovery_count FROM download_jobs WHERE id = ?', (job_id,)).fetchone()[0] > MAX_AUTO_RECOVERIES


def local_available(database, connection, kind):
    session = getattr(database, 'worker_session', None)
    if session is not None and session.stopping:
        return False
    state = 'DOWNLOADING' if kind == 'DOWNLOAD' else 'CONVERSION_RUNNING'
    for job_id, attempt_id in connection.execute('SELECT j.id, t.current_attempt_id FROM download_jobs j '
            'JOIN queue_tasks t ON t.task_id = j.task_id WHERE j.state = ?', (state,)):
        if session is None or not session.reclaimable(connection, job_id, attempt_id):
            return False
    if session is not None:
        # A cancelled attempt still owns its local slot during physical cleanup.
        return not any(session.active(row[0]) for row in connection.execute(
            "SELECT a.attempt_id FROM queue_attempts a JOIN queue_tasks t ON t.task_id = a.task_id "
            "WHERE a.kind = ? AND t.status = 'STOPPING'", (kind,)))
    return True


def remote_available(connection, task_id=None):
    count = connection.execute("SELECT count(*) FROM ("
        "SELECT task_id FROM queue_remote_resources WHERE status <> 'STOP_CONFIRMED' AND download_finished = 0 "
        "UNION SELECT task_id FROM download_jobs WHERE provider = 'EH_TORRENT' "
        "AND state IN ('DOWNLOADING', 'WAITING_TORRENT', 'STOPPING')) "
        'WHERE (? IS NULL OR task_id <> ?)', (task_id, task_id)).fetchone()[0]
    return count < REMOTE_INFLIGHT
