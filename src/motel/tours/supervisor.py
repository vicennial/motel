"""The supervisor: ``motel-desk tour supervise <id>``, one per tour, a transient systemd unit ``motel-tour-<id>``.

It runs the guests of one tour with at most ``max_concurrent_guests`` at a time (host-wide, through the slot
pool), retries a guest once when an attempt failed before its command started, and ends the tour. If it dies,
``tour reap`` starts it again; on start it first recovers whatever the previous supervisor left half done.
"""

from __future__ import annotations

import contextlib
import os
import signal
from concurrent.futures import ThreadPoolExecutor

from motel.cabins.util import iso
from motel.tours import recovery
from motel.tours.pipeline import AttemptRunner, Env
from motel.tours.state import TERMINAL


@contextlib.contextmanager
def stop_on_sigterm(event, signum=signal.SIGTERM):
    """While active, a SIGTERM (a stopping systemd unit, a reboot) sets ``event`` instead of killing the process: the
    running attempts see it, revoke, collect what they can and delete their cabins, and the supervisor returns without
    finishing the tour, so a restarted supervisor (or `tour reap`) carries on. Outside the main thread it does nothing."""
    try:
        previous = signal.signal(signum, lambda _n, _f: event.set())
    except ValueError:
        yield
        return
    try:
        yield
    finally:
        signal.signal(signum, previous)


class Supervisor:
    def __init__(self, env: Env, tour_id: str, *, executor_factory=ThreadPoolExecutor, runner=None):
        self.env, self.tour_id = env, tour_id
        self.store = env.store
        self.runner = runner or AttemptRunner(env)
        self.executor_factory = executor_factory

    def run(self) -> dict:
        store, tid = self.store, self.tour_id

        def mark(doc):
            if doc["status"] in ("queued", "running"):
                doc["status"] = "running"
            doc["supervisor"]["pid"] = os.getpid()
            doc["supervisor"]["stopped"] = None
            doc["supervisor"]["last_seen"] = iso(self.env.clock())
        tour = store.update(tid, mark)
        if tour["status"] in TERMINAL_TOUR:
            return tour
        recovery.recover_tour(self.runner, tid)
        if self.env.inspect_root:       # the previous supervisor may have died inside an inspection: nothing of it runs now
            from motel.tours import bundle
            bundle.remove_scratch_of(self.env.inspect_root, [a["id"] for a in store.attempts(tid)])
        recovery.requeue_failed_before_start(self.runner, tid)
        pending = [gid for gid, g in store.load(tid)["guests"].items() if g["status"] not in TERMINAL]
        workers = max(1, min(self.env.limits.max_concurrent_guests, len(pending) or 1))
        with self.executor_factory(max_workers=workers) as pool:
            futures = [pool.submit(self._guest, gid) for gid in pending]
            for future in futures:
                future.result()
        if self.env.stop.is_set():          # told to stop: leave the tour running for the restarted supervisor
            def stopped(doc):
                doc["supervisor"]["stopped"] = iso(self.env.clock())      # a deliberate stop: reap restarts it for free
            return store.update(tid, stopped)
        return self.finish()

    def _guest(self, gid: str) -> None:
        store, tid = self.store, self.tour_id
        while True:
            doc = store.load(tid)
            guest = doc["guests"][gid]
            if guest["status"] in TERMINAL:
                return
            if store.cancel_requested(tid):
                def cancel(d):
                    if d["guests"][gid]["status"] not in TERMINAL:
                        d["guests"][gid]["status"] = "cancelled"
                store.update(tid, cancel)
                return
            if self.env.stop.is_set():
                return
            # The attempt decides its own retry when it settles (guest back to "queued"); loop until terminal.
            self.runner.run(tid, gid, len(guest["attempts"]) + 1)

    def finish(self) -> dict:
        store, tid = self.store, self.tour_id

        def end(doc):
            states = [g["status"] for g in doc["guests"].values()]
            cancelled = store.cancel_requested(tid)
            doc["status"] = "cancelled" if cancelled else ("done" if all(s == "done" for s in states) else "failed")
            doc["finished_at"] = iso(self.env.clock())
        doc = store.update(tid, end)
        store.drop_inputs(tid)              # the tour is over: nothing needs the staged copies of the caller's files
        store.event(tid, "finished", status=doc["status"])
        return doc


TERMINAL_TOUR = ("done", "failed", "cancelled")
assert set(TERMINAL_TOUR) <= set(TERMINAL)
