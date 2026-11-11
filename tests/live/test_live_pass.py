"""The real live pass as a test. Skipped unless MOTEL_LIVE=1, and then it needs the arguments in MOTEL_LIVE_ARGS
(for example: --hcloud-cmd 'WRAPPER --' --token-file PATH --ref HEAD). It creates and deletes real cloud resources."""

import os
import shlex
import unittest

import run_live


@unittest.skipUnless(os.environ.get("MOTEL_LIVE") == "1", "set MOTEL_LIVE=1 (and MOTEL_LIVE_ARGS) to run against a real cloud project")
class LivePass(unittest.TestCase):
    def test_live_pass(self):
        self.assertEqual(0, run_live.main(shlex.split(os.environ["MOTEL_LIVE_ARGS"])))
