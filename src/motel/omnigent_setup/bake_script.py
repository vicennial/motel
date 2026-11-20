"""The setup script for a baked Omnigent cabin image: ``motel omnigent bake-script``.

The operator runs it through the tours image bake::

    motel omnigent bake-script > setup.sh && sudo motel-desk tour bake omnigent --script setup.sh

It installs what the Omnigent provider's ``bootstrap: pip`` installs on a bare cabin (see
``omnigent/community/sandbox/motel/transport.py``): the same apt packages, a virtual environment with ``omnigent==VERSION``
and, unless told not to, Node plus the harness CLIs from npm. Two things differ on purpose, because an image is shared by
every guest user and is made by root:

* everything is system wide: the venv is ``/opt/omnigent-motel/venv`` and ``omnigent`` is linked into ``/usr/local/bin``,
  so it is on the PATH of the cabin's guest user, and the npm packages go to ``/usr/local``;
* nothing is left that the bake's credential scan refuses or that only makes the snapshot larger: no pip or npm cache, no
  npm configuration file, and ``HOME`` is pinned to ``/root`` so nothing lands in a guest home.

The text is a pure function of its arguments: no date, no host name, no random value. Standard library only.
"""

from __future__ import annotations

import re
import shlex

PACKAGES = ("git", "tmux", "curl", "ca-certificates", "python3-venv", "python3-pip", "python3-yaml")
"""apt packages: the same list as the provider's ``base_setup`` (a test compares them)."""
NODE_PACKAGES = ("nodejs", "npm")
DEFAULT_HARNESS_PACKAGES = ("@anthropic-ai/claude-code", "@openai/codex")
"""npm packages: the provider's ``DEFAULT_HARNESS_PACKAGES`` (a test compares them)."""
VENV = "/opt/omnigent-motel/venv"
LINK = "/usr/local/bin/omnigent"

# The same rules the provider's config uses for these two values.
VERSION_RE = re.compile(r"^[0-9]+(\.[0-9]+)*([a-z]+[0-9]*|\.?(dev|post|rc)[0-9]+)*$")
NPM_SPEC_RE = re.compile(r"^(@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*(@[A-Za-z0-9][A-Za-z0-9._-]*)?$")


def release_version(version: str) -> bool:
    """A released version number (no dev or local build), safe to put in ``pip install omnigent==VERSION``."""
    return bool(VERSION_RE.fullmatch(version)) and "dev" not in version


def render(version: str, harness_packages=DEFAULT_HARNESS_PACKAGES) -> str:
    """The bake script for ``omnigent==version`` and the given npm harness packages (none: no Node, no CLIs).

    Raises ``ValueError`` for a version or package spec that is not plain."""
    if not isinstance(version, str) or not release_version(version):
        raise ValueError(f"{version!r} is not a released omnigent version such as 0.21.0")
    harness = list(harness_packages)
    for spec in harness:
        if not NPM_SPEC_RE.fullmatch(spec):
            raise ValueError(f"{spec!r} is not a plain npm package spec")
    q = shlex.quote
    apt = " ".join((*PACKAGES, *(NODE_PACKAGES if harness else ())))
    lines = [
        "#!/bin/bash",
        "# Setup script for a baked Omnigent cabin image. Made by `motel omnigent bake-script`; run it with:",
        "#   sudo motel-desk tour bake omnigent --script setup.sh",
        f"# Omnigent {version}, harness packages: {' '.join(harness) if harness else 'none'}.",
        "# To move to another version, remove the old image (`sudo motel-desk tour image rm ID`) and bake again.",
        "set -euo pipefail",
        "export DEBIAN_FRONTEND=noninteractive",
        "# The bake runs this as root through sudo. Keep every cache and config file in root's home, never in a guest's.",
        "export HOME=/root",
        "export npm_config_update_notifier=false",
        "",
        f"OMNIGENT_VERSION={q(version)}",
        f"VENV={q(VENV)}",
        f"LINK={q(LINK)}",
        "",
        "if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'; then",
        "  echo 'motel: omnigent needs Python 3.12 or newer on the cabin; use the ubuntu-24.04 image' >&2",
        "  exit 1",
        "fi",
        "",
        "apt-get update -q",
        f"apt-get install -y -q {apt}",
        "",
        'python3 -m venv "$VENV"',
        '"$VENV/bin/pip" install --quiet --disable-pip-version-check --no-cache-dir "omnigent==$OMNIGENT_VERSION"',
        'ln -sf "$VENV/bin/omnigent" "$LINK"',
    ]
    if harness:
        lines += ["", f"npm install -g --prefix /usr/local --no-audit --no-fund --loglevel=error {' '.join(q(p) for p in harness)}"]
    lines += [
        "",
        "# Check what was installed: the pinned version, importable, on the PATH of every user.",
        'installed=$("$VENV/bin/python3" -c "import importlib.metadata as m; print(m.version(\'omnigent\'))")',
        'if [ "$installed" != "$OMNIGENT_VERSION" ]; then',
        '  echo "motel: expected omnigent $OMNIGENT_VERSION, found $installed" >&2',
        "  exit 1",
        "fi",
        '[ -x "$LINK" ] || { echo "motel: $LINK is missing" >&2; exit 1; }',
        'echo "omnigent $installed is at $LINK"',
        "",
        "# Leave nothing the bake's credential scan refuses, and no cache that only makes the snapshot larger.",
        "if command -v npm >/dev/null 2>&1; then npm cache clean --force >/dev/null 2>&1 || true; fi",
        "rm -rf /root/.npm /root/.cache /root/.npmrc",
        "apt-get clean",
        "",
    ]
    return "\n".join(lines)
