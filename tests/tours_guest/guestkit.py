"""Helpers: run the guest runner end to end against a temporary git repository."""
import json
import os
import shutil
import stat
import subprocess
import sys
import textwrap

PKG = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src", "motel", "tours",
                   "guest")
PLACEHOLDER = "mtl_placeholder_0123456789abcdefABCDEF"
OAUTH = "sk-ant-oat01-" + "Z9" * 24
GIT_ENV = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/nonexistent", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}


def git(cwd, *args, check=True):
    proc = subprocess.run(["git", "-c", "commit.gpgsign=false", *args], cwd=cwd, env=GIT_ENV, capture_output=True,
                          text=True, check=False)
    if check and proc.returncode != 0:
        raise AssertionError(proc.stderr)
    return proc.stdout.strip()


class Guest:
    """One guest sandbox under ``tmp``: a repo, the input directory, a copy of the runner, a fake home."""

    def __init__(self, tmp):
        self.tmp = str(tmp)
        self.repo = os.path.join(self.tmp, "repo")
        self.indir = os.path.join(self.tmp, "in")
        self.home = os.path.join(self.tmp, "guesthome")
        self.out = os.path.join(self.home, "out")
        self.work = os.path.join(self.home, "work")
        self.bin = os.path.join(self.tmp, "bin")
        self.lib = os.path.join(self.tmp, "lib")
        for d in (self.repo, self.indir, self.bin, self.lib):
            os.makedirs(d)
        # The runner is shipped as two files next to each other; run exactly that layout.
        for name in ("runner.py", "adapters.py"):
            shutil.copy(os.path.join(PKG, name), self.lib)
        git(self.repo, "init", "-q", "-b", "main")
        self.write_repo("README.md", "hello\n")
        self.write_repo("src/app.txt", "one\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        self.base = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "bundle", "create", os.path.join(self.indir, "input.bundle"), "HEAD")

    def write_repo(self, rel, text):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write(text)

    def fake_cli(self, name, source):
        """Install an executable python script called ``name`` into the agent's PATH directory."""
        path = os.path.join(self.bin, name)
        with open(path, "w") as handle:
            handle.write(f"#!{sys.executable}\n" + textwrap.dedent(source))
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)

    def card(self, **over):
        card = {"schema": "motel/guest-card/v1", "tour": "t-1", "guest": "fix-auth", "attempt": "a-1",
                "base": self.base, "branch": "motel/t-1/fix-auth", "adapter": "command",
                "argv": [sys.executable, "-c", "pass"], "env": {}, "bindings": [], "concierge_port": 17781,
                "placeholder": PLACEHOLDER, "ttl_s": 30, "max_turns": 5, "artifacts": [], "max_result_mb": 8}
        card.update(over)
        return card

    def script(self, code, **over):
        """A card whose agent is a python snippet."""
        return self.card(argv=[sys.executable, "-c", textwrap.dedent(code)], **over)

    def run(self, card, *, stdin=b"", args=(), path=None, overlay=None, env=None, timeout=120):
        with open(os.path.join(self.indir, "card.json"), "w") as handle:
            json.dump(card, handle)
        if overlay is not None:
            with open(os.path.join(self.indir, "overlay.patch"), "w") as handle:
                handle.write(overlay)
        argv = [sys.executable, os.path.join(self.lib, "runner.py"), "--in", self.indir, "--home", self.home,
                "--path", path or (self.bin + ":/usr/bin:/bin"), *args]
        environment = dict(os.environ, SECRET_CANARY="canary-value-must-not-leak", **(env or {}))
        proc = subprocess.run(argv, input=stdin, capture_output=True, env=environment, timeout=timeout, check=False)
        return Result(self, proc)


class Result:
    def __init__(self, guest, proc):
        self.guest, self.proc = guest, proc
        assert proc.returncode == 0, proc.stderr.decode()
        with open(os.path.join(guest.out, "run.json")) as handle:
            self.text = handle.read()
        self.run = json.loads(self.text)

    def out_path(self, *parts):
        return os.path.join(self.guest.out, *parts)

    def read(self, *parts):
        with open(self.out_path(*parts), "rb") as handle:
            return handle.read()

    def exists(self, *parts):
        return os.path.lexists(self.out_path(*parts))

    def all_bytes(self):
        """Everything the runner left for the controller, plus the guest home and work tree, concatenated."""
        blobs = []
        for base in (self.guest.out, self.guest.home):
            for root, _dirs, files in os.walk(base):
                for name in files:
                    path = os.path.join(root, name)
                    if os.path.islink(path):
                        continue
                    with open(path, "rb") as handle:
                        blobs.append(handle.read())
        return b"\n".join(blobs)

    def handed_over(self):
        """Only what the controller collects: everything under out/ (the work tree is the agent's own scratch)."""
        blobs = []
        for root, _dirs, files in os.walk(self.guest.out):
            for name in files:
                with open(os.path.join(root, name), "rb") as handle:
                    blobs.append(handle.read())
        return b"\n".join(blobs)

    def bundle_clone(self, dest):
        """Clone the original repo, fetch the result bundle's HEAD into branch ``result`` and return ``dest``."""
        subprocess.run(["git", "clone", "-q", self.guest.repo, dest], env=GIT_ENV, check=True, capture_output=True)
        proc = subprocess.run(["git", "fetch", "-q", self.out_path("result.bundle"), "HEAD:refs/heads/result"],
                              cwd=dest, env=GIT_ENV, capture_output=True, check=False)
        assert proc.returncode == 0, proc.stderr.decode()
        return dest
