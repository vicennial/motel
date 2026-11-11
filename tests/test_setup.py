"""setup: discovery, the token prompt rules, never-touch recording and the receipt."""

import getpass
import io
import json
import os
import stat
import unittest
import warnings
from pathlib import Path
from unittest import mock

from helpers import SERVER, Fixture
from motel.errors import Guard, InventoryIncomplete, Refused, Usage
from motel.providers.fake import FakeProvider
from motel.setup import read_token_tty

TOKEN = "t" * 64


class SetupTest(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture(receipt=False)
        self.addCleanup(self.fx.close)
        self.cloud = FakeProvider(server_id=SERVER, location="fsn1")
        self.cloud.add(11, 100, name="data-volume", server_id=SERVER)
        self.cloud.add(12, 20, name="elsewhere", server_id="999")

    def setup(self, **hooks):
        out, err = io.StringIO(), io.StringIO()
        from motel import desk
        code = desk.run(["setup", "hetzner", "--json"], paths=self.fx.paths, caller=self.fx.caller, sysops=self.fx.sysops,
                        clock=self.fx.clock, out=out, err=err, hetzner_factory=hooks.get("factory", lambda token: self.cloud),
                        token_reader=hooks.get("reader", lambda: TOKEN))
        return code, out.getvalue(), err.getvalue()

    def as_root(self):
        from motel.rooms import Caller
        self.fx.caller = Caller(0, 0, "root")

    def test_setup_records_every_existing_volume_as_never_touch(self):
        self.as_root()
        code, out, _ = self.setup()
        self.assertEqual(code, 0, out)
        receipt = json.loads(Path(self.fx.paths.receipt_file).read_text())
        self.assertEqual(sorted(v["id"] for v in receipt["never_touch"]), ["11", "12"])
        self.assertEqual((receipt["server_id"], receipt["location"], receipt["provider"]), (SERVER, "fsn1", "hetzner"))
        self.assertNotIn(TOKEN, Path(self.fx.paths.receipt_file).read_text())
        self.assertNotIn(TOKEN, out)

    def test_token_is_stored_0600_in_a_0700_directory(self):
        self.as_root()
        self.setup()
        token = Path(self.fx.paths.token_file("hetzner"))
        self.assertEqual(token.read_text().strip(), TOKEN)
        self.assertEqual(stat.S_IMODE(token.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(token.parent.stat().st_mode), 0o700)

    def test_a_token_that_cannot_see_this_server_is_refused_and_nothing_is_written(self):
        self.as_root()
        other = FakeProvider(server_id=SERVER)
        other.visible = lambda: type("Inv", (), {"volumes": (), "servers": ("555",), "complete": True})()
        code, out, _ = self.setup(factory=lambda token: other)
        self.assertEqual(code, Refused.code)
        self.assertIn("different project", json.loads(out)["error"])
        self.assertFalse(os.path.exists(self.fx.paths.receipt_file))
        self.assertFalse(os.path.exists(self.fx.paths.token_file("hetzner")))

    def test_partial_inventory_refuses_instead_of_freezing_a_short_list(self):
        self.as_root()
        self.cloud.incomplete = True
        self.cloud.visible = lambda: (_ for _ in ()).throw(InventoryIncomplete("partial list"))
        code, _, _ = self.setup()
        self.assertEqual(code, 9)
        self.assertFalse(os.path.exists(self.fx.paths.receipt_file))

    def test_rerun_keeps_old_entries_and_does_not_freeze_motels_own_floors(self):
        self.as_root()
        self.setup()
        from motel.rooms import Rooms
        self.fx.provider = self.cloud
        # motel creates a floor, then setup runs again
        self.fx.ctx = type(self.fx.ctx)(cfg=self.fx.cfg, receipt=__import__("motel.config", fromlist=["Receipt"]).Receipt.load(self.fx.paths.receipt_file),
                                        provider=self.cloud, sysops=self.fx.sysops, store=__import__("motel.state", fromlist=["Store"]).Store(self.fx.paths.state_dir, self.fx.clock, SERVER),
                                        paths=self.fx.paths, clock=self.fx.clock)
        rooms = Rooms(self.fx.ctx)
        from datetime import timedelta

        from motel.rooms import Caller
        rooms.checkin(Caller(1000, 1000, "alice"), 10, timedelta(hours=1), "t", False)
        self.cloud.add(13, 10, name="new-after-setup", server_id=SERVER)
        code, out, _ = self.setup()
        self.assertEqual(code, 0, out)
        ids = sorted(v["id"] for v in json.loads(Path(self.fx.paths.receipt_file).read_text())["never_touch"])
        self.assertEqual(ids, ["11", "12", "13"])  # the floor motel itself created (900001) is not frozen

    def test_setup_for_a_different_server_is_refused(self):
        self.as_root()
        self.setup()
        self.cloud.server_id = "2000002"
        code, _, _ = self.setup()
        self.assertEqual(code, Guard.code)

    def test_only_root_may_run_setup(self):
        code, out, _ = self.setup()
        self.assertEqual(code, Refused.code)
        self.assertIn("operator command", out)


class TokenPromptTest(unittest.TestCase):
    class Stdin(io.StringIO):
        def __init__(self, tty):
            super().__init__("")
            self.tty = tty

        def isatty(self):
            return self.tty

    def test_non_tty_is_refused_so_tokens_never_come_from_pipes(self):
        with self.assertRaises(Usage):
            read_token_tty(self.Stdin(False))

    def test_echoing_terminal_is_refused(self):
        def echoing(prompt):
            warnings.warn("Can not control echo on the terminal.", getpass.GetPassWarning, stacklevel=2)
            return TOKEN
        with mock.patch("getpass.getpass", echoing), self.assertRaises(Usage):
            read_token_tty(self.Stdin(True))

    def test_hidden_prompt_returns_the_token(self):
        with mock.patch("getpass.getpass", lambda prompt: f"  {TOKEN}  "):
            self.assertEqual(read_token_tty(self.Stdin(True)), TOKEN)

    def test_garbage_is_refused(self):
        for bad in ("", "short", "has space " + "x" * 30):
            with mock.patch("getpass.getpass", lambda prompt, b=bad: b), self.assertRaises(Usage):
                read_token_tty(self.Stdin(True))


class SetupLocalTest(unittest.TestCase):
    def test_setup_local_writes_a_receipt_without_a_token(self):
        from helpers import BASE_TOML
        from motel import desk
        from motel.providers.local import LocalProvider
        fx = Fixture(BASE_TOML.replace('provider = "hetzner"', 'provider = "local"'), receipt=False)
        self.addCleanup(fx.close)
        from motel.rooms import Caller
        out = io.StringIO()
        code = desk.run(["setup", "local", "--json"], paths=fx.paths, caller=Caller(0, 0, "root"), sysops=fx.sysops, clock=fx.clock,
                        out=out, err=io.StringIO(),
                        local_factory=lambda: LocalProvider(str(fx.root / "local"), runner=lambda *a, **k: ""))
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(json.loads(Path(fx.paths.receipt_file).read_text())["provider"], "local")
        self.assertFalse(os.path.exists(fx.paths.credentials_dir))


if __name__ == "__main__":
    unittest.main()


class TokenFileTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write(self, name="token", mode=0o600, text=TOKEN + "\n"):
        path = self.dir / name
        path.write_text(text)
        path.chmod(mode)
        return str(path)

    def read(self, path, uid=None):
        from motel.setup import read_token_file
        return read_token_file(path, owner_uid=os.getuid() if uid is None else uid)

    def test_a_private_regular_file_is_accepted(self):
        self.assertEqual(self.read(self.write()), TOKEN)
        self.assertEqual(self.read(self.write("ro", 0o400)), TOKEN)

    def test_loose_modes_are_refused_without_echoing_the_token(self):
        for mode in (0o640, 0o604, 0o644, 0o700, 0o660):
            with self.subTest(mode=oct(mode)), self.assertRaises(Usage) as raised:
                self.read(self.write(mode=mode))
            self.assertNotIn(TOKEN, raised.exception.line())

    def test_symlinks_directories_and_missing_files_are_refused(self):
        real = self.write()
        os.symlink(real, self.dir / "link")
        for path in (str(self.dir / "link"), str(self.dir), str(self.dir / "missing")):
            with self.subTest(path), self.assertRaises(Usage):
                self.read(path)

    def test_wrong_owner_is_refused(self):
        with self.assertRaises(Usage):
            self.read(self.write(), uid=os.getuid() + 1)  # as if the file belonged to a non-root user

    def test_garbage_content_is_refused(self):
        for text in ("", "short", TOKEN + " extra\n", "\x00\x01" * 20):
            with self.subTest(text[:6]), self.assertRaises(Usage):
                self.read(self.write(text=text))

    def test_setup_with_token_file_stores_the_token_and_prints_nothing_secret(self):
        from motel import desk
        from motel.rooms import Caller
        fx = Fixture(receipt=False)
        self.addCleanup(fx.close)
        cloud = FakeProvider(server_id=SERVER)
        out = io.StringIO()
        code = desk.run(["setup", "hetzner", "--token-file", self.write(), "--json"], paths=fx.paths, caller=Caller(0, 0, "root"),
                        sysops=fx.sysops, clock=fx.clock, out=out, err=io.StringIO(), hetzner_factory=lambda token: cloud,
                        token_reader=lambda: self.fail("the terminal prompt must not be used"), token_file_uid=os.getuid())
        self.assertEqual(code, 0, out.getvalue())
        self.assertNotIn(TOKEN, out.getvalue())
        self.assertEqual(Path(fx.paths.token_file("hetzner")).read_text().strip(), TOKEN)
        self.assertEqual(stat.S_IMODE(Path(fx.paths.token_file("hetzner")).stat().st_mode), 0o600)

    def test_token_file_is_for_hetzner_only_and_for_root_only(self):
        from motel import desk
        from motel.rooms import Caller
        fx = Fixture(receipt=False)
        self.addCleanup(fx.close)
        for caller, argv, want in ((Caller(0, 0, "root"), ["setup", "local", "--token-file", self.write(), "--json"], 2),
                                   (fx.caller, ["setup", "hetzner", "--token-file", self.write(), "--json"], 3)):
            code = desk.run(argv, paths=fx.paths, caller=caller, sysops=fx.sysops, clock=fx.clock, out=io.StringIO(), err=io.StringIO())
            self.assertEqual(code, want)
