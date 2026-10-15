"""Durable post-publication work owned by the existing approval worker."""


def due_followups(database, limit):
    """Read due identity/version pairs; supplier calls never hold this connection."""
    with database.connection() as connection:
        return connection.execute(
            "SELECT e.id, e.generation, e.followup_revision, e.candidate_id FROM ingest_entries e "
            "WHERE e.processing_state = 'LINKED' AND e.followup_revision > e.followup_done_revision "
            "AND (e.followup_retry_at IS NULL OR e.followup_retry_at <= unixepoch()) "
            "AND NOT EXISTS(SELECT 1 FROM ingest_reference_changes r WHERE r.candidate_id = e.candidate_id AND r.status = 'PENDING') "
            "AND NOT EXISTS(SELECT 1 FROM ingest_observation_changes o WHERE o.candidate_id = e.candidate_id AND o.status = 'PENDING') "
            "ORDER BY e.followup_retry_at, e.id LIMIT ?", (limit,),
        ).fetchall()


def followup_ready(database, candidate_id):
    """Do not forget a pending automatic decision when enrichment is unavailable."""
    with database.connection() as connection:
        row = connection.execute('SELECT status FROM candidates WHERE id = ?', (candidate_id,)).fetchone()
        if row is None or row[0] not in {'PENDING_REVIEW', 'NEEDS_INFO', 'APPROVED'}:
            return True
        if row[0] == 'APPROVED':
            # A partial legacy approve/enqueue must remain recoverable instead
            # of silently acknowledging a decision without a download task.
            return connection.execute('SELECT 1 FROM download_jobs WHERE candidate_id = ?', (candidate_id,)).fetchone() is not None
        return (connection.execute('SELECT 1 FROM auto_approval_rules WHERE enabled = 1 LIMIT 1').fetchone() is None
                or connection.execute("SELECT 1 FROM metadata_values WHERE candidate_id = ? AND value_source = 'EXHENTAI' LIMIT 1",
                                      (candidate_id,)).fetchone() is not None)


def finish_followup(database, identity, completed):
    """Acknowledge only this published generation/version, or persist capped backoff.

    completed=True acknowledges the attempted decision; False retains it after
    a failure or missing metadata. Retries back off at 30, 120, then 600 seconds.
    """
    entry_id, generation, revision, candidate_id = identity
    with database.connection() as connection:
        connection.execute('BEGIN IMMEDIATE')
        row = connection.execute(
            "SELECT followup_attempts FROM ingest_entries WHERE id = ? AND generation = ? "
            "AND followup_revision = ? AND candidate_id = ? AND processing_state = 'LINKED'",
            (entry_id, generation, revision, candidate_id),
        ).fetchone()
        if row is None:
            return
        if completed:
            connection.execute(
                'UPDATE ingest_entries SET followup_done_revision = ?, followup_attempts = 0, followup_retry_at = NULL WHERE id = ?',
                (revision, entry_id),
            )
        else:
            delay = (30, 120, 600)[min(row[0], 2)]
            connection.execute(
                'UPDATE ingest_entries SET followup_attempts = followup_attempts + 1, followup_retry_at = unixepoch() + ? WHERE id = ?',
                (delay, entry_id),
            )
