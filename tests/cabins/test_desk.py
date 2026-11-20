"""``motel-desk cabin ...``: routing, authorization, exit codes and JSON output over the fakes."""
import io
import json
import os

import pytest
from cabin_fakes import (
    HOST,
    HOST_IPV4,
    HZ_TOKEN_HASH,
    World,
    fake_keygen,
    pubkey,
    receipt,
)

from motel.cabins import desk
from motel.cabins.config import CabinPaths
from motel.cabins.desk import Caller
from motel.cabins.provider import GuardedApi
from motel.cabins.store import Store
from motel.cabins.util import sha256_text

TOKEN = "hcloud-token-abcdefghijklmnop"
TOML = """\
[host]
allow_users = ["alice", "@cabiners"]

[cabins]
enabled = true
types = ["cpx32", "cpx52"]
max_eur_per_hour = 0.2
"""


class Env:
    def __init__(self, tmp_path):
        self.root = str(tmp_path / "root")
        self.paths = CabinPaths(self.root)
        self.w = World(str(tmp_path / "w"))
        os.makedirs(os.path.dirname(self.paths.config_file))
        with open(self.paths.config_file, "w") as handle:
            handle.write(TOML)
        self.write_setup()

    def write_setup(self):
        token_path = self.paths.real("/etc/motel/credentials/cabins.token")
        os.makedirs(os.path.dirname(token_path), exist_ok=True)
        with open(token_path, "w") as handle:
            handle.write(TOKEN + "\n")
        doc = receipt()
        doc["hetzner"]["token_sha256"] = sha256_text(TOKEN)
        with open(self.paths.receipt_file, "w") as handle:
            json.dump(doc, handle)

    def call(self, *argv, uid=1000, name="alice", groups=(), operator=False, **hooks):
        out, err = io.StringIO(), io.StringIO()
        caller = Caller(uid=uid, gid=uid, name=name, operator=operator)
        wiring = {"hetzner_factory": lambda _t, h: GuardedApi(self.w.hz, h), "guest": self.w.guest,
                      "fetch": self.w.meta.__getitem__, "clock": self.w.clock, "sleep": self.w.clock.sleep,
                      "keygen": fake_keygen, "groups_fn": lambda _c: set(groups)}
        wiring.update(hooks)
        rc = desk.run(list(argv), paths=self.paths, caller=caller, out=out, err=err, **wiring)
        return rc, out.getvalue(), err.getvalue()


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def checkin(env, *extra, **kw):
    return env.call("checkin", "--task", "build", "--client-key", pubkey("U"), "--json", *extra, **kw)


def test_checkin_json_and_human_output(env):
    rc, out, _err = checkin(env, "--type", "cpx32", "--for", "90m")
    doc = json.loads(out)
    assert rc == 0 and doc["ok"] and doc["cabin"]["offer"]["server_type"] == "cpx32"
    assert doc["cabin"]["ssh"]["user"] == "cabin" and doc["cabin"]["access"]["mode"] == "ssh-allowlist"
    assert Store(env.paths.state_dir).load(doc["cabin"]["lease_id"])["owner"] == "alice"
    rc, out, _ = env.call("checkin", "--task", "x", "--client-key", pubkey("U"))
    assert rc == 0 and "cabin x-" in out and "Deletion is final" in out


def test_checkin_with_disk(env):
    rc, out, _ = checkin(env, "--disk", "100G")
    cab = json.loads(out)["cabin"]
    assert rc == 0 and cab["disk"]["size_gb"] == 100 and cab["volume_id"] in env.w.hz.volumes


def test_user_not_in_allow_users_is_refused(env):
    rc, out, _ = checkin(env, uid=1001, name="mallory")
    assert rc == 3 and "allow_users" in json.loads(out)["error"]
    assert env.w.hz.writes == []


def test_group_membership_allows_a_user(env):
    rc, _out, _ = checkin(env, uid=1002, name="bob", groups=("cabiners",))
    assert rc == 0


def test_operator_verbs_need_root(env):
    for verb in (["reap"], ["setup", "hetzner"]):
        rc, out, _ = env.call(*verb, "--json")
        assert rc == 3 and "operator command" in json.loads(out)["error"]


def test_operator_via_sudo_is_allowed_to_reap(env):
    rc, out, _ = env.call("reap", "--json", uid=1000, name="admin", operator=True)
    assert rc == 0 and json.loads(out)["apply"] is False


def test_reap_apply_deletes_expired_cabins(env):
    rc, out, _ = checkin(env)
    lease = json.loads(out)["cabin"]["lease_id"]
    env.w.clock.advance(hours=3)
    rc, out, _ = env.call("reap", "--apply", "--json", uid=0, name="root")
    assert rc == 0 and json.loads(out)["actions"][0]["state"] == "released"
    assert not env.w.hz.servers and lease


def test_ls_status_extend_checkout_flow(env):
    _rc, out, _ = checkin(env)
    lease = json.loads(out)["cabin"]["lease_id"]
    rc, out, _ = env.call("ls", "--json")
    assert rc == 0 and [c["lease_id"] for c in json.loads(out)["cabins"]] == [lease]
    rc, out, _ = env.call("status", lease, "--json")
    assert rc == 0 and json.loads(out)["cabin"]["state"] == "active"
    rc, out, _ = env.call("extend", lease, "--for", "5h", "--json")
    assert rc == 0 and json.loads(out)["cabin"]["time_left_s"] > 4 * 3600
    rc, out, _ = env.call("checkout", lease, "--json")
    assert rc == 0 and json.loads(out)["cabin"]["state"] == "released"
    rc, _out, err = env.call("ls")
    assert rc == 0 and err == ""


def test_other_users_cannot_see_or_delete_a_cabin(env):
    _rc, out, _ = checkin(env)
    lease = json.loads(out)["cabin"]["lease_id"]
    rc, out, _ = env.call("checkout", lease, "--json", uid=1002, name="bob", groups=("cabiners",))
    assert rc == 7 and json.loads(out)["code"] == 7
    assert env.w.hz.servers


def test_incomplete_checkout_exits_9(env):
    from motel.cabins.errors import ApiError
    _rc, out, _ = checkin(env)
    cab = json.loads(out)["cabin"]
    env.w.hz.fail[("DELETE", f"/servers/{cab['server_id']}")] = [ApiError(503, "x")]
    rc, out, _ = env.call("checkout", cab["lease_id"], "--json")
    assert rc == 9 and json.loads(out)["cabin"]["state"] == "cleanup_pending"


def test_usage_errors_exit_2_and_never_traceback(env):
    for argv in (["bogus"], ["checkin"], ["extend", "x"], ["status"], ["checkin", "--task", "t", "--client-key", "k",
                                                                         "--abbrev"]):
        rc, out, err = env.call(*argv)
        assert rc == 2 and "Traceback" not in out + err and "motel:" in err


def test_bad_duration_and_size_are_usage_errors(env):
    assert checkin(env, "--for", "soon")[0] == 2
    assert checkin(env, "--disk", "lots")[0] == 2


def test_not_set_up_exits_8(tmp_path):
    e = Env(tmp_path)
    os.unlink(e.paths.receipt_file)
    rc, out, _ = checkin(e)
    assert rc == 8 and json.loads(out)["hint"]


def test_missing_cabins_table_exits_8(tmp_path):
    e = Env(tmp_path)
    with open(e.paths.config_file, "w") as handle:
        handle.write("[host]\nallow_users = []\n")
    assert checkin(e)[0] == 8


def test_disabled_cabins_exit_8(tmp_path):
    e = Env(tmp_path)
    with open(e.paths.config_file, "w") as handle:
        handle.write(TOML.replace("enabled = true", "enabled = false"))
    assert checkin(e)[0] == 8


def test_ls_and_status_use_the_cleanup_config_when_cabins_are_disabled(tmp_path):
    """`enabled = false` stops new checkins only: the verbs that look at a cabin keep working (docs/cabins.md)."""
    e = Env(tmp_path)
    with open(e.paths.config_file, "w") as handle:
        handle.write(TOML.replace("enabled = true", "enabled = false"))
    rc, out, err = e.call("ls", "--json")
    assert rc == 0, out + err
    assert json.loads(out)["cabins"] == []
    rc, out, _ = e.call("status", "swift-gull-0123456789ab", "--json")
    assert rc == 7 and json.loads(out)["code"] == 7          # not found, not "cabins are not enabled"
    assert checkin(e)[0] == 8                                 # and a new checkin is still refused


def test_ls_survives_a_broken_unrelated_cabins_key_and_a_missing_table(tmp_path):
    e = Env(tmp_path)
    with open(e.paths.config_file, "w") as handle:
        handle.write(TOML.replace("max_eur_per_hour = 0.2", "max_eur_per_hour = \"lots\""))
    rc, out, err = e.call("ls", "--json")
    assert rc == 0, out + err
    with open(e.paths.config_file, "w") as handle:
        handle.write("[host]\nallow_users = [\"alice\"]\n")
    assert e.call("ls", "--json")[0] == 0
    assert checkin(e)[0] == 8


def test_ls_in_cleanup_mode_still_requires_an_allowed_caller(tmp_path):
    e = Env(tmp_path)
    with open(e.paths.config_file, "w") as handle:
        handle.write(TOML.replace("enabled = true", "enabled = false"))
    rc, out, _ = e.call("ls", "--json", uid=1234, name="mallory")
    assert rc != 0 and "allow_users" in out


def test_reap_apply_exits_0_with_a_note_when_cabins_were_never_set_up(tmp_path):
    e = Env(tmp_path)
    os.unlink(e.paths.receipt_file)
    rc, out, err = e.call("reap", "--apply", uid=0, name="root")
    assert rc == 0 and err == "" and "not set up" in out and "nothing to reap" in out
    rc, out, _ = e.call("reap", "--apply", "--json", uid=0, name="root")
    doc = json.loads(out)
    assert rc == 0 and doc["ok"] and doc["set_up"] is False and doc["actions"] == [] and doc["errors"] == []
    assert not os.path.exists(e.paths.state_dir), "the short path creates no state"


def test_reap_still_runs_for_a_receipt_or_a_lease_record(tmp_path):
    e = Env(tmp_path)
    rc, out, _ = e.call("reap", "--json", uid=0, name="root")
    assert "set_up" not in json.loads(out)                       # a receipt exists: the real pass
    os.unlink(e.paths.receipt_file)
    leases = os.path.join(e.paths.state_dir, "leases")
    os.makedirs(leases, exist_ok=True)
    with open(os.path.join(leases, "swift-gull-0123456789ab.json"), "w") as handle:
        json.dump({"lease_id": "swift-gull-0123456789ab", "state": "released"}, handle)
    rc, out, _ = e.call("reap", "--json", uid=0, name="root")
    assert "set_up" not in json.loads(out)                       # a lease record exists: the real pass, never skipped


def test_a_user_still_cannot_run_reap_when_cabins_are_not_set_up(tmp_path):
    e = Env(tmp_path)
    os.unlink(e.paths.receipt_file)
    rc, out, _ = e.call("reap", "--apply", "--json")
    assert rc != 0 and "operator" in out


def test_doctor_json_and_exit_code(env):
    rc, out, _ = env.call("doctor", "--json")
    doc = json.loads(out)
    assert rc == 0 and doc["ok"]
    env.w.hz.add_server("production-web", {})
    env.w.clock.advance(seconds=31)             # users are served a 30 s cache of provider reads
    rc, out, _ = env.call("doctor", "--json")
    assert rc == 8 and not json.loads(out)["ok"]


def test_json_error_shape_matches_the_core(env):
    rc, out, _ = checkin(env, "--type", "ccx63")
    doc = json.loads(out)
    assert rc == 3 and doc["schema"] == "motel/v1" and doc["ok"] is False and doc["code"] == 3
    assert doc["error"] and doc["hint"]


def test_human_error_format(env):
    rc, out, err = env.call("checkin", "--task", "t", "--client-key", pubkey("U"), "--type", "ccx63")
    assert rc == 3 and out == "" and err.startswith("motel: ") and ". Try: " in err


def test_internal_errors_do_not_leak_details(env):
    def boom(*_a, **_k):
        raise RuntimeError("secret-detail-xyz")
    rc, out, err = env.call("ls", "--json", hetzner_factory=boom)
    assert rc == 5 and "secret-detail-xyz" not in out + err


def test_setup_token_file_option_reaches_the_reader(env, tmp_path):
    path = tmp_path / "tok"
    path.write_text(TOKEN + "\n")
    os.chmod(path, 0o600)
    called = {}

    def transport(token):
        called["token"] = token
        return env.w.hz
    rc, out, _ = env.call("setup", "hetzner", "--token-file", str(path), "--json", uid=0, name="root",
                          token_file_uid=os.getuid(), setup_transport=transport, fetch=_metadata())
    doc = json.loads(out)
    assert rc == 0 and doc["ok"] and called["token"] == TOKEN
    assert TOKEN not in out


def test_setup_token_file_refuses_loose_mode_and_other_providers(env, tmp_path):
    path = tmp_path / "tok"
    path.write_text(TOKEN + "\n")
    os.chmod(path, 0o644)
    rc, out, _ = env.call("setup", "hetzner", "--token-file", str(path), "--json", uid=0, name="root",
                          token_file_uid=os.getuid(), fetch=_metadata())
    assert rc == 2 and "0600" in json.loads(out)["error"] and TOKEN not in out
    rc, out, _ = env.call("setup", "tailscale", "--token-file", str(path), "--json", uid=0, name="root")
    assert rc == 2


def test_setup_token_file_refused_for_non_operators(env, tmp_path):
    rc, _out, _ = env.call("setup", "hetzner", "--token-file", "/nonexistent", "--json")
    assert rc == 3


def test_root_helper_reads_only_root_owned_files_in_strict_mode(tmp_path, monkeypatch):
    path = tmp_path / "f"
    path.write_text("x")
    os.chmod(path, 0o600)
    monkeypatch.setattr(os, "lstat", lambda p: type("S", (), {"st_mode": 0o100600, "st_uid": 1000})())
    with pytest.raises(Exception, match="root-owned"):
        desk.check_root_file(str(path), private=True)


def test_sudoers_verbs_are_the_user_verbs_only():
    assert desk.USER_VERBS == ("checkin", "ls", "status", "extend", "checkout", "doctor", "images")
    assert not set(desk.USER_VERBS) & set(desk.OPERATOR_VERBS)
    assert HOST and HOST_IPV4 and HZ_TOKEN_HASH


def _metadata():
    data = {"instance-id": str(HOST), "public-ipv4": HOST_IPV4, "availability-zone": "fsn1-dc14"}

    def fetch(key):
        if key not in data:
            raise OSError("none")
        return data[key]
    return fetch
