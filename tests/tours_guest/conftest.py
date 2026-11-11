"""Make the in-tree package and the cabin fakes importable without installing anything."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = os.path.dirname(HERE)
SRC = os.path.join(os.path.dirname(TESTS), "src")
for path in (SRC, os.path.join(TESTS, "cabins"), HERE):
    if path not in sys.path:
        sys.path.insert(0, path)
