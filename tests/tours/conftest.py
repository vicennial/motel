"""Make the in-tree package and the shared cabin fakes importable without installing anything."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = os.path.dirname(HERE)
SRC = os.path.join(os.path.dirname(TESTS), "src")
for path in (SRC, HERE, os.path.join(TESTS, "cabins")):
    if path not in sys.path:
        sys.path.insert(0, path)

import pytest  # noqa: E402


@pytest.fixture
def rig(tmp_path, monkeypatch):
    from rig import make
    return make(tmp_path, monkeypatch)
