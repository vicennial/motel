"""Provider control through the ``hcloud`` CLI, via a wrapper command that is split with shlex (never a shell string)."""

from __future__ import annotations

import json
import math

from motel_live.core import CmdResult, Runner

LABEL = "motel-live"
KINDS = ("server", "volume", "ssh-key", "firewall", "primary-ip", "floating-ip", "network")
ROOMS_PREFLIGHT_KINDS = ("server", "volume")
# The cabins project must be empty of everything motel's cabin guard looks at (it refuses foreign servers, volumes and
# firewalls) and of anything else that could be mistaken for a cabin remnant. SSH keys do not matter to either.
CABINS_PREFLIGHT_KINDS = ("server", "volume", "firewall", "primary-ip", "floating-ip", "network")
CABIN_KINDS = ("server", "firewall", "primary-ip", "volume")  # in deletion order
CABIN_SELECTOR = "managed-by=motel,role=cabin"
OPTIONAL_KINDS = ("firewall", "primary-ip", "floating-ip", "network")  # a wrapper's token may not be allowed to list these


class HcloudError(RuntimeError):
    pass


def norm_ids(items) -> set:
    return {str(i["id"]) for i in items}


class Hcloud:
    def __init__(self, runner: Runner, base_cmd: list, dry: bool = False):
        self.r, self.base, self.dry = runner, list(base_cmd), dry

    def run(self, *args, check: bool = True, timeout: float = 600) -> CmdResult:
        res = self.r.run([*self.base, *args], timeout=timeout)
        if check and res.rc != 0:
            raise HcloudError(f"hcloud {' '.join(str(a) for a in args[:3])} failed (rc {res.rc}): {res.err.strip()[:300]}")
        return res

    def list(self, kind: str, selector: str | None = None) -> list:
        args = [kind, "list", "-o", "json"] + (["-l", selector] if selector else [])
        res = self.run(*args)
        if self.dry:
            return []
        text = res.out.strip()
        try:
            doc = json.loads(text) if text else []
        except ValueError as exc:
            raise HcloudError(f"hcloud {kind} list did not return JSON") from exc
        return doc or []

    def describe(self, kind: str, ident) -> dict:
        res = self.run(kind, "describe", str(ident), "-o", "json")
        if self.dry:
            return {"id": f"<{kind}-id>", "name": str(ident), "labels": {}, "public_net": {"ipv4": {"ip": "<server-ip>"}}}
        try:
            return json.loads(res.out)
        except ValueError as exc:
            raise HcloudError(f"hcloud {kind} describe {ident} did not return JSON") from exc

    # -------------------------------------------------------------- inventory
    def inventory(self, kinds: tuple = KINDS) -> dict:
        """Every resource kind the harness cares about (or just ``kinds``): {kind: {id: {name, labels, ...}}}."""
        inv = {}
        for kind in kinds:
            try:
                items = self.list(kind)
            except HcloudError:
                if kind not in OPTIONAL_KINDS:
                    raise
                items = []
            inv[kind] = {str(i["id"]): _brief(kind, i) for i in items}
        return inv

    @staticmethod
    def foreign(inv: dict, run_id: str, kinds: tuple = ROOMS_PREFLIGHT_KINDS) -> list:
        """Resources of ``kinds`` that do not carry this run's label: the preflight refuses if any exist."""
        out = []
        for kind in kinds:
            for ident, item in inv.get(kind, {}).items():
                if item["labels"].get(LABEL) != run_id:
                    out.append(f"{kind} {ident} ({item['name']})")
        return out

    @staticmethod
    def diff(initial: dict, final: dict) -> dict:
        """Resources present now that were not at the start (``added``) and the reverse (``removed``)."""
        added, removed = [], []
        for kind in KINDS:
            now, then = set(final.get(kind, {})), set(initial.get(kind, {}))
            added += [f"{kind} {i} ({final[kind][i]['name']})" for i in sorted(now - then)]
            removed += [f"{kind} {i} ({initial[kind][i]['name']})" for i in sorted(then - now)]
        return {"added": added, "removed": removed}

    # -------------------------------------------------------------- creation
    def create_ssh_key(self, name: str, pubkey_path: str, run_id: str) -> None:
        self.run("ssh-key", "create", "--name", name, "--public-key-from-file", pubkey_path, "--label", f"{LABEL}={run_id}")

    def create_server(self, name: str, server_type: str, image: str, location: str, key_name: str, run_id: str) -> None:
        self.run("server", "create", "--name", name, "--type", server_type, "--image", image, "--location", location,
                 "--ssh-key", key_name, "--label", f"{LABEL}={run_id}", "--label", "role=testserver", timeout=900)

    def create_bystander(self, name: str, size_gb: int, server_name: str, run_id: str) -> None:
        self.run("volume", "create", "--name", name, "--size", str(size_gb), "--server", server_name,
                 "--label", f"{LABEL}={run_id}", "--label", "role=bystander", timeout=900)

    # -------------------------------------------------------------- price
    def hourly_price(self, server_type: str, location: str) -> float | None:
        try:
            doc = self.describe("server-type", server_type)
        except HcloudError:
            return None
        best = None
        for price in doc.get("prices", []) or []:
            try:
                value = float(price["price_hourly"]["gross"])
            except (KeyError, TypeError, ValueError):
                continue
            if price.get("location") == location:
                return value
            best = best if best is not None else value
        return best

    # -------------------------------------------------------------- teardown
    def owned(self, run_id: str, known: dict | None = None) -> dict:
        """Everything provably belonging to this run, found by label or by recorded id.

        ``known`` is the run's state file (ids recorded at creation). Volumes created by motel itself carry
        ``managed-by=motel`` and ``host=<server id>``: they count only when that id is one of this run's servers.
        """
        known = known or {}
        sel = f"{LABEL}={run_id}"
        servers = self.list("server", sel)
        server_ids = norm_ids(servers) | set(known.get("server_ids", []))
        volumes = {str(v["id"]): v for v in self.list("volume", sel)}
        mine, unattributed = [], []
        for vol in self.list("volume", "managed-by=motel"):
            labels = vol.get("labels") or {}
            attached = str(vol.get("server") or "") if not isinstance(vol.get("server"), dict) else str(vol["server"].get("id"))
            if str(vol["id"]) in volumes:
                continue
            if labels.get("host") in server_ids or attached in server_ids:
                volumes[str(vol["id"])] = vol
                mine.append(str(vol["id"]))
            else:
                unattributed.append(str(vol["id"]))
        return {"servers": servers, "volumes": list(volumes.values()), "motel_volumes": mine, "unattributed": unattributed,
                "ssh_keys": self.list("ssh-key", sel), "server_ids": sorted(server_ids)}

    def destroy_run(self, run_id: str, known: dict | None = None) -> dict:
        """Delete this run's servers, volumes and ssh keys. Returns {deleted: [...], errors: [...], unattributed: [...]}."""
        found = self.owned(run_id, known)
        deleted, errors = [], []

        def attempt(label: str, *args) -> None:
            try:
                self.run(*args, timeout=600)
                deleted.append(label)
            except HcloudError as exc:
                errors.append(f"{label}: {exc}")

        for server in found["servers"]:
            attempt(f"server {server['id']}", "server", "delete", str(server["id"]))
        for vol in found["volumes"]:
            if (vol.get("protection") or {}).get("delete"):
                attempt(f"volume {vol['id']} protection off", "volume", "disable-protection", str(vol["id"]), "delete")
            attempt(f"volume {vol['id']}", "volume", "delete", str(vol["id"]))
        for key in found["ssh_keys"]:
            attempt(f"ssh-key {key['id']}", "ssh-key", "delete", str(key["id"]))
        return {"deleted": deleted, "errors": errors, "unattributed": found["unattributed"]}

    def owned_cabins(self, run_id: str, server_ids) -> tuple:
        """Cabin-project resources that provably belong to this run: ``{kind: {id: item}}`` and the unattributed rest.

        Provable means labelled ``motel-live=<run>``, or created by motel as a cabin for one of this run's controller
        servers (``managed-by=motel,role=cabin,controller=<server id>``). A cabin that names another controller is left alone.
        """
        found: dict = {kind: {} for kind in CABIN_KINDS}
        unattributed: list = []
        selectors = [f"{LABEL}={run_id}"] + [f"{CABIN_SELECTOR},controller={sid}" for sid in sorted({str(i) for i in server_ids})]
        for kind in CABIN_KINDS:
            try:
                for sel in selectors:
                    for item in self.list(kind, sel):
                        found[kind][str(item["id"])] = item
                for item in self.list(kind, CABIN_SELECTOR):
                    if str(item["id"]) not in found[kind]:
                        unattributed.append(f"{kind} {item['id']}")
            except HcloudError:
                if kind not in OPTIONAL_KINDS:
                    raise
        return found, unattributed

    def destroy_cabins(self, run_id: str, server_ids) -> dict:
        """Delete this run's cabin servers, firewalls, addresses and volumes in the cabins project.

        Returns {deleted: [...], errors: [...], unattributed: [...]}. Servers go first so firewalls and volumes are free.
        """
        found, unattributed = self.owned_cabins(run_id, server_ids)
        deleted, errors = [], []
        for kind in CABIN_KINDS:
            for item in found[kind].values():
                try:
                    if kind == "volume" and (item.get("protection") or {}).get("delete"):
                        self.run("volume", "disable-protection", str(item["id"]), "delete", timeout=600)
                    self.run(kind, "delete", str(item["id"]), timeout=600)
                    deleted.append(f"{kind} {item['id']}")
                except HcloudError as exc:
                    errors.append(f"{kind} {item['id']}: {exc}")
        return {"deleted": deleted, "errors": errors, "unattributed": unattributed}

    def delete_leaks(self, initial: dict, run_id: str) -> list:
        """Delete resources created during the run that are not in the initial inventory and not labelled for another run.

        Only meaningful in the dedicated, empty-at-start test project. Order matters: servers, then firewalls,
        addresses and volumes.
        """
        current = self.inventory()
        removed = []
        for kind, cmd in (("server", "server"), ("firewall", "firewall"), ("primary-ip", "primary-ip"),
                          ("floating-ip", "floating-ip"), ("volume", "volume")):
            for ident, item in current.get(kind, {}).items():
                if ident in initial.get(kind, {}):
                    continue
                other = item["labels"].get(LABEL)
                if other not in (None, run_id):
                    continue
                try:
                    self.run(cmd, "delete", ident)
                    removed.append(f"{kind} {ident} ({item['name']})")
                except HcloudError as exc:
                    removed.append(f"{kind} {ident} FAILED: {exc}")
        return removed


def _brief(kind: str, item: dict) -> dict:
    return {"name": item.get("name", ""), "labels": item.get("labels") or {}, "size": item.get("size"),
            "server": _server_ref(item.get("server")), "protection": item.get("protection") or {},
            "location": ((item.get("location") or {}).get("name") if isinstance(item.get("location"), dict) else item.get("location")),
            "rules": item.get("rules"), "type": item.get("type"),
            "assignee": _assignee(item), "created": item.get("created"),
            "server_type": (item.get("server_type") or {}).get("name") if isinstance(item.get("server_type"), dict) else None}


def _assignee(item: dict):
    """The server a primary IP is assigned to (id as text), else None. Other kinds have no assignee."""
    if item.get("assignee_type") not in (None, "server") or item.get("assignee_id") in (None, ""):
        return None
    return str(item["assignee_id"])


def _server_ref(value):
    if isinstance(value, dict):
        value = value.get("id")
    return None if value in (None, "") else str(value)


def snapshot_volume(item: dict) -> dict:
    """The identity of a volume that must not change: id, name, size, labels, attachment, protection."""
    return {"id": str(item.get("id")), "name": item.get("name"), "size": item.get("size"), "labels": item.get("labels") or {},
            "server": _server_ref(item.get("server")), "protection": item.get("protection") or {}}


def server_ip(doc: dict) -> str:
    return str(((doc.get("public_net") or {}).get("ipv4") or {}).get("ip", ""))


def server_ip_ids(doc: dict) -> list:
    """IDs of the primary IPs Hetzner created with a server (IPv4 and IPv6). They are unlabelled and Hetzner-named,
    and are deleted with the server, so they belong to whoever owns the server."""
    net = doc.get("public_net") or {}
    ids = [str((net.get(fam) or {}).get("id")) for fam in ("ipv4", "ipv6") if (net.get(fam) or {}).get("id")]
    return [i for i in ids if i not in ("0", "None")]


def billed_hours(seconds: float) -> int:
    return max(1, math.ceil(seconds / 3600))
