"""Own archive subprocesses and their descendants until confirmed exit."""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time

from app.downloads.execution import current_execution


def check_execution() -> None:
    """Poll cancellation and durable execution identity from a worker thread."""
    execution = current_execution.get()
    if execution is None:
        return
    if execution.stop_requested.is_set():
        raise sqlite3.IntegrityError("QUEUE_STALE_EXECUTION")
    connection = sqlite3.connect(execution.database_path)
    try:
        execution.check(connection)
    finally:
        connection.close()


def _windows_job():
    """A Windows Job kills descendants even after the direct child has exited."""
    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
            ("flags", wintypes.DWORD), ("minimum_working_set", ctypes.c_size_t),
            ("maximum_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
            ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD),
        ]
    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in
                    ("read_operations", "write_operations", "other_operations",
                     "read_bytes", "write_bytes", "other_bytes")]
    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("basic", BasicLimits), ("io", IoCounters),
                    ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                    ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
        "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
        "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        "QueryInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                       wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
        "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        "CreateToolhelp32Snapshot": ([wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
        "OpenThread": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        "ResumeThread": ([wintypes.HANDLE], wintypes.DWORD),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = arguments, result
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = ExtendedLimits()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        error = ctypes.WinError(ctypes.get_last_error())
        kernel.CloseHandle(job)
        raise error
    return kernel, job


def _stop_windows_job(kernel, job):
    """Wait for every descendant to exit before closing the Job and releasing files."""
    class Accounting(ctypes.Structure):
        _fields_ = [(name, ctypes.c_longlong) for name in
                    ("user_time", "kernel_time", "period_user_time", "period_kernel_time")]
        _fields_ += [(name, wintypes.DWORD) for name in
                     ("page_faults", "processes", "active_processes", "terminated_processes")]
    if not kernel.TerminateJobObject(job, 1):
        raise ctypes.WinError(ctypes.get_last_error())
    accounting = Accounting()
    while True:
        if not kernel.QueryInformationJobObject(job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
            raise ctypes.WinError(ctypes.get_last_error())
        if not accounting.active_processes:
            return
        time.sleep(0.01)


def _resume_windows_process(kernel, process_id):
    """Resume the primary thread only after the suspended process belongs to our Job."""
    class ThreadEntry(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD),
                    ("thread_id", wintypes.DWORD), ("process_id", wintypes.DWORD),
                    ("base_priority", wintypes.LONG), ("delta_priority", wintypes.LONG),
                    ("flags", wintypes.DWORD)]
    for name in ("Thread32First", "Thread32Next"):
        function = getattr(kernel, name)
        function.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        function.restype = wintypes.BOOL
    snapshot = kernel.CreateToolhelp32Snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = ThreadEntry()
        entry.size = ctypes.sizeof(entry)
        found = kernel.Thread32First(snapshot, ctypes.byref(entry))
        while found:
            if entry.process_id == process_id:
                thread = kernel.OpenThread(0x0002, False, entry.thread_id)  # THREAD_SUSPEND_RESUME
                if not thread:
                    raise ctypes.WinError(ctypes.get_last_error())
                try:
                    if kernel.ResumeThread(thread) == 0xFFFFFFFF:
                        raise ctypes.WinError(ctypes.get_last_error())
                    return
                finally:
                    kernel.CloseHandle(thread)
            found = kernel.Thread32Next(snapshot, ctypes.byref(entry))
        raise OSError("Archive process has no resumable thread")
    finally:
        kernel.CloseHandle(snapshot)


@contextmanager
def managed_process(arguments, **kwargs):
    """Use an isolated POSIX process group or Windows Job, then kill and reap."""
    check_execution()
    kernel, job = _windows_job() if os.name == "nt" else (None, None)
    process = None
    read_fd = write_fd = None
    try:
        # CREATE_SUSPENDED prevents a fast wrapper from spawning descendants
        # before it has been assigned to the owned Job.
        options = {"creationflags": subprocess.CREATE_NO_WINDOW | 0x00000004} if os.name == "nt" else {"start_new_session": True}
        if sys.platform == 'linux':
            read_fd, write_fd = os.pipe()
            execution = current_execution.get()
            inherited = (execution.worker_lock_fd,) if execution is not None and execution.worker_lock_fd is not None else ()
            options['pass_fds'] = (read_fd, *inherited)
            arguments = [sys.executable, str(Path(__file__).with_name('_supervisor.py')), str(read_fd), *arguments]
        process = subprocess.Popen(arguments, shell=False, **options, **kwargs)
        if read_fd is not None:
            os.close(read_fd)
            read_fd = None
        if kernel is not None and not kernel.AssignProcessToJobObject(job, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())
        if kernel is not None:
            _resume_windows_process(kernel, process.pid)
        yield process
    finally:
        try:
            if kernel is not None:
                try:
                    _stop_windows_job(kernel, job)
                finally:
                    kernel.CloseHandle(job)
            elif process is not None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        finally:
            if read_fd is not None:
                os.close(read_fd)
            if write_fd is not None:
                os.close(write_fd)
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait()
                for stream in (process.stdout, process.stderr, process.stdin):
                    if stream is not None:
                        stream.close()
