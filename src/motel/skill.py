"""``motel skill``: print or install the agent skill (``SKILL.md``) that ships inside the package.

    motel skill print                  # the skill text on stdout
    motel skill path                   # where the packaged skill file is
    motel skill install [--dir DIR]    # copy it to DIR/motel/SKILL.md (default ~/.claude/skills)

A wheel carries the file as package data (``motel/skill_data/SKILL.md``, copied from ``skills/motel/SKILL.md`` at
build time). A source checkout reads ``skills/motel/SKILL.md`` directly.
"""

from __future__ import annotations

import argparse
import os
import sys
from importlib import resources

from motel.errors import MotelError, NotFound, Usage

DEFAULT_DIR = "~/.claude/skills"


def skill_text() -> str:
    """The packaged skill, or the checkout's copy when run from source."""
    try:
        return (resources.files("motel") / "skill_data" / "SKILL.md").read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError):
        pass
    checkout = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "skills", "motel", "SKILL.md")
    try:
        with open(checkout, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        raise NotFound("the agent skill is not part of this installation", "npx skills add vicennial/motel") from None


def skill_path() -> str:
    packaged = resources.files("motel") / "skill_data" / "SKILL.md"
    if packaged.is_file():
        return str(packaged)
    return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "skills", "motel", "SKILL.md")


def install_skill(directory: str) -> str:
    """Write ``<directory>/motel/SKILL.md`` (replacing an older copy) and return its path."""
    target_dir = os.path.join(os.path.expanduser(directory), "motel")
    text = skill_text()
    try:
        os.makedirs(target_dir, exist_ok=True)
        target = os.path.join(target_dir, "SKILL.md")
        tmp = target + ".new"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, target)
    except OSError as exc:
        raise Usage(f"cannot write the skill to {target_dir}: {exc.strerror}", "motel skill install --dir PATH") from exc
    return target


def main(argv=None, *, out=None, err=None) -> int:
    out, err = out or sys.stdout, err or sys.stderr
    parser = argparse.ArgumentParser(prog="motel skill", description="The motel agent skill (SKILL.md).")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("print", help="print the skill text")
    sub.add_parser("path", help="print where the packaged skill file is")
    p = sub.add_parser("install", help="copy the skill into an agent's skills directory")
    p.add_argument("--dir", default=DEFAULT_DIR, help=f"skills directory (default {DEFAULT_DIR}; other agents keep theirs elsewhere)")
    try:
        args = parser.parse_args(sys.argv[1:] if argv is None else argv)
        if args.action == "print":
            out.write(skill_text())
        elif args.action == "path":
            print(skill_path(), file=out)
        else:
            print(f"installed {install_skill(args.dir)}", file=out)
    except SystemExit as exc:
        return 2 if exc.code else 0
    except MotelError as exc:
        print(exc.line(), file=err)
        return exc.code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
