"""Parsing and formatting of sizes, durations, names and timestamps."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from motel.errors import Usage

TASK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
ROOM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}-[a-z0-9]{4}$")
FLOOR_RE = re.compile(r"^f-[0-9a-f]{4,8}$")
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
SIZE_RE = re.compile(r"^([0-9]{1,6})(?:([gGtT])[bB]?)?$")
DURATION_RE = re.compile(r"^([0-9]{1,12})([mhd])$")
# One bound for the CLI and the desk: the CLI sends minutes, at most 7 digits. A longer request is clamped by the
# CLI, so it still reaches the desk's own cap check (exit 3) instead of failing as bad usage.
MAX_DURATION_MINUTES = 9_999_999
ROOM_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


def parse_size_gb(text: str) -> int:
    """'200G', '200GB', '1T' (= 1024 GB) or a plain integer (GB) -> GB."""
    match = SIZE_RE.fullmatch(text or "")
    if not match:
        raise Usage(f"bad size {text!r}: use e.g. 200G, 1T or a plain number of GB", "motel checkin 200G")
    value = int(match.group(1)) * (1024 if (match.group(2) or "").lower() == "t" else 1)
    if value <= 0:
        raise Usage("size must be positive", "motel checkin 200G")
    return value


def parse_duration(text: str, *, allow_zero: bool = False, what: str = "duration", clamp: bool = False) -> timedelta:
    """'90m', '6h' or '3d' -> timedelta. Longer than MAX_DURATION_MINUTES is bad usage, or clamped with ``clamp``."""
    match = DURATION_RE.fullmatch(text or "")
    if not match:
        raise Usage(f"bad {what} {text!r}: use e.g. 90m, 6h or 3d", "motel --help")
    value, unit = int(match.group(1)), match.group(2)
    minutes = value * {"m": 1, "h": 60, "d": 1440}[unit]
    if minutes == 0 and not allow_zero:
        raise Usage(f"{what} must be positive", "motel --help")
    if minutes > MAX_DURATION_MINUTES:
        if not clamp:
            raise Usage(f"{what} {text!r} is longer than {MAX_DURATION_MINUTES} minutes", "motel --help")
        minutes = MAX_DURATION_MINUTES
    return timedelta(minutes=minutes)


def canonical_minutes(delta: timedelta) -> str:
    return f"{int(delta.total_seconds() // 60)}m"


def check_task(task: str) -> str:
    if not TASK_RE.fullmatch(task or ""):
        raise Usage(f"bad task name {task!r}: letters, digits, '_' and '-', at most 32 characters",
                    "motel checkin 200G --task video-render")
    return task


def check_room(room: str) -> str:
    if not ROOM_RE.fullmatch(room or ""):
        raise Usage(f"bad room id {room!r}: expected TASK-xxxx", "motel ls --mine")
    return room


def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def label_stamp(dt: datetime) -> str:
    return dt.strftime("%Y%m%dt%H%Mz")


def round_up10(gb: int) -> int:
    return max(10, -(-int(gb) // 10) * 10)


def hours(delta: timedelta) -> float:
    return round(delta.total_seconds() / 3600, 2)


def fmt_td(delta: timedelta) -> str:
    """timedelta -> '90m', '6h' or '3d' (exact when possible)."""
    minutes = int(delta.total_seconds() // 60)
    if minutes and minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes and minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"
