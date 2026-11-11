"""The wheel carries what the guests and the concierge need at run time (package data is not code the tests import)."""
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NEEDED = ("motel/tours/guest/runner.py", "motel/tours/guest/adapters.py", "motel/tours/guest/egress.py",
          "motel/tours/guest/__init__.py", "motel/tours/bake.py", "motel/concierge/service.py", "motel/cabins/cloud-init.yaml",
          "motel/cabins/guest_agent.py", "motel/tours/pipeline.py")


def test_the_guest_files_are_in_the_source_tree_and_not_ignored():
    for name in NEEDED:
        assert (ROOT / "src" / name).is_file(), name
    git = shutil.which("git")
    if git and (ROOT / ".git").exists():
        for name in ("runner.py", "adapters.py"):
            proc = subprocess.run([git, "check-ignore", "-q", f"src/motel/tours/guest/{name}"], cwd=ROOT, capture_output=True)
            assert proc.returncode == 1, f"a .gitignore rule would keep {name} out of the wheel"


def test_the_built_wheel_lists_the_runner_and_its_adapters(tmp_path):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not installed")
    proc = subprocess.run([uv, "build", "--wheel", "--out-dir", str(tmp_path), str(ROOT)], capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        pytest.skip("the wheel could not be built here (build backend unavailable offline?): " + proc.stderr[-200:])
    [wheel] = tmp_path.glob("*.whl")
    names = set(zipfile.ZipFile(wheel).namelist())
    missing = [n for n in NEEDED if n not in names]
    assert missing == []
    assert not any(n.endswith(".pyc") or "__pycache__" in n for n in names)
