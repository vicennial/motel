"""Make the in-tree package and the shared fakes importable without installing anything."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(os.path.dirname(HERE)), "src")
for path in (SRC, HERE):
    if path not in sys.path:
        sys.path.insert(0, path)


import shutil  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture
def sock_dir():
    """A short directory for unix sockets: pytest's tmp_path can exceed the socket path limit when TMPDIR is long."""
    from concierge_fakes import short_dir
    path = short_dir()
    yield path
    shutil.rmtree(path, ignore_errors=True)
