"""The terminal banner, the "start here" list and the expiry line.

Colour is used only on a terminal, and never when ``NO_COLOR`` is set (https://no-color.org) or ``TERM`` is
``dumb``. Anywhere else the output is plain text with no escape codes.
"""

from __future__ import annotations

import os

PINK = "\033[38;5;198m"
TEAL = "\033[38;5;80m"
AMBER = "\033[38;5;214m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"

TAGLINE = "disk and servers by the hour, for agents"
LETTERS = (
    "#   #   ###   #####  #####  #",
    "## ##  #   #    #    #      #",
    "# # #  #   #    #    #      #",
    "#   #  #   #    #    ####   #",
    "#   #  #   #    #    #      #",
    "#   #  #   #    #    #      #",
    "#   #   ###     #    #####  #####",
)
PUMPKIN = "your room turned into a pumpkin \U0001F383"

START_HERE = (
    ("motel try", "a 30-second tour: no root, no cloud, nothing installed"),
    ("sudo uvx --from motel motel install", "set this host up (or install.sh from a root-owned clone)"),
    ("motel doctor", "check the installation"),
    ("motel checkin 50G --for 6h", "take a room: a private folder on a temporary volume"),
    ("motel cabin checkin", "a disposable server (experimental)"),
    ("motel mcp", "run the MCP server for your agent"),
)


def want_color(stream, no_color: bool = False) -> bool:
    """True only for a terminal, with colour not switched off by a flag, ``NO_COLOR`` or ``TERM=dumb``."""
    if no_color or os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return False
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


def paint(text: str, *codes: str, color: bool) -> str:
    return f"{''.join(codes)}{text}{RESET}" if color else text


def banner(color: bool = False) -> str:
    lines = [paint(row.rstrip(), PINK, BOLD, color=color) for row in LETTERS]
    lines.append("   " + paint(TAGLINE, TEAL, color=color))
    return "\n".join(lines)


def start_here(color: bool = False) -> str:
    width = max(len(cmd) for cmd, _ in START_HERE)
    lines = [paint("start here", BOLD, color=color)]
    for cmd, what in START_HERE:
        lines.append(f"  {paint(cmd.ljust(width), TEAL, color=color)}  {paint(what, DIM, color=color)}")
    return "\n".join(lines)


def expiry_line(room: str, color: bool = False) -> str:
    """The one deliberately whimsical line: shown when a room passes its checkout time."""
    tag = paint("[motel]", AMBER, color=color)
    return f"{tag} {paint(room + ':', DIM, color=color)} checkout time reached. {PUMPKIN}"


def first_run(out) -> bool:
    """Bare ``motel`` on a terminal: print the banner and the start-here list. Returns True if it printed.

    Anything that is not a terminal (a pipe, a file, a test buffer) gets nothing here, so scripts see the
    same behaviour as before.
    """
    isatty = getattr(out, "isatty", None)
    if not (isatty and isatty()):
        return False
    color = want_color(out)
    text = f"{banner(color)}\n\n{start_here(color)}\n"
    out.write(text)
    return True
