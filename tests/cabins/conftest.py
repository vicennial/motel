"""Make the in-tree package and the shared fakes importable without installing anything."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(os.path.dirname(HERE)), "src")
for path in (SRC, HERE):
    if path not in sys.path:
        sys.path.insert(0, path)
