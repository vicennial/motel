"""One attempt of one guest, start to finish, on one fresh cabin.

Order (``docs/design/tours-contract.md``): reserve budget, ``Controller.checkin`` (with the egress profile),
issue a placeholder, copy the inputs, run the runner over unprivileged ssh with a reverse forward, collect with
caps, validate, revoke, ``checkout``, settle cost and usage. A cabin is never reused. Intent is saved before
every create. Execution status and cleanup status are separate.

* A failure the engine itself saw before it launched the runner (a failed copy, preflight, booking...) may be
  retried, once, on another fresh cabin. The engine saves ``command_started_at`` BEFORE it launches the runner;
  from then on the attempt is never run again, whatever the guest's ``run.json`` says.
* Cancel order: revoke, stop, bounded collect, delete.
* In every case the teardown revokes the token and checks the cabin out; a failed deletion is recorded as
  ``deletion_pending`` and ``tour reap`` retries it.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import shutil
import threading
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from motel.cabins.controller import EXPIRY_GRACE
from motel.cabins.errors import CheckinAborted, Fail, PolicyRefused, Usage
from motel.cabins.util import OWNER_RE, iso
from motel.receipts import cabin_receipt
from motel.tours import artifacts as art
from motel.tours import ownlogin
from motel.tours.state import (
    TourStore,
    new_attempt_id,
    open_dir_path,
    worst_cleanup,
    write_new_file,
)
from motel.tours.transport import Target, TransportError

RESULT_SCHEMA = "motel/tour-result/v1"
CARD_SCHEMA = "motel/guest-card/v1"
SETUP_MARGIN = timedelta(minutes=30)      # cabin bootstrap (up to 20 min) plus time to collect
RUN_GRACE_S = 60                          # the runner enforces the ttl itself; the controller waits this much longer
BOUNDED_BYTES = 4 * 1024 * 1024           # what a cancelled attempt may still bring back
KEY_FILE = "tour_ed25519"
MAX_CHANGED_IN_RECORD = 200
MAX_RETRIES = 1                           # a guest is run again at most once, and only after a pre-start failure


class Cancelled(Exception):
    pass


class PreStart(Exception):
    """A failure before the guest command started. ``retry`` says whether another fresh cabin may help."""

    def __init__(self, message: str, retry: bool = True):
        super().__init__(message)
        self.retry = retry


class Stopped(Cancelled):
    """The supervisor was told to stop (SIGTERM): clean up this attempt now, the tour is not cancelled."""


class OverBudget(Exception):
    pass


@dataclass
class Env:
    store: TourStore
    limits: object
    cabin_cfg: object
    controller_factory: object      # (owner dict) -> a cabins Controller
    concierge: object
    transport: object
    keygen: object                  # (directory, name, comment) -> (private, public)
    extras: object                  # deps.userdata_extras
    runner_files: object            # deps.runner_files: () -> {name: bytes}
    token_reader: object            # () -> own-login token (str)
    slots: object
    own_slots: object
    clock: object
    stop: object = field(default_factory=threading.Event)     # set by the SIGTERM handler of the supervisor
    inspect_root: str | None = None   # where inspection scratch directories live (a restarted supervisor cleans its own)
    accepting: object = None        # () -> bool: False once [tours] or [cabins] is disabled; queued guests then end cancelled
    block_reason: object = None     # () -> "tours_disabled" | "cabins_disabled": why accepting() said no (default tours_disabled)
    inspect_bundle: object = None   # (base_bundle, result_bundle, base_sha, branch, attempt, token, deliver_to) -> {head, changed_files, delivered, ...}


@dataclass
class Outcome:
    attempt: str
    status: str
    retry: bool = False


@dataclass
class Ctx:
    tour: dict
    guest: dict
    rec: dict
    owner: dict
    ctl: object = None
    target: Target | None = None
    reserved: Decimal = Decimal(0)
    cabin_ttl: timedelta = timedelta(0)
    token: str | None = None            # concierge placeholder
    revoked: bool = False
    cancelled: bool = False
    over_budget: bool = False
    timed_out: bool = False
    pre_start: bool = False
    retry: bool = False
    started: bool = False
    stopped: bool = False
    will_retry: bool = False
    requeued: bool = False             # a SIGTERM stop before the command: queued again without using the retry
    runner_exit: int | None = None
    error: str | None = None
    reason: str | None = None            # why the engine itself failed the attempt (concierge_listener_lost)
    run_doc: dict | None = None
    claims: object = None
    saved: list = field(default_factory=list)
    violations: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    partial: bool = False
    view: dict | None = None
    final_view: dict | None = None      # the cabin record after checkout (else the checkin one): the receipt's source
    lease_id: str | None = None
    start_time: object = None
    runner_log: str | None = None
    own_token: str | None = None
    upstream_port: int | None = None      # this attempt's own concierge listener (the host end of the reverse forward)
    saved_files: list = field(default_factory=list)
    inspected: dict | None = None         # head and changed files computed here from the fetched bundle
    saved_summary: dict | None = None
    bundle_delivered: dict | None = None  # size and digest of the bundle motel rebuilt (what the user is given)


def owner_label(owner: dict) -> str:
    return owner["name"] if OWNER_RE.fullmatch(owner["name"]) else f"uid-{owner['uid']}"


class AttemptRunner:
    def __init__(self, env: Env):
        self.env = env
        self.store = env.store

    # ------------------------------------------------------------------ public
    def run(self, tour_id: str, guest_id: str, n: int) -> Outcome:
        env, store = self.env, self.store
        tour = store.load(tour_id)
        guest = next(g for g in tour["spec"]["guests"] if g["id"] == guest_id)
        aid = new_attempt_id()
        rec = {"id": aid, "tour": tour_id, "guest": guest_id, "n": n, "status": "queued", "cleanup": "deleted",
               "phase": "queued", "lease_id": None, "command_started_at": None, "pre_start": False, "reserved_eur": "0",
               "cost_eur": None, "usage": None, "error": None, "auth": guest["auth"], "created_at": iso(env.clock()),
               "finished_at": None, "revoke_pending": False}
        store.save_attempt(tour_id, rec)

        def register(doc):
            doc["guests"][guest_id]["attempts"].append(aid)
            doc["guests"][guest_id]["status"] = "queued"
        store.update(tour_id, register)
        ctx = Ctx(tour=tour, guest=guest, rec=rec, owner=tour["owner"])
        cancel = lambda: store.cancel_requested(tour_id) or env.stop.is_set()  # noqa: E731
        slot = own_slot = None
        try:
            self._check_accepting(ctx)
            slot = env.slots.acquire(cancel)
            if slot is not None and guest["auth"] == "own-login":
                own_slot = env.own_slots.acquire(cancel)
                if own_slot is None:
                    slot.release()
                    slot = None
            if slot is None:
                raise (Stopped() if env.stop.is_set() and not store.cancel_requested(tour_id) else Cancelled())
            self._check_accepting(ctx)         # the wait for a slot can be long: the operator may have disabled tours meanwhile
            self._body(ctx)
        except Stopped:
            self._mark_stopped(ctx)
        except Cancelled:
            ctx.cancelled = True
        except OverBudget:
            ctx.over_budget = True
        except PreStart as exc:
            if self._stopped_before_the_command(ctx):
                self._mark_stopped(ctx)
            else:
                ctx.pre_start, ctx.retry, ctx.error = True, exc.retry, str(exc)
        except Fail as exc:
            if self._stopped_before_the_command(ctx):
                self._mark_stopped(ctx)
            else:
                ctx.error = exc.message
        except Exception as exc:  # noqa: BLE001  the teardown below must always run
            if self._stopped_before_the_command(ctx):
                self._mark_stopped(ctx)
            else:
                ctx.error = f"internal error: {type(exc).__name__}"
        finally:
            try:
                self._teardown(ctx)
            finally:
                for held in (own_slot, slot):
                    if held is not None:
                        held.release()
        return Outcome(aid, ctx.rec["status"], retry=ctx.will_retry)

    def _check_accepting(self, ctx: Ctx) -> None:
        """A disabled ``[tours]`` (or ``[cabins]``, which tours run on) stops NEW attempts: nothing is booked, and the guest
        ends cancelled with the reason ``tours_disabled`` (``cabins_disabled``). Attempts that already booked a cabin are not
        asked again and finish normally."""
        accepting = self.env.accepting
        if accepting is not None and not accepting():
            reason = self.env.block_reason() if self.env.block_reason is not None else None
            ctx.reason = reason or "tours_disabled"
            raise Cancelled()

    def _stopped_before_the_command(self, ctx: Ctx) -> bool:
        """systemd's KillMode=control-group sends SIGTERM to the ssh children too, so the step that was running when the
        stop came fails with an error of its own. While the stop flag is set and the command has not started, that
        error is the stop, not a failure of the attempt: it must not use the guest's retry."""
        return self.env.stop.is_set() and not ctx.started and not self.store.cancel_requested(ctx.tour["id"])

    @staticmethod
    def _mark_stopped(ctx: Ctx) -> None:
        ctx.stopped = True
        ctx.pre_start, ctx.retry, ctx.error = not ctx.started, not ctx.started, "the supervisor was stopped"

    # ------------------------------------------------------------------ the phases
    def _cancel_check(self, ctx: Ctx) -> None:
        if self.store.cancel_requested(ctx.tour["id"]):
            raise Cancelled()
        if self.env.stop.is_set():
            raise Stopped()

    def _body(self, ctx: Ctx) -> None:
        env = self.env
        self._cancel_check(ctx)
        ctx.ctl = env.controller_factory(ctx.owner)
        self._phase_reserve(ctx)
        self._cancel_check(ctx)
        self._phase_book(ctx)
        self._cancel_check(ctx)
        self._phase_issue(ctx)
        self._cancel_check(ctx)
        self._phase_copy(ctx)
        self._cancel_check(ctx)
        self._phase_run(ctx)
        if ctx.cancelled or ctx.stopped:
            self._cancel_sequence(ctx)
            return
        self._phase_collect(ctx, bounded=ctx.timed_out or ctx.runner_exit != 0)

    def _set(self, ctx: Ctx, *, status: str | None = None, phase: str | None = None, cleanup: str | None = None) -> None:
        rec = ctx.rec
        if status:
            rec["status"] = status
        if phase:
            rec["phase"] = phase
        if cleanup:
            rec["cleanup"] = cleanup
        rec["lease_id"] = ctx.lease_id
        self.store.save_attempt(ctx.tour["id"], rec)
        gid = ctx.guest["id"]

        def apply(doc):
            g = doc["guests"][gid]
            if status:
                g["status"] = status
            attempts = [self.store.load_attempt(doc["id"], a) if a != rec["id"] else rec for a in g["attempts"]]
            g["cleanup"] = worst_cleanup(a["cleanup"] for a in attempts)
        self.store.update(ctx.tour["id"], apply)

    def _phase_reserve(self, ctx: Ctx) -> None:
        env, guest = self.env, ctx.guest
        self._set(ctx, status="booking", phase="reserve")
        ttl = timedelta(seconds=guest["ttl_s"])
        ctx.cabin_ttl = ttl + SETUP_MARGIN
        try:
            offer = ctx.ctl.offer(guest["server_type"], ctx.cabin_ttl, image=guest["image"])
        except (PolicyRefused, Usage) as exc:
            raise PreStart(exc.message, retry=False) from None
        except Fail as exc:
            raise PreStart(exc.message) from None
        # The provider bills every started hour from creation, and a cabin may live until its expiry plus the grace before
        # the reaper deletes it even if busy: reserve for those started hours. This is a bound, not a guarantee: the cost can
        # exceed the reservation by one started hour (one hourly price per attempt) when the reaper, not the tour, deletes the
        # cabin and a pass runs late, or when the provider's clock runs behind motel's.
        hours = math.ceil((ctx.cabin_ttl + EXPIRY_GRACE).total_seconds() / 3600)
        need = Decimal(offer["total_hourly_net_eur"]) * hours
        ok = []

        def reserve(doc):
            b = doc["budget"]
            if Decimal(b["reserved_eur"]) + Decimal(b["spent_eur"]) + need > Decimal(b["max_eur"]):
                return
            b["reserved_eur"] = str(Decimal(b["reserved_eur"]) + need)
            # The attempt records what it reserved in the SAME locked write that raises the tour's reservation, so
            # a crash right after can never leave money reserved that no attempt knows about.
            ctx.rec["reserved_eur"] = str(need)
            env.store.save_attempt(ctx.tour["id"], ctx.rec)
            ok.append(True)
        env.store.update(ctx.tour["id"], reserve)
        if not ok:
            ctx.error = f"the tour's cloud budget of EUR {ctx.tour['budget']['max_eur']} would be exceeded"
            raise OverBudget()
        ctx.reserved = need

    def _phase_book(self, ctx: Ctx) -> None:
        env, guest, ctl = self.env, ctx.guest, ctx.ctl
        self._set(ctx, status="booking", phase="checkin")
        scratch = ctl.store.scratch()
        try:
            private, public = env.keygen(scratch, "tour_client", f"motel-tour-{ctx.rec['id']}")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        extras = env.extras(hosts=list(guest["egress"]), dns_resolvers=list(env.cabin_cfg.dns_resolvers),
                            concierge_port=self._forward_port(guest), own_login=guest["auth"] == "own-login")
        # Intent first: the attempt says a cabin may exist from here on (its lease id starts with the attempt id).
        ctx.rec["intent_at"] = iso(env.clock())
        self._set(ctx, cleanup="live", phase="checkin")
        tid = ctx.tour["id"]

        def on_boot(info):
            ctx.rec["boot"] = info            # why the guest is still booking, as the controller last saw it
            self.store.save_attempt(tid, ctx.rec)
        try:
            view = ctl.checkin(guest["server_type"], ctx.cabin_ttl, ctx.rec["id"], owner_label(ctx.owner), public, None,
                               image=guest["image"], userdata_extras=extras, on_boot=on_boot,
                               abort=lambda: self.store.cancel_requested(tid) or env.stop.is_set())
        except BaseException as exc:
            self._adopt(ctx)
            if isinstance(exc, CheckinAborted):       # a cancel (or a stop) while the cabin was still booting: the cabin is gone already
                raise (Cancelled() if self.store.cancel_requested(tid) else Stopped()) from None
            if isinstance(exc, Fail):
                raise PreStart(exc.message) from None
            if isinstance(exc, Exception):
                raise PreStart(type(exc).__name__) from None
            raise
        ctx.view, ctx.lease_id = view, view["lease_id"]
        ctx.rec.pop("boot", None)                 # the report is only for a guest that is still booking (docs/cli-json.md)
        self._set(ctx, phase="booked")
        try:
            with ctl.store.key_dir_fd(ctx.lease_id) as dfd:
                ctl.store.write_key(dfd, KEY_FILE, private)
        except Exception as exc:
            raise PreStart(f"could not store the attempt key ({type(exc).__name__})") from None
        finally:
            del private
        ctx.target = Target(lease_id=ctx.lease_id, alias=view["name"], address=view["address"],
                            key_path=os.path.join(ctl.store.key_path(ctx.lease_id), KEY_FILE),
                            known_hosts=os.path.join(ctl.store.key_path(ctx.lease_id), "known_hosts"))

    def _forward_port(self, guest: dict):
        """The reverse forward to the concierge exists only for a guest that talks to it (an API-key guest)."""
        if guest["auth"] == "own-login" or not guest["secrets"]:
            return None
        return self.env.limits.concierge_port

    def _adopt(self, ctx: Ctx) -> None:
        """A failed checkin may leave a lease (cleanup pending). Find it by the attempt id in its name."""
        try:
            found = [r for r in ctx.ctl.store.all() if str(r.get("lease_id", "")).startswith(ctx.rec["id"] + "-")]
        except Exception:  # noqa: BLE001
            found = []
        if found:
            ctx.lease_id = found[0]["lease_id"]
            self._set(ctx, phase="checkin_failed")
        else:
            self._set(ctx, cleanup="deleted", phase="checkin_failed")

    def _phase_issue(self, ctx: Ctx) -> None:
        env, guest = self.env, ctx.guest
        if guest["auth"] == "own-login" or not guest["secrets"]:
            return          # no placeholder: own-login sends nothing through the concierge; a command may need none
        expires = env.clock() + timedelta(seconds=guest["ttl_s"]) + timedelta(minutes=30)
        # Intent first: if the supervisor dies inside issue(), recovery still knows a placeholder may exist.
        ctx.rec["token_issuing"] = True
        self._set(ctx, phase="issuing")
        try:
            ctx.token = env.concierge.issue(tour=ctx.tour["id"], guest=guest["id"], attempt=ctx.rec["id"],
                                            bindings=list(guest["secrets"]), expires_at=iso(expires),
                                            budget_usd=float(guest["budget_usd"]), max_concurrent=2)
        except Exception as exc:  # noqa: BLE001  kind "busy": no listener port is free right now; another cabin may find one
            raise PreStart(f"the concierge refused a placeholder ({getattr(exc, 'kind', None) or type(exc).__name__})") from None
        ctx.rec["token_issued"] = True
        ctx.upstream_port = self._listener_port(ctx)
        self._set(ctx, phase="issued")

    def _listener_port(self, ctx: Ctx) -> int:
        """The attempt's own concierge listener (loopback, controller side). The guest's fixed port is forwarded to it."""
        try:
            port = int(self.env.concierge.endpoint(attempt=ctx.rec["id"])["port"])
        except Exception as exc:  # noqa: BLE001  gone = revoked, expired or lost in a restart; the others are a concierge problem
            what = "no open listener (concierge_listener_lost)" if getattr(exc, "kind", None) == "gone" else type(exc).__name__
            raise PreStart(f"the concierge gave no listener for this attempt ({what})") from None
        if not 0 < port < 65536:
            raise PreStart("the concierge gave an invalid listener port")
        return port

    def _card(self, ctx: Ctx) -> dict:
        g, tid = ctx.guest, ctx.tour["id"]
        return {"schema": CARD_SCHEMA, "tour": tid, "guest": g["id"], "attempt": ctx.rec["id"], "base": g["base"],
                "branch": f"motel/{tid}/{g['id']}", "adapter": g["adapter"], "prompt": g.get("prompt", ""),
                "argv": g.get("argv", []), "env": g["env"], "bindings": [] if g["auth"] == "own-login" else list(g["secrets"]),
                "auth": "own-login" if g["auth"] == "own-login" else "api-key", "placeholder": ctx.token or "",
                "ttl_s": g["ttl_s"], "max_turns": g["max_turns"], "artifacts": g["artifacts"],
                "max_result_mb": self.env.limits.max_result_mb} | (
            {} if g["auth"] == "own-login" else {"concierge_port": self.env.limits.concierge_port}) | (
            {} if g.get("max_output_tokens") is None else {"max_output_tokens": g["max_output_tokens"]}) | (
            {} if g.get("model") is None else {"model": g["model"]})

    def _phase_copy(self, ctx: Ctx) -> None:
        env, store, tid = self.env, self.store, ctx.tour["id"]
        self._set(ctx, phase="copy")
        tmp = store.path(tid, "tmp")
        card_path = os.path.join(tmp, ctx.rec["id"] + ".card.json")
        fd = os.open(card_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(self._card(ctx), handle, sort_keys=True)
        ctx.saved.append(card_path)
        puts = []
        for name, data in sorted(env.runner_files().items()):
            path = os.path.join(tmp, f"{ctx.rec['id']}.{name}")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            ctx.saved.append(path)
            puts.append((name, path))
        try:
            puts += [("card.json", card_path), ("input.bundle", store.input_path(tid, ctx.guest["bundle"]))]
            if ctx.guest.get("overlay_input"):
                puts.append(("overlay.patch", store.input_path(tid, ctx.guest["overlay_input"])))
            for name, path in puts:
                self._cancel_check(ctx)
                env.transport.put(ctx.target, name, path)
            self._cancel_check(ctx)
            env.transport.preflight(ctx.target, self._forward_port(ctx.guest), forward_to=ctx.upstream_port)
        except TransportError as exc:
            raise PreStart(str(exc)) from None
        finally:
            for path in ctx.saved:             # the card holds the placeholder: do not keep it
                with contextlib.suppress(OSError):
                    os.unlink(path)

    def _phase_run(self, ctx: Ctx) -> None:
        env, store, tid = self.env, self.store, ctx.tour["id"]
        stdin_line = None
        if ctx.guest["auth"] == "own-login":
            try:
                ctx.own_token = env.token_reader()
            except Fail as exc:
                raise PreStart(exc.message, retry=False) from None
            stdin_line = (ctx.own_token + "\n").encode()
        self._cancel_check(ctx)
        # Persist the start BEFORE launching: from here on nothing is retried.
        ctx.started = True
        ctx.start_time = env.clock()
        ctx.rec["command_started_at"] = iso(ctx.start_time)
        self._set(ctx, status="running", phase="running")
        results = os.path.join(store.results_dir(tid, ctx.guest["id"], create=True), ctx.rec["id"])
        os.makedirs(results, mode=0o700, exist_ok=True)
        ctx.runner_log = os.path.join(results, "runner.log")
        try:
            rr = env.transport.run(ctx.target, forward_port=self._forward_port(ctx.guest), forward_to=ctx.upstream_port,
                                   timeout=ctx.guest["ttl_s"] + RUN_GRACE_S,
                                   cancel=lambda: store.cancel_requested(tid) or env.stop.is_set(),
                                   log_path=ctx.runner_log, stdin_line=stdin_line)
        finally:
            stdin_line = None
            self._redact_file(ctx.runner_log, ctx.own_token)       # also when the run raised: the log is on disk already
        ctx.runner_exit, ctx.timed_out = rr.exit_status, rr.timed_out
        if rr.cancelled:                  # the user's cancel, or the supervisor's own stop
            ctx.cancelled = self.store.cancel_requested(tid)
            ctx.stopped = not ctx.cancelled
            if ctx.stopped:
                ctx.error = "the supervisor was stopped"

    @staticmethod
    def _redact_file(path: str | None, token: str | None) -> None:
        if not path or not token or not os.path.exists(path):
            return
        with open(path, "rb") as handle:
            data = handle.read()
        clean = ownlogin.redact(data, token, tail=True)
        if clean != data:
            with open(path, "wb") as handle:
                handle.write(clean)

    def _stop(self, ctx: Ctx) -> None:
        with contextlib.suppress(Exception):
            self.env.transport.stop(ctx.target)

    def _cancel_sequence(self, ctx: Ctx) -> None:
        """revoke, stop (inside the collect), bounded collect; the teardown then deletes."""
        self._revoke(ctx)
        if ctx.started and ctx.target is not None:
            self._phase_collect(ctx, bounded=True)

    def _revoke(self, ctx: Ctx) -> None:
        if (ctx.token or ctx.rec.get("token_issuing")) and not ctx.revoked:   # an issue() that died may still have succeeded
            try:
                self.env.concierge.revoke(attempt=ctx.rec["id"])
                ctx.revoked = True
            except Exception as exc:  # noqa: BLE001
                if getattr(exc, "kind", None) == "not_found":
                    ctx.revoked = True                 # the concierge has already forgotten the attempt: nothing to revoke
                else:
                    ctx.rec["revoke_pending"] = True   # tokens also expire on their own; reap retries

    # ------------------------------------------------------------------ collection
    def _phase_collect(self, ctx: Ctx, bounded: bool) -> None:
        env, store, tid = self.env, self.store, ctx.tour["id"]
        self._set(ctx, status="collecting", phase="collecting")
        self._stop(ctx)           # on every collection: nothing of the guest may still run (or write) while we read
        ctx.partial = bounded
        cap = BOUNDED_BYTES if bounded else env.limits.max_result_bytes
        t_list, t_fetch = (30, 60) if bounded else (120, 600)
        base = os.path.join(store.results_dir(tid, ctx.guest["id"], create=True), ctx.rec["id"])
        os.makedirs(base, mode=0o700, exist_ok=True)
        root_fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            self._collect_into(ctx, root_fd, cap, t_list, t_fetch, bounded)
        except TransportError as exc:
            ctx.error = ctx.error or f"collecting the output failed: {exc}"
        finally:
            os.close(root_fd)

    def _fetch_all(self, ctx, rel, cap, timeout) -> bytes:
        return b"".join(self.env.transport.fetch(ctx.target, rel, max_bytes=cap, timeout=timeout))

    def _collect_into(self, ctx, root_fd, cap, t_list, t_fetch, bounded) -> None:
        env = self.env
        entries = env.transport.list_out(ctx.target, timeout=t_list)
        by = {e.path: e for e in entries}
        run_entry = by.get(art.RUN_JSON)
        run = None
        if run_entry is not None and run_entry.kind == "f" and run_entry.size <= art.MAX_RUN_JSON:
            try:
                run = art.validate_run_doc(art.loads_limited(self._fetch_all(ctx, art.RUN_JSON, art.MAX_RUN_JSON, t_fetch)),
                                           ctx.rec["id"])
            except (ValueError, RecursionError, art.BadRunDoc) as exc:
                ctx.violations.append(f"run.json refused: {exc}")
        if run is None:
            ctx.notes.append("the guest left no usable run.json")
            ctx.error = ctx.error or ("the runner could not write run.json (exit 70)" if ctx.runner_exit == 70
                                      else "the guest left no usable run.json")
            run = {"exit_code": None, "termination": "killed", "started": "", "finished": "", "head": None,
                   "bundle_sha256": None, "changed_files": [], "artifacts": [], "stdout_bytes": 0, "stderr_bytes": 0}
            only_logs = True
        else:
            only_logs = False
        ctx.run_doc = run
        if run.get("termination") == "failed":
            # What the guest says about "not started" is a claim: the engine launched the runner, so no retry.
            ctx.error = ctx.error or f"the guest could not run the job ({run.get('reason') or 'unknown reason'})"
        if run.get("stragglers_left") or run.get("reason") in art.UNCLEAN_REASONS:
            # The runner could not make out/ safe (a process of the agent was still running, or the clean-up failed):
            # it declares nothing to collect, and whatever sits in out/ anyway is not fetched. The guest has failed.
            why = (f"{run['stragglers_left']} process(es) of the agent could not be killed on the guest"
                   if run.get("stragglers_left") else "the runner could not clean the guest's output directory")
            ctx.violations.append(why + "; nothing was collected from it (only the logs)")
            ctx.error = ctx.error or f"the guest could not hand over a clean result ({run.get('reason') or 'stragglers_left'})"
            run["bundle_sha256"], run["result_json"], run["artifacts"] = None, False, []
            run["termination"] = "failed"
        plan = art.plan_collection(entries, run, ctx.guest["artifacts"] if not only_logs else [], cap)
        ctx.violations.extend(plan.violations)
        ctx.notes.extend(plan.notes)
        total = 0
        for item in plan.items:
            if only_logs and item.role not in ("stdout", "stderr"):
                continue
            try:
                kept = self._fetch_item(ctx, root_fd, item, t_fetch)
            except (OSError, RecursionError, ValueError, Fail) as exc:
                # One bad item never drops the others: report it and go on (a good copy is never unlinked).
                ctx.violations.append(f"{item.remote}: not collected ({type(exc).__name__})")
                continue
            if kept is not None:
                total += kept["bytes"]
                ctx.saved_files.append(kept)
            if total > cap:
                ctx.violations.append("the result is over the size limit; the rest was not fetched")
                break

    def _fetch_item(self, ctx, root_fd, item, timeout):
        parts = item.local.split("/")
        dfd = open_dir_path(root_fd, parts[:-1]) if len(parts) > 1 else os.dup(root_fd)
        name = parts[-1]
        created = False
        try:
            try:
                chunks = self.env.transport.fetch(ctx.target, item.remote, max_bytes=item.max_bytes, timeout=timeout,
                                                  truncate=item.truncate)
                leak = []
                if item.role in ("stdout", "stderr"):
                    chunks = [ownlogin.redact(b"".join(chunks), ctx.own_token, tail=True)]
                elif item.role in ("bundle", "artifact", "claims") and ctx.own_token:
                    chunks = ownlogin.scan(chunks, ctx.own_token, leak)     # defence in depth: the runner withholds these too
                sha, size = write_new_file(dfd, name, chunks)
                created = True
            except FileExistsError:
                ctx.violations.append(f"{item.remote}: a file of that name was already collected; the second one was ignored")
                return None
            except (TransportError, OSError) as exc:
                ctx.violations.append(f"{item.remote}: not fetched ({exc})")
                return None
            if leak:
                ctx.violations.append(f"{item.remote}: contained the own-login token; withheld")
                ctx.error = ctx.error or "the guest output contained the own-login token (secret_leak); it was withheld"
                os.unlink(name, dir_fd=dfd)
                return None
            if item.sha256 is not None and sha != item.sha256:
                ctx.violations.append(f"{item.remote}: digest differs from what the guest declared; discarded")
                os.unlink(name, dir_fd=dfd)
                return None
            if item.role == "bundle" and not self._accept_bundle(ctx, dfd, name):
                os.unlink(name, dir_fd=dfd)
                return None
            if item.role == "claims" and not self._accept_claims(ctx, dfd, name):
                os.unlink(name, dir_fd=dfd)
                return None
            if item.role == "bundle" and ctx.bundle_delivered:
                size, sha = ctx.bundle_delivered["bytes"], ctx.bundle_delivered["sha256"]
            return {"role": item.role, "path": item.local, "bytes": size, "sha256": sha}
        except BaseException:
            if created:
                with contextlib.suppress(OSError):
                    os.unlink(name, dir_fd=dfd)
            raise
        finally:
            os.close(dfd)

    def _accept_bundle(self, ctx, dfd, name) -> bool:
        with open(name, "rb", opener=lambda p, f: os.open(p, f, dir_fd=dfd)) as handle:
            head = handle.read(20)
        if not head.startswith((b"# v2 git bundle\n", b"# v3 git bundle\n")):
            ctx.violations.append("result.bundle is not a git bundle; discarded")
            return False
        inspect = self.env.inspect_bundle
        if inspect is None:
            return True         # only a hand-built Env (tests) has none: make_env_factory always sets one
        tid, guest = ctx.tour["id"], ctx.guest
        base_bundle = self.store.input_path(tid, guest["bundle"])
        result_bundle = os.path.join(self.store.results_dir(tid, guest["id"]), ctx.rec["id"], name)
        rebuilt = result_bundle + ".rebuilt"
        try:
            ctx.inspected = inspect(base_bundle, result_bundle, guest["base"], f"motel/{tid}/{guest['id']}", ctx.rec["id"],
                                    ctx.own_token, rebuilt)
        except Exception as exc:  # noqa: BLE001  fail closed: a bundle motel could not inspect is not handed over
            with contextlib.suppress(OSError):
                os.unlink(rebuilt)
            ctx.violations.append("result.bundle was discarded: " + (getattr(exc, "message", None) or type(exc).__name__))
            ctx.error = ctx.error or "the result bundle could not be inspected, so it was withheld"
            return False
        if ctx.inspected.get("token_found"):
            ctx.inspected = None
            ctx.violations.append("result.bundle: a commit, tree or file in it contained the own-login token; withheld")
            ctx.error = ctx.error or "the guest output contained the own-login token (secret_leak); it was withheld"
            return False
        delivered = ctx.inspected.get("delivered")
        if delivered:
            # The user gets the bundle motel rebuilt from the objects it scanned, not the file the guest wrote.
            if delivered["bytes"] > self.env.limits.max_result_bytes:
                with contextlib.suppress(OSError):
                    os.unlink(rebuilt)
                ctx.violations.append("the rebuilt result.bundle is over the size limit; withheld")
                ctx.error = ctx.error or "the result bundle is over the size limit, so it was withheld"
                return False
            os.replace(rebuilt, result_bundle)
            ctx.bundle_delivered = delivered
        claimed = (ctx.run_doc or {}).get("head")
        if claimed and claimed != ctx.inspected.get("head"):
            ctx.notes.append("run.json named a different head than the bundle holds; the bundle's own head is reported")
        return True

    def _accept_claims(self, ctx, dfd, name) -> bool:
        """RESULT.json: parsed only with a depth limit, and withheld if it carries the own-login token in any form."""
        with open(name, "rb", opener=lambda p, f: os.open(p, f, dir_fd=dfd)) as handle:
            data = handle.read(art.MAX_CLAIMS)
        try:
            value = art.loads_limited(data)
        except (ValueError, RecursionError):
            ctx.claims = None
            ctx.violations.append("RESULT.json is not valid JSON, or is nested too deeply; kept but not parsed")
            return True
        if ctx.own_token and art.contains_text(value, ctx.own_token):
            ctx.violations.append("RESULT.json contained the own-login token; withheld")
            ctx.error = ctx.error or "the guest output contained the own-login token (secret_leak); it was withheld"
            return False
        ctx.claims = value
        return True

    # ------------------------------------------------------------------ teardown
    def _decide(self, ctx: Ctx) -> str:
        run = ctx.run_doc or {}
        if ctx.cancelled:
            return "cancelled"
        if ctx.over_budget:
            return "over_budget"
        if ctx.timed_out or (not ctx.error and run.get("termination") == "timeout"):
            return "timed_out"
        if ctx.error or ctx.runner_exit not in (0, None) or run.get("exit_code") not in (0,) or run.get("termination") == "failed":
            return "failed"
        return "done"

    def _teardown(self, ctx: Ctx) -> None:
        env, store, rec = self.env, self.store, ctx.rec
        tid, aid = ctx.tour["id"], rec["id"]
        self._revoke(ctx)
        view = ctx.view
        if ctx.lease_id:
            self._set(ctx, cleanup="deleting")
            try:
                view = ctx.ctl.checkout(ctx.lease_id)
                clean = "deleted" if view.get("state") == "released" else "deletion_pending"
            except Exception as exc:  # noqa: BLE001
                clean = "deletion_pending"
                rec["cleanup_error"] = getattr(exc, "message", type(exc).__name__)
        else:
            clean = "deleted"
        ctx.final_view = view
        cost = None
        try:
            if view and view.get("cost_estimate_net_eur") is not None:
                cost = Decimal(str(view["cost_estimate_net_eur"]))
        except Exception:  # noqa: BLE001
            cost = None
        if cost is None or clean != "deleted":
            cost = max(cost or Decimal(0), ctx.reserved) if clean != "deleted" else ctx.reserved
            # unknown, or still billing: the reservation is the upper bound
        usage = self._usage(ctx)
        status = self._decide(ctx)
        lost = self._lost_listener(ctx)
        run_doc = ctx.run_doc or {}
        finished_fine = run_doc.get("termination") == "exited" and run_doc.get("exit_code") == 0
        if lost and finished_fine and status == "done":
            # The runner says the job ended cleanly with exit 0, so the lost listener did not cut it off from the model.
            ctx.notes.append(f"concierge_listener_lost: the concierge lost this attempt's listener late ({lost}), but "
                             "the runner reported a clean exit")
        elif lost and status != "cancelled":
            # The concierge restarted and could not re-open this attempt's port, so it revoked the attempt: whatever
            # the guest did after that was cut off from the model API. A cut attempt is a failure, never a retry
            # once the command has started (the retry rule above already needs `not ctx.started`).
            status, ctx.reason = "failed", "concierge_listener_lost"
            ctx.error = ctx.error or f"concierge_listener_lost: the concierge lost this attempt's listener ({lost})"
        if status == "failed" and not ctx.reason and self._budget_was_the_cause(ctx, usage):
            status = "over_budget"
        elif status == "failed" and not ctx.error and self._feature_denied(ctx):
            ctx.error = "the concierge denied a request feature the binding does not allow (motel_feature_denied)"
        rec.update(status=status, cleanup=clean, cost_eur=str(cost), usage=usage, error=ctx.error, reason=ctx.reason, pre_start=ctx.pre_start,
                   retry=ctx.retry, finished_at=iso(env.clock()))
        self._write_result(ctx, status, clean, cost, usage)
        store.save_attempt(tid, rec)

        def settle(doc):
            # Idempotent: a second teardown of the same attempt (a restarted supervisor, `tour reap` racing a
            # late worker) changes nothing. The flag is set under the same lock as the money moves.
            done = doc.setdefault("settled", [])
            if aid in done:
                return
            done.append(aid)
            b = doc["budget"]
            b["reserved_eur"] = str(max(Decimal(0), Decimal(b["reserved_eur"]) - ctx.reserved))
            b["spent_eur"] = str(Decimal(b["spent_eur"]) + cost)
            g = doc["guests"][ctx.guest["id"]]
            # The retry is decided here, in the same write that settles the attempt: a guest that will be run
            # again never shows a terminal status, so `wait` cannot report it as finished.
            # A stop (SIGTERM) before the command started is not the attempt's fault: the guest goes back to the queue
            # for the restarted supervisor, and neither this nor the restart counts against the guest's one retry.
            ctx.requeued = ctx.stopped and not ctx.started and not ctx.cancelled and not store.cancel_requested(tid)
            ctx.will_retry = (ctx.retry and status == "failed" and not ctx.cancelled and not ctx.started
                              and not store.cancel_requested(tid) and g.get("retries_used", 0) < MAX_RETRIES)
            if ctx.requeued:
                g["status"] = "queued"
            elif ctx.will_retry:
                g["retries_used"] = g.get("retries_used", 0) + 1
                g["status"] = "queued"
            else:
                g["status"] = status
                if ctx.reason:
                    g["reason"] = ctx.reason
                if ctx.saved_summary is not None:
                    g["result"] = ctx.saved_summary
            attempts = [store.load_attempt(tid, a) if a != aid else rec for a in g["attempts"]]
            g["cleanup"] = worst_cleanup(a["cleanup"] for a in attempts)
        store.update(tid, settle)
        if ctx.requeued:
            store.event(tid, "requeued_on_stop", guest=ctx.guest["id"], after=aid)
        elif ctx.will_retry:
            store.event(tid, "retry", guest=ctx.guest["id"], after=aid)
        store.event(tid, "attempt_done", guest=ctx.guest["id"], attempt=aid, status=status, cleanup=clean)
        for path in ctx.saved:
            with contextlib.suppress(OSError):
                os.unlink(path)
        ctx.own_token = None

    def _lost_listener(self, ctx: Ctx) -> str | None:
        """Why the concierge says it lost this attempt's listener (``health()["lost_listeners"]``), or None."""
        if not (ctx.rec.get("token_issued") or ctx.rec.get("token_issuing")):
            return None
        try:
            lost = self.env.concierge.health().get("lost_listeners")
        except Exception:  # noqa: BLE001  an unreachable concierge is already handled by usage and revoke
            return None
        reason = lost.get(ctx.rec["id"]) if isinstance(lost, dict) else None
        return art.clean_text(str(reason), 160) if reason else None

    @staticmethod
    def _feature_denied(ctx: Ctx) -> bool:
        """A denied feature is a failure with a reason, never an over-budget result."""
        return any(r.get("type") == "motel_feature_denied" for r in (ctx.run_doc or {}).get("concierge_refusals", []))

    @staticmethod
    def _budget_was_the_cause(ctx: Ctx, usage: dict) -> bool:
        """The concierge's own count must back it up: the guest's report alone can be wrong or invented."""
        if not usage.get("metered") or not usage.get("refused", 0):
            return False
        reported = any(r["type"] == "motel_budget_exhausted" for r in (ctx.run_doc or {}).get("concierge_refusals", []))
        spent = Decimal(str(usage.get("usd", 0))) >= Decimal(ctx.guest["budget_usd"]) * Decimal("0.99") > 0
        return reported or spent

    def _usage(self, ctx: Ctx) -> dict:
        if ctx.guest["auth"] == "own-login":
            return {"metered": False, "note": "not metered: model use is billed to your own Claude login"}
        if not (ctx.rec.get("token_issued") or ctx.rec.get("token_issuing")):
            return {"metered": True, "usd": "0", "input_tokens": 0, "output_tokens": 0, "requests": 0, "refused": 0}
        try:
            usage = dict(self.env.concierge.usage(attempt=ctx.rec["id"]))
            usage["metered"] = True
            return usage
        except Exception as exc:  # noqa: BLE001
            return {"metered": True, "error": f"usage unavailable ({type(exc).__name__})"}

    def _write_result(self, ctx: Ctx, status: str, clean: str, cost: Decimal, usage: dict) -> None:
        store, tid, g = self.store, ctx.tour["id"], ctx.guest
        run = ctx.run_doc
        files = ctx.saved_files
        bundle = next((f for f in files if f["role"] == "bundle"), None)
        artifacts = [f for f in files if f["role"] == "artifact"]
        duration = None
        if ctx.start_time is not None:
            duration = int((self.env.clock() - ctx.start_time).total_seconds())
        record = {
            "schema": RESULT_SCHEMA, "tour": tid, "guest": g["id"], "attempt": ctx.rec["id"], "status": status,
            "cleanup": clean, "auth": g["auth"], "branch": f"motel/{tid}/{g['id']}", "base": g["base"],
            "partial": ctx.partial, "error": ctx.error, "reason": ctx.reason,
            **({} if g.get("model") is None else {"model": g["model"]}),
            # What the controller measured or saved itself.
            "verified": {
                "duration_s": duration,
                "bundle": None if bundle is None else {"bytes": bundle["bytes"], "sha256": bundle["sha256"]},
                "head": (ctx.inspected or {}).get("head"),
                "changed_files_total": (ctx.inspected or {}).get("changed_total", 0),
                "changed_files": ((ctx.inspected or {}).get("changed_files") or [])[:MAX_CHANGED_IN_RECORD],
                "artifacts": [{"path": a["path"].split("/", 1)[1], "bytes": a["bytes"], "sha256": a["sha256"]} for a in artifacts],
                "cost_eur": str(cost), "usage": usage, "cabin": ctx.lease_id,
            },
            # What the runner on the guest reported in run.json (and its ssh exit status): a claim by software
            # that runs next to the agent. It decides the status, but motel did not observe it.
            "runner_reported": {
                "runner_exit": ctx.runner_exit, "exit_code": (run or {}).get("exit_code"),
                "termination": (run or {}).get("termination"),
            },
            "concierge_refusals": {"reported_by_runner": (run or {}).get("concierge_refusals", []),
                                   "counted_by_concierge": usage.get("refused")},
            "run": {"reason": (run or {}).get("reason"), "command_started": (run or {}).get("command_started"),
                    "rejected": (run or {}).get("rejected", []),
                    "detail": (run or {}).get("detail"), "stragglers_killed": (run or {}).get("stragglers_killed", 0),
                    "stragglers_left": (run or {}).get("stragglers_left", 0),
                    "pruned_out": (run or {}).get("pruned_out", 0)},
            "violations": ctx.violations[:50], "notes": ctx.notes[:20],
            "guest_claims": {"untrusted": True, "result": ctx.claims if isinstance(ctx.claims, dict) else None},
            "logs": {"stdout": any(f["role"] == "stdout" for f in files), "stderr": any(f["role"] == "stderr" for f in files),
                     "runner": bool(ctx.runner_log and os.path.exists(ctx.runner_log))},
        }
        base = os.path.join(store.results_dir(tid, g["id"], create=True), ctx.rec["id"])
        os.makedirs(base, mode=0o700, exist_ok=True)
        from motel.cabins.store import atomic_write
        atomic_write(os.path.join(base, "result.json"), json.dumps(record, sort_keys=True, indent=1))
        ctx.saved_summary = {"attempt": ctx.rec["id"], "branch": record["branch"], "exit_code": record["runner_reported"]["exit_code"],
                             "bundle": bundle is not None, "changed_files": (ctx.inspected or {}).get("changed_total", 0), "artifacts": len(artifacts),
                             "cost_eur": str(cost), "usd": usage.get("usd"), "metered": usage.get("metered"), "auth": g["auth"],
                             "partial": ctx.partial, "violations": len(ctx.violations),
                             "cabin": cabin_receipt(ctx.final_view, self.env.clock()) if ctx.final_view else None}
