"""Commit guards for account requests that may run outside a queue attempt."""
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
import sqlite3


@dataclass(frozen=True, slots=True)
class AccountExecution:
    database_path: Path
    account_id: str
    credential_revision: int
    require_enabled: bool

    def check(self, connection: sqlite3.Connection) -> None:
        """Reject old metadata/ingestion writes inside their committing transaction."""
        row = connection.execute(
            "SELECT 1 FROM external_accounts WHERE account_id = ? AND credential_revision = ? "
            "AND deleted_at IS NULL AND stop_operation_id IS NULL "
            "AND (? = 0 OR (enabled = 1 AND verified_credential_revision = credential_revision))",
            (self.account_id, self.credential_revision, int(self.require_enabled)),
        ).fetchone()
        if row is None:
            raise sqlite3.IntegrityError("ACCOUNT_STALE_EXECUTION")


current_account_executions: ContextVar[tuple[AccountExecution, ...]] = ContextVar("account_executions", default=())
