"""POSIX archive guardian; an EOF from the backend kills its entire tool group.

Executed as a standalone child, avoiding preexec_fn in a threaded backend.
The guardian inherits the worker's flock descriptor until the group exits.
"""
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time


def reap_group():
    """Keep the group leader alive until no descendant can produce bytes.

    pidfds prevent a recycled PID from directing a signal at another process.
    Older kernels without pidfds wait safely for the backend's group timeout.
    """
    while True:
        alive = False
        for entry in Path('/proc').iterdir():
            if not entry.name.isdigit() or int(entry.name) == os.getpid():
                continue
            descriptor = None
            try:
                stat = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
                if int(stat[2]) != os.getpgrp() or stat[0] == 'Z':
                    continue
                alive = True
                descriptor = os.pidfd_open(int(entry.name))
                # Recheck membership after obtaining the stable process handle.
                stat = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
                if int(stat[2]) == os.getpgrp():
                    signal.pidfd_send_signal(descriptor, signal.SIGKILL)
            except (OSError, AttributeError):
                pass
            finally:
                if descriptor is not None:
                    os.close(descriptor)
        if not alive:
            return
        time.sleep(0.01)


def main():
    read_fd = int(sys.argv[1])
    def watch_parent():
        try:
            while os.read(read_fd, 1):
                pass
        finally:
            os.killpg(os.getpgrp(), signal.SIGKILL)
    threading.Thread(target=watch_parent, daemon=True).start()
    result = subprocess.Popen(sys.argv[2:], close_fds=True).wait()
    reap_group()
    if result < 0:
        os.kill(os.getpid(), -result)
    return result


if __name__ == '__main__':
    sys.exit(main())
