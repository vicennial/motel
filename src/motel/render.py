"""Human-readable output, shared by the desk and the unprivileged CLI."""

from __future__ import annotations


def left(room: dict) -> str:
    if room["state"] == "lost-and-found":
        return f"purge at {room.get('purge_at')}"
    if room["state"] == "purged":
        return "deleted"   # no checkout time applies to a room that no longer exists
    hours = room["time_left_h"]
    return f"{hours:+.1f}h left" if hours >= 0 else f"{-hours:.1f}h past checkout"


def room_line(room: dict) -> str:
    return (f"{room['id']:<28} {room['state']:<15} {room['size_gb']:>5} GB  {left(room):<22} "
            f"{room.get('owner', '')}  {room.get('path') or ''}")


def human_room(room: dict) -> str:
    return room_line(room)


def human_ls(doc: dict) -> str:
    rooms = doc["rooms"]
    lines = [room_line(r) for r in rooms] or ["no rooms"]
    cap = doc.get("capacity") or {}
    if cap:
        cost = f", about EUR {cap['est_monthly_eur']}/month" if "est_monthly_eur" in cap else ""
        lines.append(f"floors {cap['floors']}/{cap['max_floors']}, {cap['total_gb']}/{cap['max_total_gb']} GB provisioned{cost} "
                     f"(cap EUR {cap['monthly_cap_eur']:g}/month)")
    return "\n".join(lines)


def human_doctor(doc: dict) -> str:
    mark = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL"}
    return "\n".join(f"{mark[c['level']]} {c['check']}{': ' + c['detail'] if c['detail'] else ''}" for c in doc["checks"])


def human_reap(doc: dict) -> str:
    lines = [f"{key}: {value}" for key in ("actions", "warnings", "refused", "orphans", "errors") for value in doc[key]]
    for held in doc.get("held") or []:   # empty shared floors kept for their release window (the provider bills started hours)
        lines.append(f"held: floor {held['floor']} is empty and kept for packing until {held['release_from']} "
                     f"(deleted by {held['release_by']}; its paid hour ends {held['paid_until']})")
    if not doc["apply"] and doc["actions"]:
        lines.append("(dry run: nothing changed; use --apply)")
    if doc.get("note"):
        lines.append(doc["note"])
    return "\n".join(lines) or "nothing to do"


def human_setup(doc: dict) -> str:
    return (f"motel is set up for {doc['provider']} server {doc['server_id']} ({doc['location']}); "
            f"{doc['never_touch']} existing volume(s) recorded as never-touch.\nReceipt: {doc['receipt']}\n"
            "Next: motel doctor")


def warnings_for(doc: dict) -> list:
    """Stderr warnings for rooms close to checkout time."""
    rooms = doc.get("rooms") or ([doc["room"]] if "room" in doc else [])
    out = []
    for room in rooms:
        if room.get("warn"):
            out.append(f"motel: room {room['id']} checks out in {room['time_left_h']:.1f}h. Try: motel extend {room['id']} --for 6h")
        elif room["state"] == "grace":
            out.append(f"motel: room {room['id']} is past checkout and in grace. Try: motel extend {room['id']} --for 6h")
    return out


def render(verb: str, doc: dict) -> str:
    if verb == "ls":
        return human_ls(doc)
    if verb == "doctor":
        return human_doctor(doc)
    if verb == "reap":
        return human_reap(doc)
    if verb == "setup":
        return human_setup(doc)
    return human_room(doc["room"])
