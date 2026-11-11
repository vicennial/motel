import json
import os

import pytest
from cabin_fakes import HOST, HOST_IPV4, FakeHetzner, World, make_config, section

from motel.cabins import setup as su
from motel.cabins.config import CabinPaths
from motel.cabins.errors import Guarded, Prerequisite, Usage

TOKEN = "hcloud-token-abcdefghijklmnop"
HOST_IPV6 = "2a01:4f8:1c1b:1::1"      # a global (non-documentation) address, as a real host has


def metadata(**over):
    data = {"instance-id": str(HOST), "public-ipv4": HOST_IPV4, "availability-zone": "fsn1-dc14",
            "network-config": f"config:\n- type: physical\n  subnets:\n  - address: {HOST_IPV6}/64\n"}
    data.update(over)

    def fetch(key):
        if key not in data:
            raise OSError("no such key")
        return data[key]
    return fetch


@pytest.fixture
def env(tmp_path):
    paths = CabinPaths(str(tmp_path))
    os.makedirs(os.path.dirname(paths.config_file), exist_ok=True)
    w = World(str(tmp_path / "w"))
    cfg = make_config()
    return paths, cfg, w


def run(env, *, fetch=None, token=TOKEN, hz=None):
    paths, cfg, w = env
    return su.setup_hetzner(paths, cfg, token_reader=lambda _p: token, fetch=fetch or metadata(),
                            transport_factory=lambda _tok: hz or w.hz)


def test_discover_host_reads_id_address_location_and_ipv6():
    host = su.discover_host(metadata())
    assert host["server_id"] == HOST and host["public_ipv4"] == HOST_IPV4
    assert host["public_ipv6"] == HOST_IPV6 and host["location"] == "fsn1"


def test_discover_host_refuses_without_metadata_or_public_ipv4():
    with pytest.raises(Prerequisite, match="metadata"):
        su.discover_host(metadata(**{"instance-id": "x"}))
    with pytest.raises(Prerequisite):
        su.discover_host(lambda _k: (_ for _ in ()).throw(OSError("no")))
    with pytest.raises(Prerequisite, match="public IPv4"):
        su.discover_host(metadata(**{"public-ipv4": "10.0.0.5"}))


def test_setup_writes_token_and_a_nonsecret_receipt(env):
    paths, cfg, _w = env
    result = run(env)
    assert result["ok"] and result["host_server_id"] == HOST
    token_path = paths.real(cfg.token_file)
    with open(token_path) as handle:
        assert handle.read().strip() == TOKEN
    assert oct(os.stat(token_path).st_mode & 0o777) == "0o600"
    with open(paths.receipt_file) as handle:
        receipt = json.load(handle)
    assert receipt["host"]["public_ipv4"] == HOST_IPV4 and receipt["hetzner"]["protected_not_found"] == [HOST]
    assert TOKEN not in json.dumps(receipt)


def test_setup_refuses_a_token_that_sees_a_non_motel_server_and_stores_nothing(env):
    paths, cfg, w = env
    w.hz.add_server("production-web", {})
    with pytest.raises(Guarded, match="refused"):
        run(env)
    assert not os.path.exists(paths.real(cfg.token_file)) and not os.path.exists(paths.receipt_file)


def test_setup_refuses_a_token_that_sees_the_host(env):
    paths, cfg, w = env
    w.hz.visible.add(HOST)
    with pytest.raises(Guarded):
        run(env)
    assert not os.path.exists(paths.real(cfg.token_file))


def test_setup_refuses_motel_labelled_servers_without_a_local_intent_record(env):
    paths, _cfg, w = env
    labels = {"managed-by": "motel", "role": "cabin", "controller": str(HOST), "lease": "ghost-aaaaaaaaaaaa",
              "intent": "1" * 32}
    w.hz.add_server("motel-ghost-aaaaaaaaaaaa", labels)
    with pytest.raises(Guarded, match="no intent record"):
        run(env)
    assert not os.path.exists(paths.receipt_file)


def test_setup_refuses_servers_labelled_for_another_controller(env):
    labels = {"managed-by": "motel", "role": "cabin", "controller": "42"}
    env[2].hz.add_server("motel-other", labels)
    with pytest.raises(Guarded):
        run(env)


def test_setup_refuses_when_provider_is_down_and_stores_nothing(env):
    paths, cfg, w = env
    w.hz.unavailable = True
    with pytest.raises(Usage, match="provider check failed"):
        run(env)
    assert not os.path.exists(paths.real(cfg.token_file))


def test_receipt_for_another_server_is_not_overwritten(env):
    paths, _cfg, _w = env
    os.makedirs(os.path.dirname(paths.receipt_file), exist_ok=True)
    with open(paths.receipt_file, "w") as handle:
        json.dump({"host": {"server_id": 7}}, handle)
    with pytest.raises(Guarded, match="set up for server 7"):
        run(env)


def test_setup_needs_root_in_strict_mode(monkeypatch):
    paths = CabinPaths("/")
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    with pytest.raises(Usage, match="must run as root"):
        su.setup_hetzner(paths, make_config(), token_reader=lambda _p: TOKEN, fetch=metadata())


def test_read_secret_refuses_non_terminals():
    class Pipe:
        def isatty(self):
            return False
    with pytest.raises(Usage, match="interactive terminal"):
        su.read_secret("x: ", Pipe())


# ---- --token-file (automation)

def write_token(tmp_path, mode=0o600, text=TOKEN + "\n"):
    path = tmp_path / "tok"
    path.write_text(text)
    os.chmod(path, mode)
    return str(path)


def test_token_file_accepts_a_strict_owned_file(tmp_path):
    assert su.read_token_file(write_token(tmp_path), owner_uid=os.getuid()) == TOKEN
    assert su.read_token_file(write_token(tmp_path, 0o400), owner_uid=os.getuid()) == TOKEN


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o660, 0o604])
def test_token_file_refuses_group_or_world_access(tmp_path, mode):
    with pytest.raises(Usage, match="0600"):
        su.read_token_file(write_token(tmp_path, mode), owner_uid=os.getuid())


def test_token_file_refuses_symlinks_wrong_owner_directories_and_oversize(tmp_path):
    real = write_token(tmp_path)
    link = tmp_path / "link"
    os.symlink(real, link)
    with pytest.raises(Usage, match="symlink"):
        su.read_token_file(str(link), owner_uid=os.getuid())
    with pytest.raises(Usage, match="owned by root"):
        su.read_token_file(real, owner_uid=os.getuid() + 1)
    with pytest.raises(Usage):
        su.read_token_file(str(tmp_path), owner_uid=os.getuid())
    with pytest.raises(Usage, match="too large"):
        su.read_token_file(write_token(tmp_path, text="x" * 5000), owner_uid=os.getuid())
    with pytest.raises(Usage, match="token"):
        su.read_token_file(write_token(tmp_path, text="short"), owner_uid=os.getuid())
    with pytest.raises(Usage):
        su.read_token_file(str(tmp_path / "missing"), owner_uid=os.getuid())


def test_token_file_errors_never_echo_the_token(tmp_path):
    path = write_token(tmp_path, 0o644, "SECRETSECRETSECRETSECRET")
    with pytest.raises(Usage) as exc:
        su.read_token_file(path, owner_uid=os.getuid())
    assert "SECRETSECRET" not in exc.value.line()


def test_tailscale_setup_requires_hetzner_first_and_tailscale_mode(env):
    paths, _cfg, w = env
    with pytest.raises(Prerequisite, match='not "tailscale"'):
        su.setup_tailscale(paths, make_config())
    ts_cfg = make_config("tailscale")
    with pytest.raises(Prerequisite, match="project first"):
        su.setup_tailscale(paths, ts_cfg)
    assert isinstance(w.hz, FakeHetzner) and section()
