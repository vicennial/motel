"""Read-only probes for ``motel omnigent``: cabins, the Omnigent install, the provider, a running server.

Every probe takes its inputs from a :class:`Deps` object, so tests can fake the machine. The defaults look at the real
one. Nothing here changes anything except :func:`install_command`'s result, which the caller runs.
"""

from __future__ import annotations

import io
import json
import os
import shlex
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from motel import __version__

DOCS_URL = "https://github.com/vicennial/motel/blob/main/docs/cabins.md#first-cabin"
FIRST_CABIN_HINT = f'follow "First cabin" in docs/cabins.md ({DOCS_URL}), then check with: motel cabin doctor'
INSTALL_OMNIGENT_HINT = "uv tool install omnigent --with motel"
SERVER_NAMES = ("omnigent", "omni")
PROCESS_NAMES = (*SERVER_NAMES, "omnigent.cli")      # `python -m omnigent.cli server` is how Omnigent starts its own server


@dataclass
class Check:
    """The answer of one probe. ``code`` is the exit code to use when ``ok`` is false."""

    ok: bool
    detail: str
    hint: str | None = None
    code: int = 0
    data: dict = field(default_factory=dict)


def default_run(argv: list[str], *, input: str | None = None, timeout: float = 60) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(argv, input=input, capture_output=True, text=True, timeout=timeout, check=False,
                              stdin=None if input is not None else subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {int(timeout)} s"
    except OSError as exc:
        return 127, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


def default_cabin_doctor() -> tuple[int, dict]:
    """What ``motel cabin doctor`` asks the front desk. Read-only: no cabin is created."""
    from motel.cabins.cli import DESK, Cli
    from motel.cabins.errors import Fail

    def quiet(argv, **kw):          # keep sudo's own complaints (for example "no new privileges") off the terminal
        return subprocess.run(argv, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, **kw)

    cli = Cli(runner=quiet, desk_exists=lambda: os.path.exists(DESK), out=io.StringIO(), err=io.StringIO())
    try:
        return cli.desk_call(["doctor"])
    except Fail as exc:
        return exc.code, {"ok": False, "error": exc.message, "hint": exc.hint}


def default_cabin_images() -> tuple[int, dict]:
    """What ``motel cabin images`` asks the front desk: the baked images this host offers. Read-only."""
    from motel.cabins.cli import DESK, Cli
    from motel.cabins.errors import Fail

    def quiet(argv, **kw):
        return subprocess.run(argv, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, **kw)

    cli = Cli(runner=quiet, desk_exists=lambda: os.path.exists(DESK), out=io.StringIO(), err=io.StringIO())
    try:
        return cli.desk_call(["images"])
    except Fail as exc:
        return exc.code, {"ok": False, "error": exc.message, "hint": exc.hint}


def scan_servers(uid: int | None = None, proc_root: str = "/proc") -> list[dict]:
    """Running ``omnigent server`` processes of this user (Linux ``/proc``; an empty list elsewhere).

    Each item is ``{"pid", "config", "port"}``: the ``-c/--config`` file (absolute, or None) and the ``--port`` (or None).
    """
    uid = os.getuid() if uid is None else uid
    found = []
    try:
        names = [n for n in os.listdir(proc_root) if n.isdigit() and int(n) != os.getpid()]
    except OSError:
        return []
    for name in sorted(names, key=int):
        base = os.path.join(proc_root, name)
        try:
            if os.stat(base).st_uid != uid:
                continue
            with open(os.path.join(base, "cmdline"), "rb") as handle:
                argv = handle.read().decode("utf-8", "replace").split("\0")
        except OSError:
            continue
        try:
            cwd = os.readlink(os.path.join(base, "cwd"))
        except OSError:
            cwd = None                       # some sandboxes hide it: a relative -c path then stays unknown
        item = parse_server_argv(argv, cwd)
        if item:
            found.append({"pid": int(name), **item})
    return found


def parse_server_argv(argv: list[str], cwd: str | None = "/") -> dict | None:
    """``{"config", "port"}`` when ``argv`` is an ``omnigent server`` (or ``omni server``) command, else None."""
    argv = [a for a in argv if a]
    start = next((i for i, a in enumerate(argv) if os.path.basename(a) in PROCESS_NAMES), None)
    if start is None:
        return None
    rest = argv[start + 1:]
    if not rest or "server" not in rest:
        return None
    rest = rest[rest.index("server") + 1:]
    config = port = None
    it = iter(rest)
    for arg in it:
        if arg in ("-c", "--config"):
            config = next(it, None)
        elif arg.startswith("--config="):
            config = arg.split("=", 1)[1]
        elif arg == "--port":
            port = next(it, None)
        elif arg.startswith("--port="):
            port = arg.split("=", 1)[1]
    if config:
        config = os.path.expanduser(config)
        config = os.path.normpath(os.path.join(cwd or "/", config)) if cwd or os.path.isabs(config) else None
    return {"config": config, "port": int(port) if port and port.isdigit() else None}


@dataclass
class Deps:
    """Everything ``motel omnigent`` reads from the machine. Replace a field in a test to fake that part."""

    which: Callable[[str], str | None] = shutil.which
    run: Callable[..., tuple[int, str, str]] = default_run
    environ: Mapping[str, str] = field(default_factory=lambda: os.environ)
    home: str = field(default_factory=lambda: os.path.expanduser("~"))
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    cabin_doctor: Callable[[], tuple[int, dict]] = default_cabin_doctor
    cabin_images: Callable[[], tuple[int, dict]] = default_cabin_images
    servers: Callable[[], list[dict]] = scan_servers
    motel_spec: Callable[[], str] = lambda: motel_requirement()


# -- cabins ---------------------------------------------------------------------------------------------------------
def check_cabins(deps: Deps) -> Check:
    """Is the front desk installed and does ``cabin doctor`` pass for this user? Creates nothing."""
    try:
        rc, doc = deps.cabin_doctor()
    except Exception as exc:  # a broken install must read as "not ready", never as a traceback
        return Check(False, f"cabins could not be checked ({type(exc).__name__}: {exc})", FIRST_CABIN_HINT, 8)
    doc = doc if isinstance(doc, dict) else {}
    checks = doc.get("checks")
    if isinstance(checks, list):
        failed = [c for c in checks if isinstance(c, dict) and not c.get("ok") and c.get("check") != "no cleanup-pending cabins"]
        if not failed:
            pending = [c for c in checks if isinstance(c, dict) and not c.get("ok")]
            note = "; a cleanup-pending cabin still bills (motel cabin ls)" if pending else ""
            return Check(True, f"front desk installed, cabin doctor passed ({len(checks)} checks){note}")
        names = "; ".join(str(c.get("check")) for c in failed[:3])
        setup = any("prerequisite" in str(c.get("check")) for c in failed)
        return Check(False, f"cabin doctor failed: {names}", FIRST_CABIN_HINT if setup else "motel cabin doctor", 8)
    error = str(doc.get("error") or f"cabin doctor gave no answer (exit {rc})")
    not_set_up = rc == 8 or "not installed" in error
    return Check(False, error, FIRST_CABIN_HINT if not_set_up else str(doc.get("hint") or "motel cabin doctor"), 8 if not_set_up else 5)


def check_image(deps: Deps, name: str) -> Check:
    """Does a baked image called ``name`` exist on this host (``motel cabin images``)? Creates nothing."""
    try:
        rc, doc = deps.cabin_images()
    except Exception as exc:
        return Check(False, f"the image list could not be read ({type(exc).__name__}: {exc})", "motel cabin images", 5)
    doc = doc if isinstance(doc, dict) else {}
    rows = doc.get("images")
    if not isinstance(rows, list):
        error = str(doc.get("error") or f"the front desk gave no answer (exit {rc})")
        return Check(False, f"cannot list the baked images: {error}", str(doc.get("hint") or "motel cabin images"), 5)
    named = [r for r in rows if isinstance(r, dict) and r.get("name") == name]
    if not named:
        return Check(False, f"no baked image named {name} on this host (it offers: "
                            f"{', '.join(sorted(str(r.get('name')) for r in rows if isinstance(r, dict))) or 'none'})",
                     "motel omnigent bake-script > setup.sh && sudo motel-desk tour bake " + name + " --script setup.sh", 8)
    if len(named) > 1:
        ids = ", ".join(str(r.get("id")) for r in named)
        return Check(False, f"more than one baked image is named {name} (ids {ids}), so the desk refuses the name",
                     "sudo motel-desk tour image rm ID", 8)
    return Check(True, f"baked image {name} exists (id {named[0].get('id')}, created {named[0].get('created')})")


# -- Omnigent -------------------------------------------------------------------------------------------------------
@dataclass
class OmnigentEnv:
    executable: str
    python: str
    root: str | None
    kind: str            # "uv tool", "virtualenv" or "system"
    version: str | None = None


def find_omnigent(deps: Deps) -> OmnigentEnv | None:
    """Locate the ``omnigent`` executable and the Python environment behind it (a ``uv tool`` environment is the main case)."""
    exe = next((p for p in (deps.which(n) for n in SERVER_NAMES) if p), None)
    if exe is None:
        return None
    real = os.path.realpath(exe)
    python = None
    try:
        with open(real, "rb") as handle:
            first = handle.read(300).split(b"\n", 1)[0].decode("utf-8", "replace")
        if first.startswith("#!"):
            parts = first[2:].split()
            if parts and os.path.basename(parts[0]).startswith("python") and os.access(parts[0], os.X_OK):
                python = parts[0]
    except OSError:
        pass
    if python is None:
        python = next((c for c in (os.path.join(os.path.dirname(real), n) for n in ("python", "python3")) if os.access(c, os.X_OK)), None)
    if python is None:
        return None
    root = os.path.dirname(os.path.dirname(python))
    has_cfg = os.path.exists(os.path.join(root, "pyvenv.cfg"))
    kind = "uv tool" if has_cfg and os.path.exists(os.path.join(root, "uv-receipt.toml")) else "virtualenv" if has_cfg else "system"
    env = OmnigentEnv(exe, python, root if has_cfg else None, kind)
    env.version = omnigent_version(deps, env)
    return env


def omnigent_version(deps: Deps, env: OmnigentEnv) -> str | None:
    code = "import importlib.metadata as m; print(m.version('omnigent'))"
    rc, out, _ = deps.run([env.python, "-I", "-c", code], timeout=30)
    return out.strip().splitlines()[-1] if rc == 0 and out.strip() else None


# Runs inside Omnigent's own Python (never in this process: motel's core does not import Omnigent).
PROVIDER_PROBE = r"""
import importlib, json
from importlib.metadata import PackageNotFoundError, entry_points, version
out = {"entry": False, "loads": False, "registry": None, "motel_version": None, "error": None}
try:
    out["motel_version"] = version("motel")
except PackageNotFoundError:
    pass
eps = [e for e in entry_points(group="omnigent.sandbox_providers") if e.name == "motel"]
out["entry"] = bool(eps)
if eps:
    try:
        eps[0].load()
        out["loads"] = True
    except Exception as exc:
        out["error"] = (type(exc).__name__ + ": " + str(exc))[:300]
    try:
        registry = importlib.import_module("omnigent.onboarding.sandboxes.registry")
        out["registry"] = "motel" in registry.available_providers()
    except Exception:
        pass
print(json.dumps(out))
"""


def check_provider(deps: Deps, env: OmnigentEnv) -> Check:
    """Does the ``omnigent.sandbox_providers`` entry point named ``motel`` resolve in Omnigent's environment?"""
    rc, out, err = deps.run([env.python, "-I", "-c", PROVIDER_PROBE], timeout=60)
    try:
        info = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return Check(False, f"could not ask {env.python}: {(err or out).strip()[:200] or f'exit {rc}'}", None, 5)
    version = info.get("motel_version")
    if info.get("entry") and info.get("loads") and info.get("registry") is not False:
        return Check(True, f"entry point motel resolves (motel {version} in the Omnigent environment)", data=info)
    if info.get("entry") and info.get("error"):
        return Check(False, f"the motel entry point is installed but does not load: {info['error']}", None, 5, info)
    if info.get("entry") and info.get("registry") is False:
        return Check(False, "the motel entry point loads, but Omnigent does not list motel as a provider", None, 5, info)
    return Check(False, "the omnigent.sandbox_providers entry point motel is not in the Omnigent environment", None, 8, info)


def motel_requirement() -> str:
    """What to install into Omnigent's environment so that it matches the motel that is running now.

    A checkout or wheel that motel was installed from (``direct_url.json``), else the checkout this file sits in, else the
    released version.
    """
    try:
        from importlib.metadata import PackageNotFoundError, distribution
        try:
            raw = distribution("motel").read_text("direct_url.json")
        except PackageNotFoundError:
            raw = None
        if raw:
            info = json.loads(raw)
            url = str(info.get("url", ""))
            if url.startswith("file://") and os.path.exists(url[7:]):
                return url[7:]
            if "vcs_info" in info:
                vcs = info["vcs_info"]
                return f"motel @ {vcs.get('vcs', 'git')}+{url}@{vcs.get('commit_id', '')}".rstrip("@")
            if url.startswith("https://"):
                return f"motel @ {url}"
    except (OSError, ValueError):
        pass
    root = Path(__file__).resolve().parents[3]
    if (root / "pyproject.toml").is_file() and (root / "src" / "motel" / "__init__.py").is_file():
        return str(root)
    return f"motel=={__version__}"


def install_command(deps: Deps, env: OmnigentEnv) -> list[str] | None:
    """The command that adds motel to Omnigent's environment without touching Omnigent, or None for a system Python.

    motel has no dependencies, so ``uv pip install`` into the tool environment cannot move Omnigent's version. It does not
    edit uv's tool receipt: ``uv tool upgrade omnigent`` rebuilds the environment and drops motel, and ``setup`` adds it again.
    """
    if env.kind == "system":
        return None
    spec = deps.motel_spec()
    uv = deps.which("uv")
    if uv:
        return [uv, "pip", "install", "--python", env.python, spec]
    return [env.python, "-m", "pip", "install", spec]


def quote(argv: list[str]) -> str:
    return " ".join(shlex.quote(a) for a in argv)
