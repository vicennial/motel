"""Host-wide concurrency slots, shared by every supervisor, held as ``flock`` on small files.

A slot is released when its holder exits, even by a crash, so a dead supervisor never strands one.
"""

from __future__ import annotations

import fcntl
import os
import time


class Slot:
    def __init__(self, fd: int):
        self.fd = fd

    def release(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class SlotPool:
    def __init__(self, folder: str, size: int, *, sleep=time.sleep, poll_s: float = 1.0):
        self.folder, self.size, self.sleep, self.poll_s = folder, max(1, size), sleep, poll_s
        os.makedirs(folder, mode=0o700, exist_ok=True)

    def try_acquire(self):
        for index in range(self.size):
            fd = os.open(os.path.join(self.folder, f"slot-{index}"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return Slot(fd)
            except BlockingIOError:
                os.close(fd)
        return None

    def acquire(self, cancel=None):
        """Wait for a free slot. Returns None if ``cancel()`` turns true first."""
        while True:
            if cancel is not None and cancel():
                return None
            slot = self.try_acquire()
            if slot is not None:
                return slot
            self.sleep(self.poll_s)
