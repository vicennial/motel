"""Opt-in per-user warm cabin pooling (``motel cabin run --pool``, ``motel cabin pool ls|drain``)."""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
from datetime import UTC, datetime, timedelta

import pytest
from cabin_fakes import pubkey

from motel.cabins import cli
from motel.cabins.pool import PoolStore, pool_task_name, validate_pool_name

NOW = datetime(2026, 11, 8, 12, 10, 0, tzinfo=UTC)


def make_view(
    lease_id: str,
    *,
    state: str = "active",
    server_type: str = "cpx32",
    image_type: str = "system",
    image_name: str = "ubuntu-24.04",
    image_id: int = 1001,
    disk_gb: int | None = None,
    sealed: bool = False,
    expires_at: str = "2026-11-08T13:00:00Z",
    release_opens: str | None = "2026-11-08T12:44:00Z",
    release_closes: str | None = "2026-11-08T12:54:00Z",
    paid_until: str | None = "2026-11-08T13:00:00Z",
    server_present: bool | None = True,
) -> dict:
    name = "motel-" + lease_id
    offer = {
        "server_type": server_type,
        "location": "fsn1",
        "image_id": image_id,
        "image_type": image_type,
        "image_name": image_name,
        "total_hourly_net_eur": "0.0318",
    }
    billing = {
        "granularity": "started_hour",
        "billed_hours": 1,
        "paid_until": paid_until,
        "release_window": (
            {"opens": release_opens, "closes": release_closes}
            if release_opens is not None
            else None
        ),
        "source": "provider",
    }
    view = {
        "lease_id": lease_id,
        "name": name,
        "state": state,
        "address": "203.0.113.7",
        "host_key": pubkey("H"),
        "offer": offer,
        "disk": {"size_gb": disk_gb, "guest_path": "/mnt/cabin-disk", "link": "~/disk"} if disk_gb else None,
        "created_at": "2026-11-08T12:00:00Z",
        "expires_at": expires_at,
        "ssh": {
            "user": "cabin",
            "host": "203.0.113.7",
            "port": 22,
            "host_key_alias": name,
            "host_key": pubkey("H"),
        },
        "access": {"mode": "ssh-allowlist"},
        "billing": billing,
        "sealed": sealed,
        "admission": {"sealed_at": "2026-11-08T12:09:00Z"} if sealed else None,
        "time_left_s": 3000,
    }
    if server_present is not None:
        view["server_present"] = server_present
    return view


class Proc:
    def __init__(self, rc: int = 0, stdout: bytes = b""):
        self.returncode, self.stdout = rc, stdout


class PoolRunner:
    """Fake process runner supporting multiple pooled cabins, live status mutations, and checkouts."""

    def __init__(
        self,
        *,
        default_type: str = "cpx32",
        default_image: str = "ubuntu-24.04",
        baked_images: dict[str, int] | None = None,
        tree: list[str] | None = None,
        ssh_rc: int = 0,
        rsync_rc: int = 0,
    ):
        self.default_type = default_type
        self.default_image = default_image
        self.baked_images = baked_images or {"omnigent": 4711, "rust-ci": 4712}
        self.tree = tree or ["a.py", "src/b.py"]
        self.ssh_rc = ssh_rc
        self.rsync_rc = rsync_rc
        self.calls: list[tuple[list[str], dict]] = []
        self.cabins: dict[str, dict] = {}
        self.next_seq = 1
        self.checkin_rc = 0
        self.checkin_doc: dict | None = None
        self.checkout_overrides: dict[str, tuple[int, dict]] = {}
        self.on_ssh = None

    def _pair_args(self, tail: list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        i = 0
        while i < len(tail):
            if tail[i].startswith("--") and tail[i] != "--json" and i + 1 < len(tail):
                out[tail[i]] = tail[i + 1]
                i += 2
            else:
                i += 1
        return out

    def __call__(self, argv: list[str], **kw):
        self.calls.append((argv, kw))
        if argv[:2] == ["/usr/bin/sudo", "-n"]:
            verb = argv[4]
            if verb == "checkin":
                if self.checkin_rc:
                    doc = self.checkin_doc or {"ok": False, "error": "no vacancy", "hint": "motel cabin ls"}
                    return Proc(self.checkin_rc, json.dumps(doc).encode())
                opts = self._pair_args(argv[5:])
                task = opts["--task"]
                server_type = opts.get("--type", self.default_type)
                disk_gb = int(opts["--disk"].rstrip("gGbB")) if "--disk" in opts else None
                if "--image" in opts:
                    req = opts["--image"]
                    if req.isdigit():
                        img_id = int(req)
                        img_name = next((k for k, v in self.baked_images.items() if v == img_id), f"img-{req}")
                    else:
                        img_name = req
                        img_id = self.baked_images.get(req, 9000)
                    img_type = "snapshot"
                else:
                    img_type = "system"
                    img_name = self.default_image
                    img_id = 1001
                lease_id = f"{task}-{self.next_seq:012x}"
                self.next_seq += 1
                view = make_view(
                    lease_id,
                    server_type=server_type,
                    image_type=img_type,
                    image_name=img_name,
                    image_id=img_id,
                    disk_gb=disk_gb,
                )
                self.cabins[lease_id] = view
                return Proc(0, json.dumps({"schema": "motel/v1", "ok": True, "cabin": view}).encode())
            if verb == "status":
                lease_id = argv[5]
                view = self.cabins.get(lease_id)
                if view is None:
                    doc = {"schema": "motel/v1", "ok": False, "error": f"no cabin {lease_id}",
                           "hint": "motel cabin ls", "code": 7}
                    return Proc(7, json.dumps(doc).encode())
                return Proc(0, json.dumps({"schema": "motel/v1", "ok": True, "cabin": view,
                                           "provider": {"inventory": "complete", "error": None}}).encode())
            if verb == "checkout":
                lease_id = argv[5]
                if lease_id in self.checkout_overrides:
                    rc, doc = self.checkout_overrides[lease_id]
                    return Proc(rc, json.dumps(doc).encode())
                view = self.cabins.get(lease_id)
                if view is None:
                    doc = {"schema": "motel/v1", "ok": False, "error": f"no cabin {lease_id}",
                           "hint": "motel cabin ls", "code": 7}
                    return Proc(7, json.dumps(doc).encode())
                done = dict(view, state="released", released_at="2026-11-08T12:20:00Z",
                            cost_estimate_net_eur="0.0318")
                self.cabins[lease_id] = done
                return Proc(0, json.dumps({"schema": "motel/v1", "ok": True, "cabin": done}).encode())
        if argv[0] == "git":
            return Proc(0, b"\0".join(n.encode() for n in self.tree) + b"\0")
        if argv[0] == "/usr/bin/rsync":
            return Proc(self.rsync_rc)
        if argv[0] == "/usr/bin/ssh":
            if self.on_ssh is not None:
                return Proc(self.on_ssh(argv, kw))
            return Proc(self.ssh_rc)
        raise AssertionError(f"unexpected command: {argv}")

    def desk_verbs(self) -> list[str]:
        return [a[4] for a, _ in self.calls if a[:2] == ["/usr/bin/sudo", "-n"]]

    def by(self, program: str) -> list[tuple[list[str], dict]]:
        return [(a, k) for a, k in self.calls if a[0] == program]


def run_cli(tmp_path, argv, runner=None, *, now=NOW, config_path=None):
    runner = runner or PoolRunner()
    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)
    for name in runner.tree:
        target = work / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x")

    def make_key(directory):
        with open(os.path.join(directory, "id_ed25519"), "w", encoding="utf-8") as handle:
            handle.write("PRIVATE")
        return pubkey("U")

    out, err = io.StringIO(), io.StringIO()
    cabins_root = str(tmp_path / "motel" / "cabins")
    rc = cli.main(
        argv,
        runner=runner,
        root=cabins_root,
        make_key=make_key,
        out=out,
        err=err,
        cwd=str(work),
        desk_exists=lambda: True,
        clock=lambda: now,
        config_path=config_path,
    )
    return rc, out.getvalue(), err.getvalue(), runner


def pool_store(tmp_path) -> PoolStore:
    return PoolStore(str(tmp_path / "motel" / "pools"))


# ---------------------------------------------------------------------------- validation & mutual exclusion


def test_pool_name_validation_and_task_derivation():
    for good in ("a", "ci", "my-pool-1", "a" * 32, "a-"):
        assert validate_pool_name(good) == good
        task = pool_task_name(good)
        assert task.startswith("pool-") and len(task) <= 32 and task[-1].isalnum()
    for bad in ("", "CI", "-ci", "ci_1", "ci.1", "../etc", "a" * 33, "bad name"):
        with pytest.raises(cli.Usage):
            validate_pool_name(bad)


@pytest.mark.parametrize("argv", [
    ["run", "--pool", "ci", "--cabin", "swift-gull-0123456789ab", "--", "true"],
    ["run", "--pool", "ci", "--keep", "--", "true"],
    ["run", "--pool", "ci", "--task", "custom", "--", "true"],
    ["run", "--pool", "Bad_Pool", "--", "true"],
    ["run", "--pool", "", "--", "true"],
    ["run", "--pool", "ci"],
    ["pool", "ls", "Bad_Pool"],
    ["pool", "drain", "../evil"],
])
def test_pool_rejects_invalid_names_and_conflicting_flags(tmp_path, argv):
    rc, _out, err, runner = run_cli(tmp_path, argv)
    assert rc == 2 and "motel:" in err and runner.calls == []


# ---------------------------------------------------------------------------- cold miss -> warm hit & rsync --delete


def test_cold_miss_then_warm_hit_reuses_cabin_without_checkout_and_sets_modes(tmp_path):
    runner = PoolRunner()
    # First run: cold miss
    rc1, out1, err1, _ = run_cli(tmp_path, ["run", "--pool", "ci", "--json", "--", "pytest"], runner)
    assert rc1 == 0
    doc1 = json.loads(out1.strip().splitlines()[-1])
    lease1 = doc1["cabin"]
    assert lease1.startswith("pool-ci-")
    assert doc1["pool"] == "ci" and doc1["pool_hit"] is False
    assert doc1["kept"] is True and doc1["released"] is False
    assert "cold miss" in err1
    assert runner.desk_verbs() == ["checkin"]
    # Default TTL is 1h and task is pool-ci
    checkin_argv = runner.by("/usr/bin/sudo")[0][0]
    assert checkin_argv[checkin_argv.index("--task") + 1] == "pool-ci"
    assert checkin_argv[checkin_argv.index("--for") + 1] == "1h"

    # Strict 0700 dirs and 0600 files under per-user pools dir
    pools_dir = tmp_path / "motel" / "pools"
    ci_dir = pools_dir / "ci"
    assert stat.S_IMODE(pools_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(ci_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((ci_dir / "index.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((ci_dir / f"{lease1}.lock").stat().st_mode) == 0o600

    # Second run: warm hit
    before = len(runner.calls)
    rc2, out2, err2, _ = run_cli(tmp_path, ["run", "--pool", "ci", "--json", "--", "pytest", "-k", "fast"], runner)
    assert rc2 == 0
    doc2 = json.loads(out2.strip().splitlines()[-1])
    assert doc2["cabin"] == lease1
    assert doc2["pool"] == "ci" and doc2["pool_hit"] is True
    assert doc2["kept"] is True and doc2["released"] is False
    assert f"motel: pool ci warm hit {lease1}" in err2
    new_verbs = [a[4] for a, _ in runner.calls[before:] if a[:2] == ["/usr/bin/sudo", "-n"]]
    assert new_verbs == ["status"]  # verified live, no checkin, no checkout

    # Warm hit uses rsync --delete into ~/work
    rsync_argv, rsync_kw = runner.by("/usr/bin/rsync")[-1]
    assert "--delete" in rsync_argv and "--delete-excluded" in rsync_argv
    assert "--include-from=-" in rsync_argv and "--exclude=*" in rsync_argv
    assert rsync_argv[-2:] == ["./", f"motel-{lease1}:work/"]
    assert b"/a.py\0" in rsync_kw["input"] and b"/src/\0" in rsync_kw["input"] and b"/src/b.py\0" in rsync_kw["input"]


def test_real_rsync_delete_removes_stale_files_and_keeps_ignored_out(tmp_path):
    """Exercise the exact rsync --delete argv produced by Cli.sync(delete=True) against real /usr/bin/rsync."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "keep.py").write_text("new-content-123")
    (src / "sub").mkdir()
    (src / "sub" / "mod.py").write_text("mod")
    (src / "secret.env").write_text("PRIVATE=1")
    (dst / "keep.py").write_text("old")
    (dst / "stale_from_prev_job.txt").write_text("stale")
    (dst / "old_dir").mkdir()
    (dst / "old_dir" / "leftover.o").write_text("obj")

    rules = cli._rsync_include_rules(["keep.py", "sub/mod.py"])
    payload = b"".join(r.encode("utf-8") + b"\0" for r in rules)
    proc = subprocess.run(
        ["/usr/bin/rsync", "-a", "--delete", "--delete-excluded", "--from0", "--include-from=-", "--exclude=*",
         "./", str(dst) + "/"],
        input=payload,
        cwd=src,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr.decode()
    assert sorted(str(p.relative_to(dst)) for p in dst.rglob("*")) == ["keep.py", "sub", "sub/mod.py"]
    assert (dst / "keep.py").read_text() == "new-content-123"


def test_warm_hit_preserves_nonzero_command_exit_code_and_releases_lock(tmp_path):
    runner = PoolRunner()
    assert run_cli(tmp_path, ["run", "--pool", "ci", "--", "true"], runner)[0] == 0
    lease1 = next(iter(runner.cabins))
    runner.ssh_rc = 7
    rc, out, _err, _ = run_cli(tmp_path, ["run", "--pool", "ci", "--json", "--", "false"], runner)
    assert rc == 7
    doc = json.loads(out.strip().splitlines()[-1])
    assert doc["ok"] is False and doc["exit_code"] == 7 and doc["pool_hit"] is True and doc["cabin"] == lease1
    assert not pool_store(tmp_path).is_cabin_locked("ci", lease1)


# ---------------------------------------------------------------------------- concurrency


def test_busy_locked_cabin_spawns_second_cabin(tmp_path):
    runner = PoolRunner()
    assert run_cli(tmp_path, ["run", "--pool", "ci", "--", "true"], runner)[0] == 0
    lease1 = next(iter(runner.cabins))

    store = pool_store(tmp_path)
    held = store.try_lock_cabin("ci", lease1)
    assert held is not None
    try:
        rc2, out2, err2, _ = run_cli(tmp_path, ["run", "--pool", "ci", "--json", "--", "true"], runner)
        assert rc2 == 0
        doc2 = json.loads(out2.strip().splitlines()[-1])
        lease2 = doc2["cabin"]
        assert lease2 != lease1 and doc2["pool_hit"] is False and "cold miss" in err2
        assert len(store.load_entries("ci")) == 2
    finally:
        held.release()

    # Once first cabin is unlocked, next run hits it warm (oldest-first packing).
    rc3, out3, _err3, _ = run_cli(tmp_path, ["run", "--pool", "ci", "--json", "--", "true"], runner)
    assert rc3 == 0
    doc3 = json.loads(out3.strip().splitlines()[-1])
    assert doc3["pool_hit"] is True and doc3["cabin"] == lease1


# ---------------------------------------------------------------------------- sealed / expiring / released pruning


@pytest.mark.parametrize("reason,mutate,run_now", [
    ("sealed", lambda v: v.update(sealed=True), NOW),
    ("released", lambda v: v.update(state="released"), NOW),
    ("cleanup_pending", lambda v: v.update(state="cleanup_pending"), NOW),
    ("not_found", None, NOW),
    ("expiring_ttl", lambda v: v.update(expires_at="2026-11-08T12:17:00Z"), NOW),
    ("expiring_release_window", lambda v: v["billing"].update(
        release_window={"opens": "2026-11-08T12:17:59Z", "closes": "2026-11-08T12:27:59Z"}), NOW),
    ("at_release_window_boundary", lambda v: v, datetime(2026, 11, 8, 12, 36, 1, tzinfo=UTC)),
])
def test_unusable_cabin_is_skipped_and_pruned(tmp_path, reason, mutate, run_now):
    runner = PoolRunner()
    assert run_cli(tmp_path, ["run", "--pool", "ci", "--", "true"], runner)[0] == 0
    old_lease = next(iter(runner.cabins))
    if mutate is None:
        del runner.cabins[old_lease]
    else:
        mutate(runner.cabins[old_lease])

    rc, out, _err, _ = run_cli(tmp_path, ["run", "--pool", "ci", "--json", "--", "true"], runner, now=run_now)
    assert rc == 0, reason
    doc = json.loads(out.strip().splitlines()[-1])
    assert doc["pool_hit"] is False and doc["cabin"] != old_lease, reason
    entries = pool_store(tmp_path).load_entries("ci")
    assert [e["lease_id"] for e in entries] == [doc["cabin"]]
    assert not (tmp_path / "motel" / "pools" / "ci" / f"{old_lease}.lock").exists()


def test_exactly_eight_minutes_before_release_window_is_still_usable(tmp_path):
    runner = PoolRunner()
    assert run_cli(tmp_path, ["run", "--pool", "ci", "--", "true"], runner)[0] == 0
    lease1 = next(iter(runner.cabins))
    # release_window.opens = 12:44:00Z, expires_at = 13:00:00Z -> 12:36:00Z has exactly 8m left
    at_8m = datetime(2026, 11, 8, 12, 36, 0, tzinfo=UTC)
    rc, out, _err, _ = run_cli(tmp_path, ["run", "--pool", "ci", "--json", "--", "true"], runner, now=at_8m)
    assert rc == 0
    doc = json.loads(out.strip().splitlines()[-1])
    assert doc["pool_hit"] is True and doc["cabin"] == lease1


def test_cabin_without_release_window_uses_expires_at(tmp_path):
    runner = PoolRunner()
    assert run_cli(tmp_path, ["run", "--pool", "ci", "--", "true"], runner)[0] == 0
    lease1 = next(iter(runner.cabins))
    runner.cabins[lease1]["billing"] = {"granularity": "per_second", "billed_hours": None,
                                        "paid_until": None, "release_window": None, "source": None}
    # 12:52:00Z -> 8m before 13:00:00Z -> usable
    rc1, out1, _, _ = run_cli(tmp_path, ["run", "--pool", "ci", "--json", "--", "true"], runner,
                              now=datetime(2026, 11, 8, 12, 52, 0, tzinfo=UTC))
    assert rc1 == 0 and json.loads(out1.strip().splitlines()[-1])["pool_hit"] is True
    # 12:53:00Z -> 7m before 13:00:00Z -> pruned and cold miss
    rc2, out2, _, _ = run_cli(tmp_path, ["run", "--pool", "ci", "--json", "--", "true"], runner,
                              now=datetime(2026, 11, 8, 12, 53, 0, tzinfo=UTC))
    assert rc2 == 0 and json.loads(out2.strip().splitlines()[-1])["pool_hit"] is False


# ---------------------------------------------------------------------------- exact (type, image, disk) matching


def test_mismatched_type_image_or_disk_never_reuses_wrong_cabin(tmp_path):
    cfg_file = tmp_path / "motel.toml"
    cfg_file.write_text('[cabins]\nenabled = true\ntypes = ["cpx32", "cpx52"]\nimage = "ubuntu-24.04"\n')
    runner = PoolRunner(default_type="cpx32")

    # 1. Default (cpx32, stock ubuntu-24.04, no disk)
    rc, out, _, _ = run_cli(tmp_path, ["run", "--pool", "ci", "--json", "--", "true"], runner, config_path=str(cfg_file))
    assert rc == 0
    base_id = json.loads(out.strip().splitlines()[-1])["cabin"]

    # Explicit --type cpx32 matches default cpx32 via config!
    rc, out, _, _ = run_cli(tmp_path, ["run", "--pool", "ci", "--type", "cpx32", "--json", "--", "true"],
                            runner, config_path=str(cfg_file))
    doc = json.loads(out.strip().splitlines()[-1])
    assert rc == 0 and doc["pool_hit"] is True and doc["cabin"] == base_id

    # 2. Different type (--type cpx52) must NOT match base_id
    rc, out, _, _ = run_cli(tmp_path, ["run", "--pool", "ci", "--type", "cpx52", "--json", "--", "true"],
                            runner, config_path=str(cfg_file))
    doc = json.loads(out.strip().splitlines()[-1])
    assert rc == 0 and doc["pool_hit"] is False and doc["cabin"] != base_id
    big_id = doc["cabin"]

    # 3. Baked image (--image omnigent) must NOT match stock image
    rc, out, _, _ = run_cli(tmp_path, ["run", "--pool", "ci", "--image", "omnigent", "--json", "--", "true"],
                            runner, config_path=str(cfg_file))
    doc = json.loads(out.strip().splitlines()[-1])
    assert rc == 0 and doc["pool_hit"] is False and doc["image"] == "omnigent"
    img_id = doc["cabin"]
    assert img_id not in (base_id, big_id)

    # Matching by numeric image id 4711 hits the omnigent cabin!
    rc, out, _, _ = run_cli(tmp_path, ["run", "--pool", "ci", "--image", "4711", "--json", "--", "true"],
                            runner, config_path=str(cfg_file))
    doc = json.loads(out.strip().splitlines()[-1])
    assert rc == 0 and doc["pool_hit"] is True and doc["cabin"] == img_id

    # Different baked image (--image rust-ci) must NOT match omnigent
    rc, out, _, _ = run_cli(tmp_path, ["run", "--pool", "ci", "--image", "rust-ci", "--json", "--", "true"],
                            runner, config_path=str(cfg_file))
    doc = json.loads(out.strip().splitlines()[-1])
    assert rc == 0 and doc["pool_hit"] is False and doc["cabin"] != img_id

    # 4. Extra disk (--disk 100G) must NOT match no-disk cabin
    rc, out, _, _ = run_cli(tmp_path, ["run", "--pool", "ci", "--disk", "100G", "--json", "--", "true"],
                            runner, config_path=str(cfg_file))
    doc = json.loads(out.strip().splitlines()[-1])
    assert rc == 0 and doc["pool_hit"] is False
    disk100_id = doc["cabin"]

    # Same disk written as plain GB ("100") hits disk100_id!
    rc, out, _, _ = run_cli(tmp_path, ["run", "--pool", "ci", "--disk", "100", "--json", "--", "true"],
                            runner, config_path=str(cfg_file))
    doc = json.loads(out.strip().splitlines()[-1])
    assert rc == 0 and doc["pool_hit"] is True and doc["cabin"] == disk100_id

    # Different disk size (--disk 200G) must NOT match 100G
    rc, out, _, _ = run_cli(tmp_path, ["run", "--pool", "ci", "--disk", "200G", "--json", "--", "true"],
                            runner, config_path=str(cfg_file))
    doc = json.loads(out.strip().splitlines()[-1])
    assert rc == 0 and doc["pool_hit"] is False and doc["cabin"] != disk100_id

    # Default (no --type, no --image, no --disk) still hits original base_id
    rc, out, _, _ = run_cli(tmp_path, ["run", "--pool", "ci", "--json", "--", "true"], runner, config_path=str(cfg_file))
    doc = json.loads(out.strip().splitlines()[-1])
    assert rc == 0 and doc["pool_hit"] is True and doc["cabin"] == base_id


def test_without_config_explicit_type_never_mismatches_default_type(tmp_path):
    runner = PoolRunner(default_type="cpx32")
    # Create explicit cpx52 first
    rc1, out1, _, _ = run_cli(tmp_path, ["run", "--pool", "ci", "--type", "cpx52", "--json", "--", "true"], runner)
    assert rc1 == 0
    cpx52_id = json.loads(out1.strip().splitlines()[-1])["cabin"]

    # Now omit --type (config_path=None): must NOT reuse cpx52; spawns default cpx32
    rc2, out2, _, _ = run_cli(tmp_path, ["run", "--pool", "ci", "--json", "--", "true"], runner)
    doc2 = json.loads(out2.strip().splitlines()[-1])
    assert rc2 == 0 and doc2["pool_hit"] is False and doc2["cabin"] != cpx52_id


# ---------------------------------------------------------------------------- pool ls & pool drain


def test_pool_ls_reports_live_state_lock_billing_and_prunes_released(tmp_path):
    runner = PoolRunner()
    # Empty pool ls works cleanly
    rc, out, _, _ = run_cli(tmp_path, ["pool", "ls"], runner)
    assert rc == 0 and "no pooled cabins" in out
    rc, out, _, _ = run_cli(tmp_path, ["pool", "ls", "--json"], runner)
    assert rc == 0 and json.loads(out)["cabins"] == []

    # Populate two pools ("ci" with two cabins, "docs" with one released cabin)
    assert run_cli(tmp_path, ["run", "--pool", "ci", "--image", "omnigent", "--", "true"], runner)[0] == 0
    assert run_cli(tmp_path, ["run", "--pool", "ci", "--type", "cpx52", "--", "true"], runner)[0] == 0
    assert run_cli(tmp_path, ["run", "--pool", "docs", "--", "true"], runner)[0] == 0

    ci_ids = [e["lease_id"] for e in pool_store(tmp_path).load_entries("ci")]
    docs_id = pool_store(tmp_path).load_entries("docs")[0]["lease_id"]
    runner.cabins[docs_id]["state"] = "released"

    # Hold a lock on the first ci cabin to verify busy vs idle reporting
    held = pool_store(tmp_path).try_lock_cabin("ci", ci_ids[0])
    assert held is not None
    try:
        rc, out, _, _ = run_cli(tmp_path, ["pool", "ls", "--json"], runner)
        assert rc == 0
        doc = json.loads(out)
        assert doc["ok"] is True and doc["pruned"] == [docs_id]
        by_id = {row["lease_id"]: row for row in doc["cabins"]}
        assert set(by_id) == set(ci_ids)
        assert by_id[ci_ids[0]]["lock"] == "busy"
        assert by_id[ci_ids[0]]["image"] == "omnigent"
        assert by_id[ci_ids[0]]["paid_until"] == "2026-11-08T13:00:00Z"
        assert by_id[ci_ids[0]]["release_window"]["opens"] == "2026-11-08T12:44:00Z"
        assert by_id[ci_ids[1]]["lock"] == "idle"
        assert by_id[ci_ids[1]]["server_type"] == "cpx52"

        # Human output for a single named pool
        rc_h, out_h, _, _ = run_cli(tmp_path, ["pool", "ls", "ci"], runner)
        assert rc_h == 0
        assert ci_ids[0] in out_h and "busy" in out_h and "idle" in out_h
        assert "paid_until 2026-11-08T13:00:00Z" in out_h
    finally:
        held.release()

    # Released docs cabin was pruned from disk
    assert pool_store(tmp_path).load_entries("docs") == []


def test_pool_drain_checks_out_idle_cabins_and_skips_busy_with_note(tmp_path):
    runner = PoolRunner()
    assert run_cli(tmp_path, ["run", "--pool", "ci", "--", "true"], runner)[0] == 0
    assert run_cli(tmp_path, ["run", "--pool", "ci", "--type", "cpx52", "--", "true"], runner)[0] == 0
    assert run_cli(tmp_path, ["run", "--pool", "other", "--", "true"], runner)[0] == 0

    ci_ids = [e["lease_id"] for e in pool_store(tmp_path).load_entries("ci")]
    other_id = pool_store(tmp_path).load_entries("other")[0]["lease_id"]

    # Lock ci_ids[0] so it is busy; drain only pool "ci"
    held = pool_store(tmp_path).try_lock_cabin("ci", ci_ids[0])
    assert held is not None
    try:
        rc, out, err, _ = run_cli(tmp_path, ["pool", "drain", "ci", "--json"], runner)
        assert rc == 0
        doc = json.loads(out)
        assert doc["ok"] is True and doc["pool"] == "ci"
        assert [d["lease_id"] for d in doc["drained"]] == [ci_ids[1]]
        assert [s["lease_id"] for s in doc["skipped"]] == [ci_ids[0]]
        assert f"skipping busy cabin {ci_ids[0]}" in err
        # Idle cabin was checked out and its private key removed; busy cabin and "other" pool untouched
        assert runner.cabins[ci_ids[1]]["state"] == "released"
        assert runner.cabins[ci_ids[0]]["state"] == "active"
        assert runner.cabins[other_id]["state"] == "active"
        assert not (tmp_path / "motel" / "cabins" / ci_ids[1] / "id_ed25519").exists()
        assert (tmp_path / "motel" / "cabins" / ci_ids[0] / "id_ed25519").exists()
    finally:
        held.release()

    # Drain all pools (no NAME): checks out both remaining cabins
    rc2, out2, _, _ = run_cli(tmp_path, ["pool", "drain"], runner)
    assert rc2 == 0
    assert f"drained ci {ci_ids[0]}" in out2 and f"drained other {other_id}" in out2
    assert pool_store(tmp_path).load_entries("ci") == []
    assert pool_store(tmp_path).load_entries("other") == []


def test_pool_drain_reports_cleanup_pending_as_exit_9(tmp_path):
    runner = PoolRunner()
    assert run_cli(tmp_path, ["run", "--pool", "ci", "--", "true"], runner)[0] == 0
    lease = next(iter(runner.cabins))
    runner.checkout_overrides[lease] = (
        9,
        {"schema": "motel/v1", "ok": True, "cabin": dict(runner.cabins[lease], state="cleanup_pending")},
    )
    rc, out, err, _ = run_cli(tmp_path, ["pool", "drain", "ci", "--json"], runner)
    assert rc == 9 and "cleanup pending" in err
    doc = json.loads(out)
    assert doc["ok"] is False and doc["drained"][0]["state"] == "cleanup_pending"
    assert pool_store(tmp_path).load_entries("ci") == []


def test_cold_miss_checkin_failure_leaves_pool_empty(tmp_path):
    runner = PoolRunner()
    runner.checkin_rc = 3
    rc, out, _err, _ = run_cli(tmp_path, ["run", "--pool", "ci", "--json", "--", "true"], runner)
    assert rc == 3 and json.loads(out)["ok"] is False
    assert pool_store(tmp_path).load_entries("ci") == []
    assert not runner.by("/usr/bin/rsync") and not runner.by("/usr/bin/ssh")


def test_pooled_run_uses_explicit_ttl_on_cold_miss(tmp_path):
    runner = PoolRunner()
    rc, _out, _err, _ = run_cli(tmp_path, ["run", "--pool", "ci", "--for", "2h", "--", "true"], runner)
    assert rc == 0
    checkin_argv = runner.by("/usr/bin/sudo")[0][0]
    assert checkin_argv[checkin_argv.index("--for") + 1] == "2h"
    assert (NOW + timedelta(hours=1)) > NOW
