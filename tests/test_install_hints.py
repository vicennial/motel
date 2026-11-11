"""Every "not installed" hint names a route that works: the installer refuses a source that anybody else can change."""

import re
import unittest
from pathlib import Path

from motel import errors, installer

SRC = Path(__file__).resolve().parent.parent / "src"
BARE = re.compile(r"sudo\s+\./install\.sh")


class InstallHintTest(unittest.TestCase):
    def test_no_source_file_points_at_a_bare_sudo_install_sh(self):
        hits = [f"{path.relative_to(SRC)}:{n}" for path in sorted(SRC.rglob("*.py"))
                for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1) if BARE.search(line)]
        self.assertEqual([], hits, "use motel.errors.INSTALL_HINT (or install_hint(args)) instead")

    def test_the_shared_hint_names_the_uvx_route_and_the_root_owned_clone(self):
        self.assertIn("sudo uvx --from motel motel install", errors.INSTALL_HINT)
        self.assertIn("(or install.sh from a root-owned clone; see docs/install.md)", errors.INSTALL_HINT)

    def test_arguments_go_between_the_command_and_the_alternative(self):
        hint = errors.install_hint("--users alice")
        self.assertTrue(hint.startswith("sudo uvx --from motel motel install --users alice (or install.sh"), hint)

    def test_the_installer_reuses_the_constants(self):
        self.assertIs(installer.INSTALL_HINT, errors.INSTALL_HINT)
        self.assertIs(installer.INSTALL_CMD, errors.INSTALL_CMD)

    def test_the_banner_shows_the_same_command(self):
        from motel.onboard import banner
        self.assertIn(errors.INSTALL_CMD, [cmd for cmd, _ in banner.START_HERE])


if __name__ == "__main__":
    unittest.main()
