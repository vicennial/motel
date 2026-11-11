"""Round 2: the key tree is root-owned end to end. Root never writes through, or chowns inside, a path that an
unprivileged user could have swapped."""
import os
import stat
from datetime import timedelta

import pytest
from cabin_fakes import HOST, World, pubkey

from motel.cabins import guestchan
from motel.cabins.errors import Fail, Guarded, PolicyRefused
from motel.cabins.store import Store

LEASE = "build-0123456789ab"


@pytest.fixture
def tree(tmp_path):
    victim = tmp_path / "victim-etc"          # stands in for /etc
    victim.mkdir()
    (victim / "shadow").write_text("root:x\n")
    store = Store(str(tmp_path / "state"), host_id=HOST, keys_dir=str(tmp_path / "keys"))
    os.makedirs(tmp_path / "keys", mode=0o750)
    return tmp_path, victim, store


@pytest.fixture(autouse=True)
def no_chown(monkeypatch):
    """Nothing in the cabin package may change ownership of anything, ever."""
    def boom(*_a, **_k):
        raise AssertionError("chown is forbidden")
    for name in ("chown", "lchown", "fchown"):
        monkeypatch.setattr(os, name, boom)


def test_lease_key_files_are_created_root_private_with_exact_modes(tree):
    tmp, _victim, store = tree
    with store.key_dir_fd(LEASE) as dfd:
        store.write_key(dfd, "controller_ed25519", "KEY\n")
        store.write_key(dfd, "known_hosts", "host key\n")
    base = tmp / "keys" / LEASE
    assert stat.S_IMODE(os.stat(base).st_mode) == 0o750
    assert stat.S_IMODE(os.stat(base / "controller_ed25519").st_mode) == 0o640
    assert all(os.stat(p).st_uid == os.geteuid() for p in (base, base / "known_hosts"))
    assert store.key_access_problems() == []


def test_swapping_the_lease_directory_after_it_is_opened_cannot_redirect_writes(tree):
    tmp, victim, store = tree
    with store.key_dir_fd(LEASE) as dfd:
        path = tmp / "keys" / LEASE
        os.rename(path, str(path) + ".moved")
        os.symlink(victim, path)                  # what a writer of keys/ could do between open and write
        store.write_key(dfd, "controller_ed25519", "KEY\n")
        store.write_key(dfd, "known_hosts", "host key\n")
    assert sorted(os.listdir(victim)) == ["shadow"]
    assert sorted(os.listdir(str(path) + ".moved")) == ["controller_ed25519", "known_hosts"]


def test_a_symlinked_lease_directory_is_refused(tree):
    tmp, victim, store = tree
    os.symlink(victim, tmp / "keys" / LEASE)
    with pytest.raises(Guarded, match="symlink"), store.key_dir_fd(LEASE):
        pass
    assert sorted(os.listdir(victim)) == ["shadow"]


def test_a_symlinked_keys_directory_is_refused_at_use(tree):
    tmp, victim, store = tree
    os.rename(tmp / "keys", tmp / "keys.moved")
    os.symlink(victim, tmp / "keys")
    with pytest.raises(Guarded), store.key_dir_fd(LEASE):
        pass
    assert sorted(os.listdir(victim)) == ["shadow"]


def test_a_preplanted_known_hosts_symlink_is_never_followed_or_overwritten(tree):
    tmp, victim, store = tree
    with store.key_dir_fd(LEASE) as dfd:
        os.symlink(victim / "shadow", tmp / "keys" / LEASE / "known_hosts")
        with pytest.raises(Guarded, match="already exists"):
            store.write_key(dfd, "known_hosts", "attacker wins\n")
    assert (victim / "shadow").read_text() == "root:x\n"


def test_a_group_writable_or_foreign_directory_is_refused(tree):
    tmp, _victim, store = tree
    (tmp / "keys" / LEASE).mkdir()
    os.chmod(tmp / "keys" / LEASE, 0o770)
    with pytest.raises(Guarded, match="not private"), store.key_dir_fd(LEASE):
        pass


def test_write_key_rejects_path_tricks(tree):
    _tmp, _victim, store = tree
    with store.key_dir_fd(LEASE) as dfd:
        for name in ("../escape", "a/b", ".", ".."):
            with pytest.raises(Fail):
                store.write_key(dfd, name, "x")


def test_full_checkin_never_chowns_and_leaves_a_root_owned_tree(tmp_path):
    w = World(str(tmp_path))
    w.store = Store(str(tmp_path / "state2"), clock=w.clock, host_id=HOST, keys_dir=str(tmp_path / "keys"))
    view = w.checkin()
    base = tmp_path / "keys" / view["lease_id"]
    assert sorted(os.listdir(base)) == ["controller_ed25519", "known_hosts"]
    assert stat.S_IMODE(os.stat(base / "controller_ed25519").st_mode) == 0o640
    # the host private key never lands in the lease key directory or anywhere the guest user can read
    assert "host_ed25519" not in os.listdir(base)
    assert not [n for n in os.listdir(tmp_path / "state2" / "tmp")]


def test_keygen_output_goes_to_a_root_only_scratch_directory(tmp_path):
    w = World(str(tmp_path))
    seen = []
    from cabin_fakes import fake_keygen

    def spy(directory, name, comment):
        seen.append((directory, stat.S_IMODE(os.stat(directory).st_mode)))
        return fake_keygen(directory, name, comment)
    w.checkin(w.controller(keygen=spy))
    assert seen and all(mode == 0o700 and "keygen-" in d for d, mode in seen)
    assert not any(str(tmp_path / "keys") in d for d, _m in seen)


def test_a_failed_write_leaves_no_partial_key_file(tree):
    _tmp, _victim, store = tree
    with store.key_dir_fd(LEASE) as dfd:
        with pytest.raises(UnicodeEncodeError):
            store.write_key(dfd, "bad", "\ud800")
        assert "bad" not in os.listdir(dfd)


def test_old_or_wrongly_laid_out_keys_are_reported_not_chowned(tree):
    tmp, _victim, store = tree
    with store.key_dir_fd(LEASE) as dfd:
        store.write_key(dfd, "controller_ed25519", "KEY\n", mode=0o600)       # root-only, as before the system user
    assert store.key_access_problems() == [f"{LEASE}/controller_ed25519"]
    # the doctor surfaces it for root, and the guest channel simply reads as unknown
    w = World(str(tmp / "w"))
    w.store = store
    report = w.controller(uid=0).doctor()
    check = next(c for c in report["checks"] if c["check"].startswith("controller key layout"))
    assert not check["ok"] and LEASE in check["detail"]


def test_ssh_client_gets_the_key_by_path_from_the_root_owned_tree(tmp_path):
    seen = []
    ch = guestchan.GuestChannel(str(tmp_path / "keys"), runner=lambda argv, t: (seen.append(argv), (0, b'{"schema": "motel-guest/v1"}'))[1])
    ch.call({"lease_id": LEASE, "name": "motel-" + LEASE, "access": {"mode": "ssh-allowlist", "address": "203.0.113.7"}},
            "ready")
    assert str(tmp_path / "keys" / LEASE / "controller_ed25519") in seen[0]


# ---- extend throttling

def test_tiny_extensions_are_refused_before_any_provider_call(tmp_path):
    w = World(str(tmp_path))
    c = w.controller()
    view = w.checkin(c)
    w.clock.advance(minutes=1)
    before = len(w.hz.reads) + len(w.hz.writes)
    with pytest.raises(PolicyRefused, match="at least 5 minutes") as exc:
        c.extend(view["lease_id"], timedelta(hours=2, minutes=2))     # moves the expiry by about a minute
    assert "extend" in exc.value.hint
    assert len(w.hz.reads) + len(w.hz.writes) == before


def test_the_same_cabin_cannot_be_extended_twice_within_a_minute(tmp_path):
    w = World(str(tmp_path))
    c = w.controller()
    view = w.checkin(c)
    c.extend(view["lease_id"], timedelta(hours=4))
    calls = len(w.hz.reads) + len(w.hz.writes)
    with pytest.raises(PolicyRefused, match="a moment ago"):
        c.extend(view["lease_id"], timedelta(hours=6))
    assert len(w.hz.reads) + len(w.hz.writes) == calls
    w.clock.advance(seconds=61)
    assert c.extend(view["lease_id"], timedelta(hours=6))["time_left_s"] > 5 * 3600


# ---- reboot safety of the pinned host key

def test_bootstrap_disables_cloud_init_so_a_reboot_keeps_the_pinned_host_key(tmp_path):
    w = World(str(tmp_path))
    w.checkin()
    user_data = next(x[2]["user_data"] for x in w.hz.writes if x[1] == "/servers")
    assert "touch /etc/cloud/cloud-init.disabled" in user_data
    # it must come after the caches are removed and before the readiness file says "done"
    assert user_data.index("rm -f /run/cloud-init/instance-data.json") < user_data.index("touch /etc/cloud/cloud-init.disabled")
    assert user_data.index("touch /etc/cloud/cloud-init.disabled") < user_data.index('"bootstrap": "done"')
    assert pubkey("C")
