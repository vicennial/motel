import re
import tomllib
import unittest
from pathlib import Path

import motel

ROOT = Path(__file__).resolve().parent.parent


class VersionTest(unittest.TestCase):
    def test_pyproject_and_package_versions_match(self):
        meta = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertEqual(meta["project"]["version"], motel.__version__)
        self.assertEqual(motel.__version__, "0.1.0")

    def test_server_json_version_matches(self):
        import json
        doc = json.loads((ROOT / "server.json").read_text())
        versions = {doc["version"], *(p.get("version") for p in doc.get("packages", []))}
        self.assertEqual(versions, {motel.__version__})

    def test_runtime_has_no_dependencies(self):
        meta = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertEqual(meta["project"]["dependencies"], [])

    def test_changelog_has_an_unreleased_section(self):
        self.assertRegex((ROOT / "CHANGELOG.md").read_text(), r"(?m)^## \[Unreleased\]")


class ReadmeTest(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / "README.md").read_text()

    def test_first_screen_is_short(self):
        first = self.text.split("\n## ")[0]
        self.assertLessEqual(len(re.findall(r"\w+", first)), 130)
        self.assertIn("**Disk and servers by the hour, for agents.**", first)

    def test_question_shaped_headings(self):
        for heading in ("How do I give an agent 200 GB for an afternoon?", "What happens if nobody checks out?",
                        "Can my agent see my cloud token?", "What will it never touch?", "How much does it cost?",
                        "Can I try it without a cloud account?"):
            self.assertIn(f"## {heading}", self.text)

    def test_honest_limits_are_stated(self):
        lowered = self.text.lower()
        for phrase in ("not a sandbox", "hostile root", "whole project"):
            self.assertIn(phrase, lowered)


if __name__ == "__main__":
    unittest.main()
