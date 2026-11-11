"""`motel skill`: the packaged agent skill is readable and installable."""

import io
import tempfile
import unittest
from pathlib import Path

from motel import cli, skill

ROOT = Path(__file__).resolve().parent.parent
SOURCE = (ROOT / "skills" / "motel" / "SKILL.md").read_text(encoding="utf-8")


class SkillTest(unittest.TestCase):
    def test_the_text_is_the_skill_file(self):
        self.assertEqual(skill.skill_text(), SOURCE)
        self.assertTrue(SOURCE.startswith("---"))

    def test_install_copies_it_and_replaces_an_older_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(skill.install_skill(tmp))
            self.assertEqual(target, Path(tmp) / "motel" / "SKILL.md")
            target.write_text("old")
            skill.install_skill(tmp)
            self.assertEqual(target.read_text(), SOURCE)

    def test_the_command_is_routed_from_motel(self):
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(cli.main(["skill", "install", "--dir", tmp], out=out, err=err), 0)
            self.assertTrue((Path(tmp) / "motel" / "SKILL.md").is_file())
        out = io.StringIO()
        self.assertEqual(skill.main(["print"], out=out), 0)
        self.assertEqual(out.getvalue(), SOURCE)
        out = io.StringIO()
        self.assertEqual(skill.main(["path"], out=out), 0)
        self.assertTrue(out.getvalue().strip().endswith("SKILL.md"))

    def test_a_bad_action_is_a_usage_error(self):
        self.assertEqual(skill.main(["frobnicate"], err=io.StringIO()), 2)


if __name__ == "__main__":
    unittest.main()
