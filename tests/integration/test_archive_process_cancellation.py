"""Real child processes, including header probes, are stopped and reaped."""
import asyncio
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest

from app.archive.backends.seven_zip import SevenZipBackend
from app.archive.errors import ArchiveError
from app.archive.models import ArchiveMember, ToolProfile
from app.downloads.execution import execution_scope, run_sync
from app.downloads.service import DownloadService
from tests.integration.test_queue_control import database, observed, pending


def backend(timeout=30):
    return SevenZipBackend(ToolProfile(1, "test process", "SEVEN_ZIP", "CLI", sys.executable,
                                       ("7z",), timeout, (), True))


def record_processes(monkeypatch, *, header_script=None):
    processes = []
    original = subprocess.Popen
    def start(arguments, **kwargs):
        if header_script is not None:
            arguments = [sys.executable, "-c", header_script]
        process = original(arguments, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr("app.archive.processes.subprocess.Popen", start)
    return processes


async def wait_for_file(path):
    deadline = time.monotonic() + 3
    while not path.exists():
        assert time.monotonic() < deadline, "test child did not start"
        await asyncio.sleep(0.01)


def assert_child_exited(process_id):
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x00100000, False, process_id)  # SYNCHRONIZE
        if handle:
            try:
                assert kernel.WaitForSingleObject(handle, 1000) == 0
            finally:
                kernel.CloseHandle(handle)
    elif Path(f"/proc/{process_id}/stat").exists():
        # A reparented zombie has exited and cannot keep producing bytes.
        assert Path(f"/proc/{process_id}/stat").read_text().split(")", 1)[1].strip().startswith("Z")
    else:
        with pytest.raises(ProcessLookupError):
            os.kill(process_id, 0)


def test_successful_subprocess_preserves_output_and_reaps(monkeypatch):
    processes = record_processes(monkeypatch)
    code, output = backend()._run_subprocess(("-c", "import sys; print('result'); print('diagnostic', file=sys.stderr)"))
    assert code == 0 and "result" in output and "diagnostic" in output
    assert processes[0].poll() == 0
    assert processes[0].stdout.closed and processes[0].stderr.closed


def test_timeout_kills_and_reaps_real_process(monkeypatch):
    processes = record_processes(monkeypatch)
    with pytest.raises(ArchiveError) as error:
        backend(timeout=1)._run_subprocess(("-c", "import time; time.sleep(30)"))
    assert error.value.code == "ARCHIVE_TOOL_TIMEOUT"
    assert processes[0].poll() is not None and processes[0].stdout.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("probe", [False, True])
async def test_queue_cancel_stops_real_tool_and_descendant(database, tmp_path, monkeypatch, probe):
    marker = tmp_path / "child.pid"
    child = "import time; time.sleep(30)"
    script = ("import pathlib, subprocess, sys, time; "
              f"p = subprocess.Popen([sys.executable, '-c', {child!r}]); "
              f"pathlib.Path({str(marker)!r}).write_text(str(p.pid)); time.sleep(30)")
    processes = record_processes(monkeypatch, header_script=script if probe else None)
    tool = backend()
    async def handler(job):
        if probe:
            await run_sync(tool.read_member_header, (tmp_path / "source.7z",),
                           ArchiveMember("1.jpg", 10, 5), None)
        else:
            await run_sync(tool._run_subprocess, ("-c", script))
    service = DownloadService(database, tmp_path)
    service._handle_job = handler
    job_id = pending(database)
    worker = asyncio.create_task(service._process_one())
    stopping = None
    try:
        await wait_for_file(marker)
        receipt = await service.command(job_id, "cancel", **observed(database, job_id))
        stopping = asyncio.create_task(service.process_operations())
        assert await asyncio.wait_for(stopping, 3) == 1
        assert await asyncio.wait_for(worker, 3) is True
        assert processes[0].poll() is not None and processes[0].stdout.closed
        assert_child_exited(int(marker.read_text()))
        assert (await service.get_task(receipt["task_id"]))["status"] == "CANCELLED"
    finally:
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, *( [stopping] if stopping is not None else []), return_exceptions=True)


@pytest.mark.asyncio
async def test_direct_thread_await_cancellation_stops_tool_without_database_revocation(database, tmp_path, monkeypatch):
    marker = tmp_path / "started"
    script = f"import pathlib, time; pathlib.Path({str(marker)!r}).touch(); time.sleep(30)"
    processes = record_processes(monkeypatch)
    service = DownloadService(database, tmp_path)
    job_id = pending(database)
    job = service._claim_pending_job_sync()
    async def run():
        with execution_scope(database.path, job):
            await run_sync(backend()._run_subprocess, ("-c", script))
    worker = asyncio.create_task(run())
    await wait_for_file(marker)
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(worker, 3)
    assert processes[0].poll() is not None
    with database.connection() as c:
        assert c.execute("SELECT state FROM download_jobs WHERE id = ?", (job_id,)).fetchone()[0] == "DOWNLOADING"


@pytest.mark.asyncio
async def test_revoked_identity_cannot_start_subprocess(database, tmp_path, monkeypatch):
    processes = record_processes(monkeypatch)
    service = DownloadService(database, tmp_path)
    job_id = pending(database)
    job = service._claim_pending_job_sync()
    await service.command(job_id, "cancel", **observed(database, job_id))
    with execution_scope(database.path, job):
        with pytest.raises(sqlite3.IntegrityError, match="QUEUE_STALE_EXECUTION"):
            await run_sync(backend()._run_subprocess, ("-c", "print('never')"))
    assert processes == []
