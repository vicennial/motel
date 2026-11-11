"""Shared skip marker for tests that exercise parts of motel that only run on Linux.

The front desk, the installer, the Action driver, the concierge service and the tour guest are Linux-only by design
(mounts, ``losetup``, ``/proc``, ``SO_PEERCRED``, systemd fd store, GNU tools, ``prlimit``). The CLI client side, the rooms and
cabins client code and ``motel try`` are meant to run on a macOS laptop, so their tests are not marked and must pass there.
Only mark a test (or a module) when the code under test is on the Linux-only list; never mark one to hide a portability bug in
code that is meant to run everywhere.
"""

from __future__ import annotations

import sys

import pytest

ON_LINUX = sys.platform == "linux"

linux_only = pytest.mark.skipif(not ON_LINUX, reason="exercises a Linux-only part of motel (see tests/platform_marks.py)")
