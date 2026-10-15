"""Local physical ownership supplements the durable ingestion claim, never its identity."""
import asyncio


class IngestProcessors:
    """Share live tasks between Telegram and manual admission in one backend process."""

    def __init__(self):
        self.lock = asyncio.Lock()
        self.tasks: dict[int, asyncio.Task] = {}
        self._stopping: set[asyncio.Task] = set()
        self.recovery_cursors: dict[str | None, int] = {}

    def active_ids(self):
        return tuple(entry_id for entry_id, task in tuple(self.tasks.items()) if not task.done())

    async def drain(self, task):
        """Cancel once; a cancelled waiter still awaits actual provider cleanup."""
        if task not in self._stopping and not task.done():
            self._stopping.add(task)
            task.cancel()
        try:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not task.cancelled():
                task.exception()
        finally:
            self._stopping.discard(task)
