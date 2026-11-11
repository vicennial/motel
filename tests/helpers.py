"""Shared test fixture: a temp root, config, fake provider, fake system operations and a fake clock."""

from __future__ import annotations

import io
import json
import os
import tempfile
from pathlib import Path

from motel import desk
from motel.config import Config, Paths
from motel.providers.fake import FakeProvider
from motel.rooms import Caller, Ctx, Rooms
from motel.state import Store, atomic_write
from motel.testing import FakeClock, FakeSysOps

SERVER = "1000001"

BASE_TOML = """
[host]
provider = "hetzner"
allow_users = ["alice", "@agents"]

[rooms]
mount_root = "/mnt/motel"
price_gb_month_eur = 0.05

[rooms.caps]
max_floors = 3
max_floor_gb = 500
max_total_gb = 1000
monthly_eur = 30
"""


def toml_with(**overrides) -> str:
    """Replace ``key = value`` lines of BASE_TOML (keys unique enough by name)."""
    text = BASE_TOML
    for key, value in overrides.items():
        lines = []
        hit = False
        for line in text.splitlines():
            if line.split("=")[0].strip() == key:
                line, hit = f"{key} = {value}", True
            lines.append(line)
        if not hit:
            lines.insert(lines.index("[rooms]") + 1, f"{key} = {value}")
        text = "\n".join(lines)
    return text


class Fixture:
    def __init__(self, toml: str = BASE_TOML, never_touch=(), uid: int | None = None, receipt: bool = True):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.paths = Paths(root=str(self.root))
        os.makedirs(self.paths.config_dir)
        Path(self.paths.config_file).write_text(toml)
        self.clock = FakeClock()
        self.provider = FakeProvider(server_id=SERVER, clock=self.clock)
        self.sysops = FakeSysOps()
        receipt_doc = {"schema": "motel/v1", "provider": "hetzner", "server_id": SERVER, "location": "fsn1",
                   "setup_at": "2026-01-01T00:00:00Z", "never_touch": [{"id": str(v)} for v in never_touch]}
        if receipt:
            atomic_write(self.paths.receipt_file, json.dumps(receipt_doc), 0o644)
        self.uid = os.getuid() if uid is None else uid
        self.caller = Caller(uid=self.uid, gid=self.uid, name="alice")
        self.cfg = Config.load(self.paths.config_file)
        self.ctx = Ctx(cfg=self.cfg, receipt=desk.Receipt.load(self.paths.receipt_file) if receipt else None, provider=self.provider,
                       sysops=self.sysops, store=Store(self.paths.state_dir, self.clock, SERVER), paths=self.paths,
                       clock=self.clock)
        self.rooms = Rooms(self.ctx) if receipt else None

    def close(self):
        self._tmp.cleanup()

    # -- conveniences
    def checkin(self, size=10, hours=6, task="job", suite=False, caller=None):
        from datetime import timedelta
        return self.rooms.checkin(caller or self.caller, size, timedelta(hours=hours), task, suite)

    def later(self, **kw):
        self.clock.advance(**kw)

    def desk(self, *argv, caller=None, groups=()):
        out, err = io.StringIO(), io.StringIO()
        code = desk.run(list(argv), paths=self.paths, caller=caller or self.caller,
                        make_provider=lambda cfg, paths: self.provider, sysops=self.sysops, clock=self.clock,
                        out=out, err=err, groups_fn=lambda c: set(groups))
        return code, out.getvalue(), err.getvalue()

    def floor_vols(self):
        return {fid: f for fid, f in self.provider.volumes.items()}
