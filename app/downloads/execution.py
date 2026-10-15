"""Execution identity and cancellation boundaries shared by download providers."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
import sqlite3
from threading import Event


@dataclass(frozen=True, slots=True)
class Execution:
    database_path: Path
    task_id: str
    attempt_id: str
    generation: int
    stop_requested: Event = field(default_factory=Event, compare=False)
    worker_session_id: str | None = None
    worker_lock_fd: int | None = field(default=None, repr=False)

    def check(self, connection: sqlite3.Connection) -> None:
        """Validate the producing attempt inside its write transaction."""
        row = connection.execute(
            "SELECT 1 FROM queue_tasks t JOIN download_jobs j ON j.task_id = t.task_id "
            "WHERE t.task_id = ? AND t.current_attempt_id = ? AND t.generation = ? "
            "AND t.status NOT IN ('STOPPING', 'CANCELLED', 'REMOVED')",
            (self.task_id, self.attempt_id, self.generation),
        ).fetchone()
        if row is None:
            raise sqlite3.IntegrityError("QUEUE_STALE_EXECUTION")
        if self.worker_session_id is not None and connection.execute(
            "SELECT 1 FROM worker_sessions WHERE session_id = ? AND status = 'LIVE'",
            (self.worker_session_id,),
        ).fetchone() is None:
            raise sqlite3.IntegrityError("QUEUE_STALE_EXECUTION")


current_execution: ContextVar[Execution | None] = ContextVar("download_execution", default=None)


def check_thread_cancelled() -> None:
    """Cooperatively stop bounded I/O chunks without a database read per chunk."""
    execution = current_execution.get()
    if execution is not None and execution.stop_requested.is_set():
        raise sqlite3.IntegrityError("QUEUE_STALE_EXECUTION")


@contextmanager
def execution_scope(database_path: Path, job: dict):
    """Bind a claimed identity; to_thread copies this context to provider writes."""
    execution = Execution(database_path, job["task_id"], job["attempt_id"], job["generation"],
                          worker_session_id=job.get("worker_session_id"), worker_lock_fd=job.get('_worker_lock_fd'))
    token = current_execution.set(execution)
    try:
        yield execution
    finally:
        current_execution.reset(token)


def attempt_path(destination: Path) -> Path:
    """Persist the isolated directory before a producer can create any bytes."""
    execution = current_execution.get()
    if execution is None:
        return destination
    result = destination.parent / "attempts" / execution.task_id / execution.attempt_id / destination.name
    connection = sqlite3.connect(execution.database_path)
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            execution.check(connection)
            connection.execute(
                "INSERT OR IGNORE INTO queue_attempt_paths(task_id, attempt_id, directory) VALUES(?, ?, ?)",
                (execution.task_id, execution.attempt_id, str(result.parent)),
            )
    finally:
        connection.close()
    return result


@contextmanager
def failure_transaction(database):
    """Validate once under the write lock, then allow a failure to revoke itself."""
    execution = current_execution.get()
    token = current_execution.set(None)
    try:
        with database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if execution is not None:
                execution.check(connection)
            yield connection
    finally:
        current_execution.reset(token)


async def run_sync(function, /, *args, **kwargs):
    """Wait for a thread to finish even when its awaiting coroutine is cancelled.

    Python cannot kill a running thread. Cancellation therefore waits for its
    actual exit before propagating; callers must never release a slot earlier.
    """
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        execution = current_execution.get()
        if execution is not None:
            execution.stop_requested.set()
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        # Retrieve a late failure without letting it mask the cancellation.
        if not task.cancelled():
            task.exception()
        raise
