"""The desk opens files that a user names while running as root: it must not be fooled (the confused-deputy cases)."""
import hashlib
import json
import os

import pytest

from motel.tours import state
from motel.tours.errors import Refused, Usage

UID = os.getuid()


def test_a_regular_file_of_the_caller_opens(tmp_path):
    path = tmp_path / "f"
    path.write_bytes(b"abc")
    fd, size = state.open_user_file(str(path), UID, 100)
    os.close(fd)
    assert size == 3


@pytest.mark.parametrize("bad", ["relative/path", "", None, "/tmp/x\0y", "/" + "a" * 5000, 5])
def test_names_must_be_absolute_plain_strings(bad):
    with pytest.raises(Usage):
        state.open_user_file(bad, UID, 100)


def test_a_symlink_is_refused(tmp_path):
    (tmp_path / "real").write_bytes(b"x")
    os.symlink(str(tmp_path / "real"), str(tmp_path / "link"))
    with pytest.raises(Usage):
        state.open_user_file(str(tmp_path / "link"), UID, 100)


def test_a_fifo_is_refused_without_blocking(tmp_path):
    os.mkfifo(str(tmp_path / "fifo"))
    with pytest.raises(Usage, match="regular file"):
        state.open_user_file(str(tmp_path / "fifo"), UID, 100)


def test_devices_and_directories_are_refused(tmp_path):
    for path in ("/dev/zero", str(tmp_path)):
        with pytest.raises(Usage):
            state.open_user_file(path, UID, 100)


def test_a_file_of_someone_else_is_refused_unless_root_asks(tmp_path):
    path = tmp_path / "f"
    path.write_bytes(b"x")
    with pytest.raises(Usage, match="belong to you"):
        state.open_user_file(str(path), UID + 1, 100)
    fd, _ = state.open_user_file(str(path), 0, 100)       # root may name any file
    os.close(fd)


def test_the_size_cap_is_enforced_when_opening_and_when_copying(tmp_path):
    path = tmp_path / "f"
    path.write_bytes(b"x" * 50)
    with pytest.raises(Refused):
        state.open_user_file(str(path), UID, 10)
    store = state.TourStore(str(tmp_path / "tours"))
    store.create("t-0123456789", {"id": "t-0123456789", "status": "staging", "owner": {"uid": UID}, "guests": {}})
    fd = os.open(str(path), os.O_RDONLY)
    try:
        with pytest.raises(Refused):
            store.stage_input("t-0123456789", "k" * 16, fd, 10)       # a file that grew after the check
    finally:
        os.close(fd)
    assert not os.path.exists(store.input_path("t-0123456789", "k" * 16))


def test_start_checks_the_digest_the_cli_sent(rig):
    wire = rig.wire()
    [(key, entry)] = wire["inputs"].items()
    entry["sha256"] = hashlib.sha256(b"different").hexdigest()[:16] + entry["sha256"][16:]
    from motel.tours.errors import Usage as U
    with pytest.raises(U, match="changed while"):
        rig.service.start(rig.alice, wire)
    assert rig.store.ids() == []                          # a half-staged tour is removed


def test_start_refuses_inputs_that_do_not_match_the_guests(rig):
    wire = rig.wire()
    wire["inputs"]["0" * 16] = {"kind": "bundle", "path": "/etc/passwd", "sha256": "0" * 64}
    with pytest.raises(Usage, match="do not match"):
        rig.service.start(rig.alice, wire)
    wire = rig.wire()
    for entry in wire["inputs"].values():
        entry["path"] = "/etc/shadow"
    with pytest.raises(Usage):
        rig.service.start(rig.alice, wire)
    assert rig.store.ids() == []


def test_start_takes_nothing_but_paths_and_the_desk_never_runs_what_it_reads(rig):
    wire = rig.wire()
    wire["spec"]["guests"][0]["repo"] = "/etc"
    with pytest.raises(Usage, match="unknown key"):
        rig.service.start(rig.alice, wire)
    assert json.dumps(rig.wire()).find("/etc/shadow") == -1


def test_a_disabled_tours_section_refuses_start(rig):
    rig.service.limits = rig.limits.__class__(**{**rig.limits.__dict__, "enabled": False})
    with pytest.raises(Refused, match="disabled"):
        rig.service.start(rig.alice, rig.wire())
