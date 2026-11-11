"""The ownership guard: an allowlist that must pass before every provider write."""

from __future__ import annotations

from motel.errors import Guard
from motel.providers.base import Volume

# Floor record states in which each action is acceptable.
ALLOWED_STATES = {
    "attach": ("created", "active"),
    "mount": ("created", "active"),
    "resize": ("active",),
    "detach": ("retiring",),
    "delete": ("retiring",),
}


def check_owned(vol: Volume, floor: dict | None, *, action: str, server_id: str, never_touch) -> None:
    """Raise Guard unless ``vol`` (freshly read) is a floor this host created and may act on.

    Required: a root-owned record for the volume, labels managed-by=motel / role=floor / host / floor, a
    ``motel-`` name, not in never-touch, delete protection off, and the right attachment state.
    """
    problems = []
    if floor is None:
        problems.append("no motel record for it (not created by motel)")
    else:
        if floor.get("volume_id") != vol.id:
            problems.append("record belongs to a different volume")
        if floor.get("state") not in ALLOWED_STATES[action]:
            problems.append(f"record state is {floor.get('state')!r}, not valid for {action}")
    if str(vol.id).casefold() in {str(v).casefold() for v in never_touch}:
        problems.append("in the never-touch set")
    labels = vol.labels or {}
    if labels.get("managed-by") != "motel" or labels.get("role") != "floor":
        problems.append("not labelled managed-by=motel, role=floor")
    if labels.get("host") != str(server_id):
        problems.append(f"labelled for host {labels.get('host')!r}")
    if floor is not None and labels.get("floor") != floor.get("id"):
        problems.append("floor label does not match the record")
    if not str(vol.name).startswith("motel-"):
        problems.append("name does not start with motel-")
    if vol.delete_protected:
        problems.append("delete protection is on")
    if action == "attach":
        if vol.server_id not in (None, str(server_id)):
            problems.append(f"attached to server {vol.server_id}")
    elif action == "delete":
        if vol.server_id not in (None, str(server_id)):
            problems.append(f"attached to server {vol.server_id}")
    elif vol.server_id != str(server_id):
        problems.append(f"not attached to this server (attached to {vol.server_id})")
    if problems:
        raise Guard(f"volume {vol.id}: " + "; ".join(problems), "motel doctor")
