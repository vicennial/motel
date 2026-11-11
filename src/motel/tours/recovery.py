"""Reconcile attempts that a dead supervisor left behind, and finish cleanups that did not finish.

Used by a restarted supervisor and by ``tour reap``. A recovered attempt is revoked, its cabin is checked out and
its cost settled. If its command had not started it counts as a failure before the start (a retry may follow);
if it had started it is never run again: a bounded collect is tried once and the attempt is ``failed``.
"""

from __future__ import annotations

import contextlib
import os
from decimal import Decimal

from motel.cabins.errors import Fail
from motel.tours.pipeline import KEY_FILE, MAX_RETRIES, AttemptRunner, Ctx
from motel.tours.state import TERMINAL, worst_cleanup
from motel.tours.transport import Target


def _find_lease(ctl, rec) -> str | None:
    if rec.get("lease_id"):
        return rec["lease_id"]
    try:
        found = [r for r in ctl.store.all() if str(r.get("lease_id", "")).startswith(rec["id"] + "-")]
    except Exception:  # noqa: BLE001
        return None
    return found[0]["lease_id"] if found else None


def _target(ctl, lease_id) -> Target | None:
    try:
        lease = ctl.store.load(lease_id)
        address = (lease.get("access") or {}).get("address")
        if not address or lease.get("state") in ("released", "cleanup_pending"):
            return None
        base = ctl.store.key_path(lease_id)
        return Target(lease_id=lease_id, alias=lease["name"], address=address, key_path=os.path.join(base, KEY_FILE),
                      known_hosts=os.path.join(base, "known_hosts"))
    except Exception:  # noqa: BLE001
        return None


def recover_attempt(runner: AttemptRunner, tour: dict, rec: dict, *, reason: str = "the supervisor stopped") -> str:
    """Finish one non-terminal attempt. Returns its final execution status."""
    env = runner.env
    guest = next(g for g in tour["spec"]["guests"] if g["id"] == rec["guest"])
    ctx = Ctx(tour=tour, guest=guest, rec=rec, owner=tour["owner"])
    ctx.ctl = env.controller_factory(tour["owner"], cleanup_only=True)
    ctx.lease_id = _find_lease(ctx.ctl, rec)
    ctx.reserved = Decimal(rec.get("reserved_eur") or 0)
    if rec.get("auth") == "own-login":
        # The runner log of a supervisor that died holds console output that was never redacted (the token is not
        # known here and must not be read back): delete it rather than keep text that may carry the token.
        with contextlib.suppress(OSError):
            os.unlink(os.path.join(env.store.results_dir(tour["id"], rec["guest"]), rec["id"], "runner.log"))
    ctx.started = bool(rec.get("command_started_at"))
    ctx.token = "issued" if (rec.get("token_issued") or rec.get("token_issuing")) else None   # revoke by attempt id
    ctx.cancelled = env.store.cancel_requested(tour["id"])
    ctx.pre_start = not ctx.started
    ctx.retry = ctx.pre_start and not ctx.cancelled
    ctx.error = None if ctx.cancelled else reason
    runner._revoke(ctx)
    if ctx.started and ctx.lease_id:
        ctx.target = _target(ctx.ctl, ctx.lease_id)
        if ctx.target is not None:
            runner._phase_collect(ctx, bounded=True)        # stops the guest's processes first
    runner._teardown(ctx)
    return rec["status"]


def requeue_failed_before_start(runner: AttemptRunner, tour_id: str) -> list:
    """Put guests back in the queue whose last attempt failed before the engine launched the runner, when a retry
    was due and not yet used (an older supervisor may have died between settling and re-queueing). Returns guest ids."""
    store = runner.store
    doc, queued = store.load(tour_id), []
    if store.cancel_requested(tour_id):
        return queued
    last = {}
    for rec in store.attempts(tour_id):
        last[rec["guest"]] = rec
    for gid, g in doc["guests"].items():
        rec = last.get(gid)
        if g["status"] != "failed" or rec is None or rec["status"] != "failed" or g.get("retries_used", 0) >= MAX_RETRIES:
            continue
        if rec.get("command_started_at") or not rec.get("retry"):
            continue
        queued.append(gid)

    def apply(d):
        for gid in queued:
            d["guests"][gid]["status"] = "queued"
            d["guests"][gid]["retries_used"] = d["guests"][gid].get("retries_used", 0) + 1
    if queued:
        store.update(tour_id, apply)
        for gid in queued:
            store.event(tour_id, "retry", guest=gid, after=last[gid]["id"])
    return queued


def recover_tour(runner: AttemptRunner, tour_id: str, *, reason: str = "the supervisor stopped") -> list:
    """Recover every non-terminal attempt of a tour. Returns (attempt, status) pairs."""
    store = runner.env.store
    done = []
    for rec in store.attempts(tour_id):
        if rec["status"] in TERMINAL:
            continue
        done.append((rec["id"], recover_attempt(runner, store.load(tour_id), rec, reason=reason)))
    return done


def retry_cleanups(runner: AttemptRunner, tour_id: str) -> list:
    """Retry deletions that are still pending and revocations that failed. Returns (attempt, cleanup) pairs."""
    env, store = runner.env, runner.store
    tour = store.load(tour_id)
    out = []
    for rec in store.attempts(tour_id):
        if rec["status"] not in TERMINAL:
            continue
        if rec.get("revoke_pending"):
            try:
                env.concierge.revoke(attempt=rec["id"])
                rec["revoke_pending"] = False
            except Exception as exc:  # noqa: BLE001
                if getattr(exc, "kind", None) == "not_found":
                    rec["revoke_pending"] = False      # the attempt is already gone from the concierge: do not retry
        if rec["cleanup"] != "deleted":
            ctl = env.controller_factory(tour["owner"], cleanup_only=True)
            lease = _find_lease(ctl, rec)
            try:
                view = ctl.checkout(lease) if lease else {"state": "released"}
                rec["cleanup"] = "deleted" if view.get("state") == "released" else "deletion_pending"
            except Fail:
                rec["cleanup"] = "deletion_pending"
            except Exception:  # noqa: BLE001
                rec["cleanup"] = "deletion_pending"
        store.save_attempt(tour_id, rec)
        out.append((rec["id"], rec["cleanup"]))

    def apply(doc):
        for gid, g in doc["guests"].items():
            attempts = [r for r in store.attempts(tour_id) if r["guest"] == gid]
            if attempts:
                g["cleanup"] = worst_cleanup(r["cleanup"] for r in attempts)
    store.update(tour_id, apply)
    return out
