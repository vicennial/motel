"""pytest: the harness unit tests (test_harness.py) always run. The live pass itself is never collected by default."""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for extra in (HERE, os.path.join(HERE, "fakes")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

collect_ignore = [] if os.environ.get("MOTEL_LIVE") == "1" else ["test_live_pass.py"]
