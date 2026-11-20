"""The tour verbs of the front desk, as methods. ``desk.py`` parses arguments and wires the real collaborators;
tests build a :class:`Service` with fakes.

Ownership: a tour belongs to the Unix user who started it. Every read or change checks the caller's uid against
the owner; to anyone else (except root) a tour does not exist. Results are only ever read through these verbs,
because the state directory is root-only.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from datetime import timedelta

from motel.cabins.errors import Fail
from motel.cabins.util import iso
from motel.errors import install_hint
from motel.receipts import tour_receipt
from motel.tours import artifacts as art
from motel.tours import bundle, ownlogin, recovery, spec
from motel.tours.errors import Incomplete, NotFound, Refused, Usage
from motel.tours.pipeline import AttemptRunner
from motel.tours.state import (
    RETENTION,
    SCHEMA,
    TERMINAL,
    TOUR_ID_RE,
    TourStore,
    new_tour_id,
    open_user_file,
    worst_cleanup,
)
from motel.tours.supervisor import TERMINAL_TOUR, Supervisor, stop_on_sigterm

OK_SCHEMA = "motel/v1"
MAX_INPUT_BYTES = 256 * 1024 * 1024
MAX_OVERLAY_BYTES = 8 * 1024 * 1024
MAX_STAGING_AGE = timedelta(hours=1)
MAX_RESTARTS = 3
MAX_LOG_BYTES = 256 * 1024
DEFAULT_LOG_BYTES = 64 * 1024
LOG_FILES = {"stdout": "stdout.log", "stderr": "stderr.log", "runner": "runner.log", "boot": None}
POLL_S = 1.0


class RawReply:
    """A verb that answers with raw bytes (a bundle or an artifact) instead of a JSON document."""

    def __init__(self, data: bytes):
        self.data = data


@dataclass
class Service:
    store: TourStore
    limits: object
    cabin_cfg: object
    clock: object
    launcher: object               # (tour_id) -> unit name; raises on failure
    unit_active: object            # (tour_id) -> bool
    env_factory: object            # () -> pipeline.Env (only for supervise, cancel and reap)
    concierge_factory: object      # () -> a concierge client
    sleep: object = time.sleep
    desk_path: str = "/usr/local/sbin/motel-desk"
    free_bytes: object = lambda path: shutil.disk_usage(path).free    # noqa: E731  (a hook for tests)
    groups_fn: object = None       # (caller) -> set of group names; used for [tours.own_login] allow_users
    operator_fn: object = None     # (caller) -> bool; who may see everyone's tours
    admission_wait_s: float = 60.0    # how long a start waits for the user or host lock before it says the host is busy
    inspect_root: str | None = None   # where inspection scratch directories live (reap removes stale ones)
    setup_check: object = None     # () -> list of problems with what a tour needs on this host (the inspection user); None: unchecked

    # ------------------------------------------------------------------ helpers
    def _owned(self, caller, tour_id: str) -> dict:
        if not TOUR_ID_RE.fullmatch(tour_id or ""):
            raise Usage(f"bad tour id {tour_id!r}", "motel tour ls")
        doc = self.store.load(tour_id)
        if caller.uid != doc["owner"]["uid"] and not self._is_operator(caller):
            raise NotFound(f"no tour {tour_id}")
        return doc

    @staticmethod
    def _cleanup_of(doc: dict) -> str:
        return worst_cleanup(g["cleanup"] for g in doc["guests"].values())

    def view(self, doc: dict, *, detail: bool = False) -> dict:
        guests = []
        models = {sg["id"]: sg["model"] for sg in doc.get("spec", {}).get("guests", []) if sg.get("model") is not None}
        for gid, g in doc["guests"].items():
            row = {"id": gid, "status": g["status"], "cleanup": g["cleanup"], "attempts": len(g["attempts"]),
                   "result": g.get("result")}
            if gid in models:
                row["model"] = models[gid]
            if g.get("reason"):
                row["reason"] = g["reason"]
            if detail:
                row["attempt_list"] = [{k: a.get(k) for k in ("id", "status", "cleanup", "phase", "lease_id", "auth", "created_at",
                                                               "intent_at", "finished_at", "error", "boot")}
                                       for a in self.store.attempts(doc["id"]) if a["guest"] == gid]
            guests.append(row)
        counts: dict = {}
        for g in guests:
            counts[g["status"]] = counts.get(g["status"], 0) + 1
        out = {"id": doc["id"], "name": doc["name"], "status": doc["status"], "cleanup": self._cleanup_of(doc),
               "owner": doc["owner"]["name"], "created_at": doc["created_at"], "finished_at": doc.get("finished_at"),
               "cancel_requested": self.store.cancel_requested(doc["id"]), "counts": counts, "guests": guests,
               "budget": dict(doc["budget"])}
        out["receipt"] = tour_receipt(out)
        if detail:
            out["supervisor"] = dict(doc["supervisor"])
            out["lost_listeners"] = self._lost_listeners(doc["id"])
        return out

    def _lost_listeners(self, tour_id: str):
        """``{attempt: reason}`` for this tour's attempts whose concierge listener the concierge lost in a restart (that
        revoked them). ``None`` when the concierge cannot be asked; the tour's status does not depend on it."""
        try:
            lost = self.concierge_factory().health().get("lost_listeners")
        except Exception:  # noqa: BLE001
            return None
        mine = {a["id"] for a in self.store.attempts(tour_id)}
        return {k: str(v)[:160] for k, v in sorted((lost or {}).items()) if k in mine} if isinstance(lost, dict) else {}

    # ------------------------------------------------------------------ start
    def start(self, caller, payload: dict) -> dict:
        limits = self.limits
        if not limits.enabled:
            raise Refused("tours are disabled on this host", "ask the operator to set enabled = true in [tours]")
        if not isinstance(payload, dict) or not isinstance(payload.get("spec"), dict) or not isinstance(payload.get("inputs", {}), dict):
            raise Usage("the front desk got a malformed tour request", "motel tour start --help")
        if self.cabin_cfg.access == "tailscale":
            raise Refused("tour guests need cabins with access = \"ssh-allowlist\": their egress filter would cut a tailnet "
                          "connection", "ask the operator to use ssh-allowlist access for cabins")
        problems = self.setup_check() if self.setup_check is not None else []
        if problems:        # before anything is staged or booked: a tour whose bundles cannot be inspected is not started
            raise Refused("tours cannot start on this host: " + "; ".join(problems), install_hint("--upgrade") + ", then motel doctor")
        parsed = spec.parse(payload["spec"], limits=limits, cabin_types=self.cabin_cfg.types, wire=True)
        if any(g.auth == "own-login" for g in parsed.guests):
            self._check_own_login_user(caller)
        with self.store.admission(f"user-{caller.uid}", self.admission_wait_s):
            # One start at a time per user from the quota check to the moment the tour is on disk and counts as active:
            # two concurrent starts cannot both pass a ceiling.
            tid, doc, needed = self._admit(caller, parsed, limits, payload)
        return self._launch(caller, parsed, tid, doc)

    def _admit(self, caller, parsed, limits, payload) -> tuple:
        staged_before = self._check_quota(caller)
        for guest in parsed.guests:
            guest.server_type = spec.effective_server_type(guest, limits, self.cabin_cfg.types)
            guest.image = spec.effective_image(guest, limits)
        inputs = payload.get("inputs", {})
        needed = {g.bundle: "bundle" for g in parsed.guests} | {g.overlay_input: "overlay" for g in parsed.guests if g.overlay_input}
        if set(inputs) != set(needed):
            raise Usage("the tour's inputs do not match its guests", "motel tour start --help")
        tid = new_tour_id()
        wire = parsed.to_wire()
        now = self.clock()
        doc = {"schema": SCHEMA, "id": tid, "name": parsed.name, "owner": {"uid": caller.uid, "name": caller.name},
               "created_at": iso(now), "finished_at": None, "status": "staging",
               "limits": {"concierge_port": limits.concierge_port, "max_result_mb": limits.max_result_mb,
                          "max_ttl_s": int(limits.max_ttl.total_seconds())},
               "spec": wire, "inputs": {},
               "budget": {"max_eur": str(limits.max_eur_per_tour), "reserved_eur": "0", "spent_eur": "0"},
               "supervisor": {"unit": f"motel-tour-{tid}", "launched_at": None, "restarts": 0, "stopped": None, "last_seen": None, "pid": None,
                              "error": None},
               "guests": {g.id: {"status": "queued", "cleanup": "deleted", "attempts": [], "retries_used": 0, "result": None}
                          for g in parsed.guests}}
        self.store.create(tid, doc)
        try:
            with self.store.admission("host", self.admission_wait_s):      # the free-space check and the copy: one at a time on the host
                self._stage(caller, tid, doc, inputs, needed, staged_before)
        except BaseException:
            self.store.remove(tid)
            raise
        doc["status"] = "queued"
        self.store.save(doc)
        return tid, doc, needed

    def _launch(self, caller, parsed, tid, doc) -> dict:
        try:
            self.launcher(tid)
        except Exception as exc:  # noqa: BLE001
            doc["status"], doc["finished_at"] = "failed", iso(self.clock())
            doc["supervisor"]["error"] = type(exc).__name__
            self.store.save(doc)
            self.store.drop_inputs(tid)
            from motel.tours.errors import Unavailable
            raise Unavailable("could not start the tour supervisor", "sudo systemctl status motel-tour-" + tid) from None

        def launched(d):
            d["supervisor"]["launched_at"] = iso(self.clock())
        doc = self.store.update(tid, launched)
        self.store.event(tid, "launched", unit=doc["supervisor"]["unit"])
        warnings = [ownlogin.WARNING] if any(g.auth == "own-login" for g in parsed.guests) else []
        out = {"schema": OK_SCHEMA, "ok": True, "tour": self.view(doc), "warnings": warnings}
        return out

    def _check_own_login_user(self, caller) -> None:
        allowed = self.limits.own_login_allow_users
        users = {u for u in allowed if not u.startswith("@")}
        wanted = {u[1:] for u in allowed if u.startswith("@")}
        if caller.name in users or (wanted and self.groups_fn is not None and wanted & set(self.groups_fn(caller))):
            return
        raise Refused(f"{caller.name} may not use own-login: [tours.own_login] allow_users does not list this user",
                      "ask the operator to add the user to [tours.own_login] allow_users, or use an API-key binding")

    def _active(self, uid: int) -> list:
        out = []
        for tid in self.store.ids():
            try:
                doc = self.store.load(tid)
            except Fail:
                continue
            if doc["owner"]["uid"] == uid and doc["status"] not in TERMINAL_TOUR:
                out.append(doc)
        return out

    def _check_quota(self, caller) -> int:
        """Per-user ceilings on unfinished tours and on the disk their staged files use (a refusal is exit 3)."""
        limits, active = self.limits, self._active(caller.uid)
        if len(active) >= limits.max_active_tours_per_user:
            raise Refused(f"you already have {len(active)} unfinished tour(s); the ceiling is {limits.max_active_tours_per_user}",
                          "motel tour ls, then motel tour cancel ID or motel tour wait ID")
        return sum(self.store.staged_bytes(d["id"]) for d in active)

    def _stage(self, caller, tid, doc, inputs, needed, staged_before: int = 0) -> None:
        staged, incoming = {}, 0
        cap = self.limits.max_staged_mb_per_user * 1024 * 1024
        floor = self.limits.min_free_after_staging_mb * 1024 * 1024
        for key, kind in needed.items():
            item = inputs.get(key)
            if not isinstance(item, dict) or item.get("kind") != kind:
                raise Usage("the tour's inputs are malformed", "motel tour start --help")
            fd, size = open_user_file(item.get("path"), caller.uid, MAX_INPUT_BYTES if kind == "bundle" else MAX_OVERLAY_BYTES)
            incoming += size
            try:
                if staged_before + incoming > cap:
                    raise Refused(f"your unfinished tours already hold {staged_before // (1024 * 1024)} MiB of staged "
                                  f"files; the ceiling is {self.limits.max_staged_mb_per_user} MiB",
                                  "motel tour cancel an unfinished tour, or wait for it to finish")
                if self.free_bytes(self.store.root) - incoming < floor:
                    raise Refused("the host does not have enough free disk space to stage your files "
                                  f"(it keeps {self.limits.min_free_after_staging_mb} MiB free)", "ask the operator to free disk space")

                sha, size = self.store.stage_input(tid, key, fd, MAX_INPUT_BYTES if kind == "bundle" else MAX_OVERLAY_BYTES)
            finally:
                os.close(fd)
            if not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256", ""))) or sha != item["sha256"] or sha[:16] != key:
                raise Usage("an input file changed while it was being handed over; start again", "motel tour start --help")
            staged[key] = {"kind": kind, "sha256": sha, "bytes": size}
        doc["inputs"] = staged

    # ------------------------------------------------------------------ reads
    def ls(self, caller, all_: bool = False) -> dict:
        rows = []
        for tid in self.store.ids():
            try:
                doc = self.store.load(tid)
            except Fail:
                continue
            if (all_ and self._is_operator(caller)) or doc["owner"]["uid"] == caller.uid:
                rows.append(self.view(doc))
        return {"schema": OK_SCHEMA, "ok": True, "tours": rows}

    def _is_operator(self, caller) -> bool:
        return self.operator_fn(caller) if self.operator_fn is not None else caller.uid == 0

    def status(self, caller, tour_id: str) -> dict:
        return {"schema": OK_SCHEMA, "ok": True, "tour": self.view(self._owned(caller, tour_id), detail=True)}

    def wait(self, caller, tour_id: str, mode: str = "all", timeout: float = 3600.0) -> dict:
        deadline = self.clock() + timedelta(seconds=timeout)
        while True:
            doc = self._owned(caller, tour_id)
            states = [g["status"] for g in doc["guests"].values()]
            finished = [s in TERMINAL for s in states]
            settled = doc["status"] in TERMINAL_TOUR or all(finished)
            ok = settled if mode == "all" else (any(finished) or settled)
            if ok:
                return {"schema": OK_SCHEMA, "ok": True, "satisfied": True, "tour": self.view(doc)}
            if self.clock() >= deadline:
                return {"schema": OK_SCHEMA, "ok": True, "satisfied": False, "tour": self.view(doc)}
            self.sleep(POLL_S)

    def _guest(self, doc: dict, guest_id: str) -> dict:
        g = doc["guests"].get(guest_id)
        if g is None:
            raise NotFound(f"tour {doc['id']} has no guest {guest_id!r}", f"motel tour status {doc['id']}")
        return g

    def _result_dir(self, doc: dict, guest_id: str) -> str:
        g = self._guest(doc, guest_id)
        if g["status"] not in TERMINAL or not g.get("result"):
            raise Incomplete(f"guest {guest_id} has no result yet (status {g['status']})", f"motel tour wait {doc['id']}")
        return os.path.join(self.store.results_dir(doc["id"], guest_id), g["result"]["attempt"])

    def result(self, caller, tour_id: str, guest_id: str) -> dict:
        doc = self._owned(caller, tour_id)
        base = self._result_dir(doc, guest_id)
        try:
            with open(os.path.join(base, "result.json"), encoding="utf-8") as handle:
                record = json.load(handle)
        except (OSError, ValueError):
            raise NotFound(f"the result of guest {guest_id} is not readable", f"motel tour status {tour_id}") from None
        return {"schema": OK_SCHEMA, "ok": True, "result": record,
                "note": "verified is what motel measured; guest_claims and all logs are untrusted guest output"}

    def fetch(self, caller, tour_id: str, guest_id: str, artifact: str | None = None) -> RawReply:
        doc = self._owned(caller, tour_id)
        base = self._result_dir(doc, guest_id)
        if artifact is None:
            rel = art.BUNDLE
        else:
            if not art.safe_relpath(artifact):
                raise Usage("bad artifact path")
            rel = f"{art.ARTIFACT_DIR}/{artifact}"
        path = os.path.join(base, rel)
        cap = self.limits.max_result_bytes
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            what = "commits" if artifact is None else f"an artifact {artifact!r}"
            raise NotFound(f"guest {guest_id} left no {what}", f"motel tour result {tour_id} {guest_id}") from None
        with os.fdopen(fd, "rb") as handle:
            data = handle.read(cap + 1)
        if len(data) > cap:
            raise Refused("the stored result is over the size limit")
        return RawReply(data)

    def logs(self, caller, tour_id: str, guest_id: str, stream: str = "stdout", tail: int = DEFAULT_LOG_BYTES) -> dict:
        doc = self._owned(caller, tour_id)
        if stream not in LOG_FILES:
            raise Usage("stream must be stdout, stderr, runner or boot")
        g = self._guest(doc, guest_id)
        if not g["attempts"]:
            raise Incomplete(f"guest {guest_id} has not started", f"motel tour wait {tour_id}")
        tail = max(1, min(int(tail), MAX_LOG_BYTES))
        if stream == "boot":
            return {"schema": OK_SCHEMA, "ok": True, "tour": tour_id, "guest": guest_id, "stream": stream, "truncated": False,
                    "untrusted": True, "text": self._boot_report(tour_id, guest_id), "boot": self._last_boot(tour_id, guest_id)}
        aid = (g.get("result") or {}).get("attempt") or g["attempts"][-1]
        path = os.path.join(self.store.results_dir(tour_id, guest_id), aid, LOG_FILES[stream])
        text, truncated = "", False
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            with os.fdopen(fd, "rb") as handle:
                size = handle.seek(0, os.SEEK_END)
                handle.seek(max(0, size - tail))
                truncated = size > tail
                text = handle.read(tail).decode("utf-8", "replace")
        except OSError:
            pass
        text = "".join(c for c in text if c in "\n\t" or c.isprintable())
        reply = {"schema": OK_SCHEMA, "ok": True, "tour": tour_id, "guest": guest_id, "stream": stream, "truncated": truncated,
                 "untrusted": True, "text": text}
        try:
            last = self.store.load_attempt(tour_id, aid)
        except Exception:  # noqa: BLE001
            last = None
        if not text.strip() and not (last or {}).get("command_started_at"):
            boot = self._boot_report(tour_id, guest_id)             # the guest never ran its command: say why
            if boot:
                reply["text"], reply["boot"], reply["stream"] = boot, self._last_boot(tour_id, guest_id), "boot"   # not stdout: it is not the command's output
        return reply

    def _last_boot(self, tour_id: str, guest_id: str):
        for rec in reversed([a for a in self.store.attempts(tour_id) if a["guest"] == guest_id]):
            if rec.get("boot") or rec.get("error"):
                return {"attempt": rec["id"], "status": rec["status"], "phase": rec.get("phase"), "error": rec.get("error"),
                        "boot": rec.get("boot")}
        return None

    def _boot_report(self, tour_id: str, guest_id: str) -> str:
        """Why a guest has not run its command (yet): per attempt, the phase it reached, what failed, and the last thing the
        controller saw while it waited for the cabin (provider phase, ssh exit status and stderr line, TCP probe, boot stage)."""
        lines = []
        for rec in [a for a in self.store.attempts(tour_id) if a["guest"] == guest_id]:
            boot = rec.get("boot") or {}
            lines.append(f"attempt {rec['id']}: {rec['status']}, phase {rec.get('phase')}" + (f", error: {rec['error']}" if rec.get("error") else ""))
            if boot:
                lines.append(f"  last probe {boot.get('at')} (poll {boot.get('polls')}): {boot.get('summary') or boot.get('phase')}")
                channel, ready = boot.get("channel") or {}, boot.get("ready") or {}
                if channel:
                    lines.append(f"  guest channel: ssh exit {channel.get('rc')}, {channel.get('error')}"
                                 + (f", tcp/22 {channel['tcp22']}" if channel.get("tcp22") else ""))
                if ready:
                    b = ready.get("boot") or {}
                    lines.append(f"  guest bootstrap: {ready.get('bootstrap')}, stage {b.get('stage')}, cloud-init {b.get('cloud_init')}, last line: {b.get('last')}")
                    if b.get('errors'):
                        lines.append("  errors: " + " | ".join(b['errors']))
        return "\n".join(lines)

    # ------------------------------------------------------------------ cancel
    def cancel(self, caller, tour_id: str) -> dict:
        doc = self._owned(caller, tour_id)
        if doc["status"] in TERMINAL_TOUR:
            return {"schema": OK_SCHEMA, "ok": True, "already_finished": True, "tour": self.view(doc)}
        self.store.request_cancel(tour_id)                    # the flag first: a worker about to book will see it
        revoked = None
        try:
            revoked = self.concierge_factory().revoke(tour=tour_id)   # then no placeholder of this tour works any more
        except Exception:  # noqa: BLE001  the attempts revoke their own tokens again
            pass
        self.store.event(tour_id, "cancel_requested", by=caller.uid, revoked=revoked)
        if not self.unit_active(tour_id):
            self._finish_orphan(tour_id)
        return {"schema": OK_SCHEMA, "ok": True, "cancelling": True, "tour": self.view(self.store.load(tour_id))}

    def _finish_orphan(self, tour_id: str, reason: str = "the tour was cancelled") -> None:
        """No supervisor is running: clean up here, in the same order the supervisor would."""
        env = self.env_factory()
        runner = AttemptRunner(env)
        recovery.recover_tour(runner, tour_id, reason=reason)

        def end(doc):
            for g in doc["guests"].values():
                if g["status"] not in TERMINAL:
                    g["status"] = "cancelled"
            doc["status"] = "cancelled"
            doc["finished_at"] = iso(self.clock())
        self.store.update(tour_id, end)
        self.store.drop_inputs(tour_id)
        recovery.retry_cleanups(runner, tour_id)

    # ------------------------------------------------------------------ operator
    def supervise(self, tour_id: str) -> dict:
        env = self.env_factory()
        with stop_on_sigterm(env.stop):
            doc = Supervisor(env, tour_id).run()
        return {"schema": OK_SCHEMA, "ok": True, "tour": self.view(doc)}

    def reap(self, apply: bool) -> dict:
        """Reconcile every tour: restart dead supervisors, finish cancelled or hopeless tours, retry pending
        deletions, drop unfinished staging and, after the retention period, finished tours. Dry run by default."""
        actions, errors = [], []
        env = None
        for tid in self.store.ids():
            try:
                doc = self.store.load(tid)
                if doc["status"] == "staging":
                    age = self.store.age(doc)
                    if age is not None and age > MAX_STAGING_AGE:
                        actions.append({"tour": tid, "action": "remove_unfinished_staging"})
                        if apply:
                            self.store.remove(tid)
                elif doc["status"] not in TERMINAL_TOUR:
                    if not self.unit_active(tid):
                        self._reap_dead(tid, doc, apply, actions)
                else:
                    if self.store.staged_bytes(tid):          # a finished tour keeps none of the caller's files
                        actions.append({"tour": tid, "action": "drop_staged_inputs"})
                        if apply:
                            self.store.drop_inputs(tid)
                    pending = [a for a in self.store.attempts(tid) if a["cleanup"] != "deleted" or a.get("revoke_pending")]
                    if pending:
                        actions.append({"tour": tid, "action": "retry_cleanup", "attempts": [a["id"] for a in pending]})
                        if apply:
                            env = env or self.env_factory()
                            recovery.retry_cleanups(AttemptRunner(env), tid)
                            left = [a for a in self.store.attempts(tid) if a["cleanup"] != "deleted"]
                            if left:
                                errors.append(f"{tid}: {len(left)} cabin(s) still not deleted")
                    else:
                        age = self.store.age(doc)
                        if age is not None and age > RETENTION:
                            actions.append({"tour": tid, "action": "remove_old_tour"})
                            if apply:
                                self.store.remove(tid)
            except Fail as exc:
                errors.append(f"{tid}: {exc.message}")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{tid}: {type(exc).__name__}")
        if self.inspect_root:
            try:
                status = {a["id"]: a["status"] for tid in self.store.ids() for a in self.store.attempts(tid)}
                actions += bundle.reap_scratch(self.inspect_root, status.get, now=time.time(), apply=apply)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"inspection scratch: {type(exc).__name__}")
        return {"schema": OK_SCHEMA, "ok": not errors, "applied": apply, "actions": actions, "errors": errors}

    def _reap_dead(self, tid: str, doc: dict, apply: bool, actions: list) -> None:
        if self.store.cancel_requested(tid):
            actions.append({"tour": tid, "action": "finish_cancelled"})
            if apply:
                self._finish_orphan(tid)
        elif doc["supervisor"].get("stopped") or doc["supervisor"]["restarts"] < MAX_RESTARTS:
            actions.append({"tour": tid, "action": "restart_supervisor"})
            if apply:
                def bump(d):
                    if d["supervisor"].get("stopped"):      # it ended on a SIGTERM (a systemd stop, a reboot): not a crash
                        d["supervisor"]["stopped"] = None
                    else:
                        d["supervisor"]["restarts"] += 1
                self.store.update(tid, bump)
                self.launcher(tid)
        else:
            actions.append({"tour": tid, "action": "cancel_dead_tour"})
            if apply:
                self.store.request_cancel(tid)
                try:
                    self.concierge_factory().revoke(tour=tid)
                except Exception:  # noqa: BLE001
                    pass
                self._finish_orphan(tid, reason="the supervisor kept stopping")

    def own_login(self, action: str, data: bytes | None = None) -> dict:
        path = self.limits.own_login_token_file
        if action == "set":
            info = ownlogin.set_token(path, data or b"")
        elif action == "clear":
            info = ownlogin.clear(path)
        else:
            info = ownlogin.status(path)
        return {"schema": OK_SCHEMA, "ok": True, "own_login": {"enabled": self.limits.own_login_enabled, **info}}


