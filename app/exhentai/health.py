"""Persist shared EH backoff and capability health for immutable credentials."""
from contextlib import closing

from app.downloads.execution import run_sync
from app.exhentai.errors import ExHentaiDownloadError, DEFAULT_RETRY_AFTER


class ExHentaiHealth:
    def __init__(self, database):
        self._database = database

    @staticmethod
    def shared_failure(connection, *, public=False):
        scopes = ("SITE", "GDATA") if public else ("SITE", "SITE")
        row = connection.execute(
            "SELECT error_code, MAX(1, CAST(strftime('%s', cooldown_until) AS INTEGER) - "
            "CAST(strftime('%s', 'now') AS INTEGER)), scope FROM eh_shared_backoff "
            "WHERE scope IN (?, ?) AND cooldown_until > CURRENT_TIMESTAMP ORDER BY cooldown_until DESC LIMIT 1",
            scopes,
        ).fetchone()
        if row is not None:
            return ExHentaiDownloadError(row[0], "EH 请求处于共享冷却，请稍后重试", failure_scope=row[2].lower(), retry_after=row[1])
        return None

    @staticmethod
    def account_failure(connection, account_id, credential_revision, capability="AUTH"):
        return connection.execute(
            "SELECT error_code, cooldown_until FROM eh_account_health "
            "WHERE account_id = ? AND credential_revision = ? AND capability IN ('AUTH', ?) "
            "AND (invalid = 1 OR cooldown_until > CURRENT_TIMESTAMP) "
            "ORDER BY invalid DESC, cooldown_until DESC LIMIT 1",
            (account_id, credential_revision, capability),
        ).fetchone()

    async def check_shared(self, *, public=False):
        def check():
            with self._database.connection() as connection:
                error = self.shared_failure(connection, public=public)
            if error is not None:
                raise error
        await run_sync(check)

    async def record_failure(self, error, *, account_id=None, credential_revision=None, capability="METADATA"):
        await run_sync(self._record_failure, error, account_id, credential_revision, capability)

    def _record_failure(self, error, account_id, credential_revision, capability):
        scope = getattr(error, "failure_scope", "operation")
        if scope not in {"site", "gdata", "credential", "capability"}:
            return
        # Diagnostics are independent of a revoked producer fence. Account
        # health always targets the observed credential version, never a newer one.
        with closing(self._database._connect()) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                if scope in {"site", "gdata"}:
                    seconds = error.retry_after or DEFAULT_RETRY_AFTER
                    connection.execute(
                        "INSERT INTO eh_shared_backoff(scope, cooldown_until, error_code) "
                        "VALUES(?, datetime('now', ?), ?) ON CONFLICT(scope) DO UPDATE SET "
                        "cooldown_until = MAX(eh_shared_backoff.cooldown_until, excluded.cooldown_until), "
                        "error_code = CASE WHEN excluded.cooldown_until >= eh_shared_backoff.cooldown_until "
                        "THEN excluded.error_code ELSE eh_shared_backoff.error_code END",
                        (scope.upper(), f"+{seconds} seconds", error.code),
                    )
                elif account_id is not None:
                    invalid = int(scope == "credential")
                    seconds = error.retry_after or DEFAULT_RETRY_AFTER
                    connection.execute(
                        "INSERT INTO eh_account_health(account_id, credential_revision, capability, invalid, cooldown_until, error_code) "
                        "VALUES(?, ?, ?, ?, CASE WHEN ? = 0 THEN datetime('now', ?) END, ?) "
                        "ON CONFLICT(account_id, credential_revision, capability) DO UPDATE SET "
                        "invalid = excluded.invalid, cooldown_until = excluded.cooldown_until, error_code = excluded.error_code",
                        (account_id, credential_revision, "AUTH" if invalid else capability, invalid, invalid,
                         f"+{seconds} seconds", error.code),
                    )
