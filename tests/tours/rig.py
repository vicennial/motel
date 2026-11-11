"""A whole engine on fakes: real cabins Controller on the Hetzner fake, real state/pipeline/supervisor/service,
doubles for the concierge and the guest (see doubles.py). No root, no network, no ssh."""
from __future__ import annotations

import hashlib
import os

import doubles
from cabin_fakes import World, fake_keygen
from doubles import FakeTransport, Log, LoggedConcierge

from motel.cabins import controller as ctl_mod
from motel.cabins.desk import Caller
from motel.tours import deps, ownlogin, spec
from motel.tours.config import Limits
from motel.tours.errors import Broken
from motel.tours.pipeline import AttemptRunner, Env
from motel.tours.service import Service
from motel.tours.slots import SlotPool
from motel.tours.state import TourStore
from motel.tours.supervisor import Supervisor

BASE = "b" * 40


class SyncExecutor:
    """Runs submitted guests one after another in the calling thread (deterministic tests)."""

    def __init__(self, max_workers=1):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def submit(self, fn, *args):
        from concurrent.futures import Future
        future = Future()
        try:
            future.set_result(fn(*args))
        except BaseException as exc:  # noqa: BLE001
            future.set_exception(exc)
        return future


def file_block(text: str, path: str) -> str:
    """The content of one write_files entry of the guest part's cloud-init extras (gz+b64 entries are decoded)."""
    import base64
    import gzip
    import re
    packed = re.search(r"- path: " + re.escape(path) + r"\n    permissions: '\d+'\n    encoding: gz\+b64\n    content: (\S+)\n", text)
    if packed:
        return gzip.decompress(base64.b64decode(packed.group(1))).decode()
    plain = re.search(r"- path: " + re.escape(path) + r"\n(?:    [^\n]*\n)*?    content: \|\n((?:      [^\n]*\n|\n)+)", text)
    assert plain, path
    return "\n".join(line[6:] for line in plain.group(1).splitlines()) + "\n"


class Rig:
    def __init__(self, tmp_path, monkeypatch, **limit_over):
        self.tmp = tmp_path
        self.log = Log()
        self.world = World(str(tmp_path / "cabins"))
        self.clock = self.world.clock
        self.store = TourStore(str(tmp_path / "tours"), clock=self.clock)
        values = dict(enabled=True, bindings=("anthropic",), egress_allow=("github.com", "pypi.org"), max_concurrent_guests=4,
                      own_login_enabled=False, own_login_token_file=str(tmp_path / "own-login" / "claude.token"))
        values.update(limit_over)
        self.limits = Limits(**values)
        self.concierge = LoggedConcierge(self.log, clock=self.clock)
        self.transport = FakeTransport(self.log)
        self.extras = []
        self.launched = []
        self.inspected = []
        self.inspect_tokens = []
        self.inspect_token_found = False
        self.inspect_error = None
        self.unit_is_active = False
        self.render_extras = []
        self.render_sizes = []
        self.token_file = self.limits.own_login_token_file
        self.alice = Caller(uid=os.getuid(), gid=os.getgid(), name="alice")
        self.bob = Caller(uid=os.getuid() + 1, gid=os.getgid(), name="bob")
        self.root = Caller(uid=0, gid=0, name="root", operator=True)

        original = ctl_mod.render_user_data

        def render(*args, extras=None, **kw):       # the real renderer; we only record what it was given
            self.render_extras.append(extras)
            text = original(*args, extras=extras, **kw)
            self.render_sizes.append(len(text.encode()))
            return text
        monkeypatch.setattr(ctl_mod, "render_user_data", render)

        def userdata_extras(**kw):
            self.extras.append(kw)
            extras = deps.userdata_extras(**kw)          # the guest part's real egress profile
            self.world.guest.forward_port = ctl_mod.forward_port_of(extras)    # what a correct guest agent reports
            return extras

        slots = SlotPool(str(tmp_path / "slots"), self.limits.max_concurrent_guests, sleep=lambda s: None)
        own = SlotPool(str(tmp_path / "own-slots"), self.limits.own_login_max_concurrent, sleep=lambda s: None)
        self.env = Env(
            store=self.store, limits=self.limits, cabin_cfg=self.world.cfg,
            controller_factory=lambda owner, cleanup_only=False: self.world.controller(uid=owner["uid"]),
            concierge=self.concierge, transport=self.transport, keygen=fake_keygen, extras=userdata_extras,
            runner_files=lambda: {"runner.py": b"print('runner')\n", "adapters.py": b"# adapters\n"}, token_reader=lambda: ownlogin.read_token(self.token_file),
            slots=slots, own_slots=own, clock=self.clock, inspect_bundle=self.inspect)
        self.service = Service(
            store=self.store, limits=self.limits, cabin_cfg=self.world.cfg, clock=self.clock, launcher=self.launch,
            unit_active=lambda tid: self.unit_is_active, env_factory=lambda: self.env,
            concierge_factory=lambda: self.concierge, sleep=self.clock.sleep)

    def set_limits(self, **over):
        self.limits = self.limits.__class__(**{**self.limits.__dict__, **over})
        self.service.limits = self.env.limits = self.limits
        return self.limits

    def inspect(self, base_bundle, result_bundle, base_sha, branch, attempt, token=None, deliver_to=None):
        """Stands in for bundle.inspect_result (the fake guests' bundles are not real git bundles)."""
        self.inspected.append((branch, base_sha))
        self.inspect_tokens.append(token)
        if self.inspect_error:
            raise Broken(self.inspect_error)
        return {"head": "b" * 40, "changed_files": ["src/x.py"], "changed_total": 1, "ahead": True,
                "token_found": self.inspect_token_found}

    def launch(self, tour_id):
        self.launched.append(tour_id)
        return "motel-tour-" + tour_id

    # -- building a tour
    def wire(self, guests=None, name="rig-tour", **defaults):
        guests = guests if guests is not None else [{"id": "fix-auth", "prompt": "Fix the auth tests.", "secrets": ["anthropic"],
                                                     "budget_usd": 0.5}]
        parsed = spec.parse({"tour": {"name": name}, "defaults": defaults, "guests": guests})
        inputs = {}
        src = self.tmp / "src"
        src.mkdir(exist_ok=True)
        path = src / "base.bundle"
        data = doubles.bundle_bytes(b"base-history")
        path.write_bytes(data)
        key = hashlib.sha256(data).hexdigest()
        inputs[key[:16]] = {"kind": "bundle", "path": str(path), "sha256": key}
        for guest in parsed.guests:
            guest.base, guest.bundle = BASE, key[:16]
        return {"spec": parsed.to_wire(), "inputs": inputs}

    def start(self, guests=None, caller=None, **defaults) -> str:
        reply = self.service.start(caller or self.alice, self.wire(guests, **defaults))
        return reply["tour"]["id"]

    def supervise(self, tour_id):
        return Supervisor(self.env, tour_id, executor_factory=SyncExecutor).run()

    def run_tour(self, guests=None, **defaults) -> dict:
        tid = self.start(guests, **defaults)
        self.supervise(tid)
        return self.store.load(tid)

    def attempts(self, tid):
        return self.store.attempts(tid)

    def fail_deletes(self, count=1000):
        """Make every server DELETE of the cloud fake fail (``count`` times in total)."""
        from cabin_fakes import ApiError
        plan = [ApiError(503, "unavailable")] * count

        class AnyDelete(dict):
            def get(self, key, default=None):
                return plan if key[0] == "DELETE" and key[1].startswith("/servers/") else default
        self.world.hz.fail = AnyDelete()

    def heal(self):
        self.world.hz.fail = {}

    def result_record(self, tid, guest):
        import json
        doc = self.store.load(tid)
        attempt = doc["guests"][guest]["result"]["attempt"]
        with open(os.path.join(self.store.results_dir(tid, guest), attempt, "result.json"), encoding="utf-8") as handle:
            return json.load(handle)

    def servers_left(self):
        return list(self.world.hz.servers)

    def tree_text(self) -> str:
        out = []
        for base in (str(self.tmp / "tours"), str(self.tmp / "cabins")):
            for root, _dirs, files in os.walk(base):
                for name in files:
                    with open(os.path.join(root, name), "rb") as handle:
                        out.append(handle.read().decode("utf-8", "replace"))
        return "\n".join(out)

    def runner(self):
        return AttemptRunner(self.env)


def make(tmp_path, monkeypatch, **over):
    return Rig(tmp_path, monkeypatch, **over)


