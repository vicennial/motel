"""Filesystem helpers for the root desk: a bounded-descriptor, device-confined tree removal."""

from __future__ import annotations

import os
import stat

from motel.errors import Guard

OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _st_dev(st) -> int:
    return st.st_dev


def safe_rmtree(top: str, *, expect_dev: int | None = None, dev_of=_st_dev) -> int:
    """Remove the directory tree at ``top`` and return the number of entries removed.

    - Works on directory descriptors with ``O_NOFOLLOW``: a symlink is unlinked, never followed.
    - Iterative, and holds exactly one directory descriptor at a time (it climbs back with ``..`` and
      verifies the parent's identity), so tree depth cannot exhaust descriptors or the Python stack.
    - Never leaves the filesystem it started on: a directory whose device differs from the top's (or from
      ``expect_dev``) stops the removal with Guard before anything inside it is touched.
    - Entries that appear while it runs make the final ``rmdir`` fail with OSError; the caller reports it and
      the next housekeeping run finishes the job.
    """
    removed = 0
    fd = os.open(top, OPEN_FLAGS)
    try:
        st = os.fstat(fd)
        dev = dev_of(st)
        if expect_dev is not None and dev != expect_dev:
            raise Guard(f"{top} is on a different filesystem than its floor; refusing to delete it", "motel doctor")
        # stack of (name in parent, device, inode, pending subdirectory names) for every directory above us
        stack = [(None, dev, st.st_ino, None)]
        while True:
            name, d_dev, d_ino, pending = stack[-1]
            if pending is None:
                pending = []
                with os.scandir(fd) as it:
                    entries = list(it)
                for entry in entries:
                    est = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
                    if stat.S_ISDIR(est.st_mode):
                        if dev_of(est) != dev:
                            raise Guard(f"{entry.name!r} inside {top} is on another filesystem; refusing to descend", "motel doctor")
                        pending.append(entry.name)
                    else:
                        os.unlink(entry.name, dir_fd=fd)
                        removed += 1
                stack[-1] = (name, d_dev, d_ino, pending)
            if pending:
                child_name = pending.pop()
                child = os.open(child_name, OPEN_FLAGS, dir_fd=fd)
                cst = os.fstat(child)
                if dev_of(cst) != dev:
                    os.close(child)
                    raise Guard(f"{child_name!r} inside {top} is on another filesystem; refusing to descend", "motel doctor")
                os.close(fd)
                fd = child
                stack.append((child_name, dev_of(cst), cst.st_ino, None))
                continue
            # this directory is empty: climb back up and remove it
            if len(stack) == 1:
                break
            stack.pop()
            parent = os.open("..", OPEN_FLAGS, dir_fd=fd)
            pst = os.fstat(parent)
            if (dev_of(pst), pst.st_ino) != (stack[-1][1], stack[-1][2]):
                os.close(parent)
                raise Guard(f"the directory tree under {top} changed while it was being removed", "motel doctor")
            os.close(fd)
            fd = parent
            os.rmdir(name, dir_fd=fd)
            removed += 1
    finally:
        os.close(fd)
    os.rmdir(top)
    return removed + 1
