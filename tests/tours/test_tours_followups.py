"""Follow-ups after the engine review: git's process group, the rebuilt bundle's file checks, withheld bundles that fail the
guest, the tours CLEANUP mode, and the installer and doctor rules that go with it."""
import contextlib
import os
import signal
import time

import pytest

from motel.tours import bundle
from motel.tours.errors import Broken

# -- L1: git's whole process group ends with the command; the rebuilt file must be a lone file of the inspection user ----

class ShellGit(bundle._Git):
    """A _Git that runs a shell snippet in place of git, in the same session and pipe handling."""

    def __init__(self, scratch, script, ident=None):
        super().__init__(bundle.Sandbox(), str(scratch), ident)
        self.script = script

    def argv(self, args):
        return ["sh", "-c", self.script]


def alive(pid):
    try:
        with open(f"/proc/{pid}/stat") as handle:
            return handle.read().rsplit(")", 1)[1].split()[0] != "Z"          # a zombie is dead, only not reaped yet
    except OSError:
        return False


def wait_dead(pid, seconds=15.0):   # generous: a loaded CI runner can take seconds to deliver and reap
    until = time.monotonic() + seconds
    while alive(pid) and time.monotonic() < until:
        time.sleep(0.02)
    return not alive(pid)


def test_a_child_git_left_running_after_a_clean_exit_is_killed(tmp_path):
    git = ShellGit(tmp_path, "sleep 60 </dev/null >/dev/null 2>&1 & echo $! > pid; exit 0")
    code, _out, _err = git(["status"])
    assert code == 0
    pid = int((tmp_path / "pid").read_text())
    try:
        assert wait_dead(pid), "the forked child outlived the git command"
    finally:
        if alive(pid):
            with contextlib.suppress(ProcessLookupError):   # it may die between the check and the kill
                os.kill(pid, signal.SIGKILL)


def test_a_child_left_running_after_a_failing_command_is_killed_too(tmp_path):
    git = ShellGit(tmp_path, "sleep 60 </dev/null >/dev/null 2>&1 & echo $! > pid; exit 3")
    code, _out, _err = git(["status"])
    assert code == 3
    pid = int((tmp_path / "pid").read_text())
    try:
        assert wait_dead(pid)
    finally:
        if alive(pid):
            with contextlib.suppress(ProcessLookupError):   # it may die between the check and the kill
                os.kill(pid, signal.SIGKILL)


def test_copy_out_copies_a_lone_regular_file_owned_by_the_inspection_user(tmp_path):
    src = tmp_path / "deliver.bundle"
    src.write_bytes(b"# v2 git bundle\nx")
    git = ShellGit(tmp_path, "true", ident=(os.geteuid(), os.getegid()))
    info = git.copy_out(str(src), str(tmp_path / "out"))
    assert info["bytes"] == len(b"# v2 git bundle\nx") and (tmp_path / "out").read_bytes() == src.read_bytes()


def test_copy_out_refuses_a_file_with_a_second_hard_link(tmp_path):
    src = tmp_path / "deliver.bundle"
    src.write_bytes(b"# v2 git bundle\nx")
    os.link(src, tmp_path / "other-name")
    git = ShellGit(tmp_path, "true")
    with pytest.raises(Broken, match="hard link"):
        git.copy_out(str(src), str(tmp_path / "out"))
    assert not (tmp_path / "out").exists()


def test_copy_out_refuses_a_file_that_the_inspection_user_does_not_own(tmp_path):
    src = tmp_path / "deliver.bundle"
    src.write_bytes(b"# v2 git bundle\nx")
    git = ShellGit(tmp_path, "true", ident=(os.geteuid() + 1, os.getegid()))
    with pytest.raises(Broken, match="not owned by the inspection user"):
        git.copy_out(str(src), str(tmp_path / "out"))
    assert not (tmp_path / "out").exists()


def test_without_a_dedicated_user_the_owner_must_be_the_desk_user_itself(tmp_path):
    src = tmp_path / "deliver.bundle"
    src.write_bytes(b"# v2 git bundle\nx")
    assert ShellGit(tmp_path, "true", ident=None).copy_out(str(src), str(tmp_path / "out"))["bytes"] == len(b"# v2 git bundle\nx")


# -- L2: a rebuilt bundle over the size cap fails the guest ----------------------------------------------------------------

def test_a_rebuilt_bundle_over_the_size_cap_is_withheld_and_fails_the_guest(rig):
    over = rig.env.limits.max_result_bytes + 1

    def inspect(base_bundle, result_bundle, base_sha, branch, attempt, token=None, deliver_to=None):
        with open(deliver_to, "wb") as handle:
            handle.write(b"# v2 git bundle\nsmall")
        return {"head": "b" * 40, "changed_files": [], "changed_total": 0, "ahead": True, "token_found": False,
                "delivered": {"bytes": over, "sha256": "0" * 64}}
    rig.env.inspect_bundle = inspect
    doc = rig.run_tour()
    record = rig.result_record(doc["id"], "fix-auth")
    assert doc["status"] != "done" and record["status"] == "failed"
    assert "over the size limit" in record["error"]
    assert any("over the size limit" in v for v in record.get("violations", []))
    folder = os.path.join(rig.store.results_dir(doc["id"], "fix-auth"), record["attempt"])
    assert not any(n.endswith(".rebuilt") for n in os.listdir(folder))


# -- L3: a disabled [tours] stops new attempts, running ones finish ------------------------------------------------------------

TWO = [{"id": "a", "prompt": "do a", "secrets": ["anthropic"], "budget_usd": 0.5},
       {"id": "b", "prompt": "do b", "secrets": ["anthropic"], "budget_usd": 0.5}]


def test_queued_guests_are_cancelled_with_tours_disabled_and_no_cabin_is_booked(rig):
    rig.env.accepting = lambda: False
    doc = rig.run_tour(TWO)
    assert {g["status"] for g in doc["guests"].values()} == {"cancelled"} and doc["status"] != "done"
    assert all(g["reason"] == "tours_disabled" for g in doc["guests"].values())
    assert rig.servers_left() == [] and "checkin" not in rig.log.kinds() and "issue" not in rig.log.kinds()
    record = rig.result_record(doc["id"], "a")
    assert record["status"] == "cancelled" and record["reason"] == "tours_disabled"
    view = rig.service.status(rig.alice, doc["id"])["tour"]
    assert [g["reason"] for g in view["guests"]] == ["tours_disabled", "tours_disabled"]


def test_an_attempt_that_already_runs_finishes_and_only_the_queued_guest_is_cancelled(rig):
    state = {"disabled": False}
    rig.env.accepting = lambda: not state["disabled"]
    original = rig.transport.preflight

    def preflight(*args, **kw):
        state["disabled"] = True                    # the operator disables tours while guest a already has its cabin
        return original(*args, **kw)
    rig.transport.preflight = preflight
    doc = rig.run_tour(TWO)
    assert doc["guests"]["a"]["status"] == "done" and "reason" not in doc["guests"]["a"]
    assert doc["guests"]["b"]["status"] == "cancelled" and doc["guests"]["b"]["reason"] == "tours_disabled"
    assert rig.servers_left() == []


def test_the_retry_of_a_guest_is_not_started_either(rig):
    state = {"disabled": False}
    rig.env.accepting = lambda: not state["disabled"]
    rig.transport.fail_preflight = 1                # the first attempt fails before the command: a retry would follow
    original = rig.transport.preflight

    def preflight(*args, **kw):
        state["disabled"] = True
        return original(*args, **kw)
    rig.transport.preflight = preflight
    doc = rig.run_tour()
    guest = doc["guests"]["fix-auth"]
    assert guest["status"] == "cancelled" and guest["reason"] == "tours_disabled"


def test_an_enabled_config_lets_new_attempts_start(rig):
    rig.env.accepting = lambda: True
    assert rig.run_tour()["status"] == "done"


CABINS_ON = "[cabins]\nenabled = true\n"


@pytest.mark.parametrize("text,expected", [
    ("[tours]\nenabled = true\n" + CABINS_ON, None),
    ("[tours]\nenabled = false\n" + CABINS_ON, "tours_disabled"),
    ("[rooms]\nx = 1\n" + CABINS_ON, "tours_disabled"),                                      # no [tours] table
    ("[tours]\nenabled = \"yes\"\n" + CABINS_ON, "tours_disabled"),
    ("[tours]\nenabled = true\nmax_guests_per_tour = 0\n" + CABINS_ON, None),             # an invalid table is not "disabled"
    ("[tours]\nenabled = true\n", "cabins_disabled"),                                       # N2: tours run on cabins
    ("[tours]\nenabled = true\n[cabins]\nenabled = false\n", "cabins_disabled"),
    ("[tours]\nenabled = true\n[cabins]\nenabled = \"yes\"\n", "cabins_disabled"),
    ("[tours]\nenabled = false\n[cabins]\nenabled = false\n", "tours_disabled"),           # tours first
    ("this is = = not toml", None),                                                          # cannot be parsed now: keep the work
])
def test_new_attempts_follow_the_raw_flags_and_ignore_a_config_that_cannot_be_parsed(tmp_path, text, expected):
    from motel.tours.config import new_attempts_allowed, new_attempts_block
    path = tmp_path / "motel.toml"
    path.write_text(text)
    assert new_attempts_block(str(path)) == expected
    assert new_attempts_allowed(str(path)) is (expected is None)


def test_a_missing_config_file_stops_new_attempts_but_another_read_error_does_not(tmp_path):
    from motel.tours.config import new_attempts_allowed, new_attempts_block
    assert new_attempts_allowed(str(tmp_path / "missing.toml")) is False                 # N1: FileNotFoundError
    assert new_attempts_block(str(tmp_path / "missing.toml")) == "tours_disabled"
    assert new_attempts_allowed(str(tmp_path)) is True                                   # a directory: IsADirectoryError, unknown
    path = tmp_path / "unreadable.toml"
    path.write_text("[tours]\nenabled = false\n")
    path.chmod(0)
    try:
        if os.geteuid() != 0:                                                            # permission denied: unknown, keep the work
            assert new_attempts_allowed(str(path)) is True
    finally:
        path.chmod(0o600)


def test_guests_are_cancelled_with_cabins_disabled_when_the_cabins_are_off(rig, tmp_path):
    from motel.tours.config import new_attempts_allowed, new_attempts_block
    config = tmp_path / "motel.toml"
    config.write_text("[tours]\nenabled = true\n[cabins]\nenabled = false\n")
    rig.env.accepting = lambda: new_attempts_allowed(str(config))
    rig.env.block_reason = lambda: new_attempts_block(str(config))
    doc = rig.run_tour(TWO)
    assert {g["status"] for g in doc["guests"].values()} == {"cancelled"}
    assert all(g["reason"] == "cabins_disabled" for g in doc["guests"].values())
    assert rig.servers_left() == [] and "issue" not in rig.log.kinds()
    assert rig.result_record(doc["id"], "a")["reason"] == "cabins_disabled"


def test_a_missing_config_cancels_queued_guests_with_tours_disabled(rig, tmp_path):
    from motel.tours.config import new_attempts_allowed, new_attempts_block
    missing = str(tmp_path / "gone.toml")
    rig.env.accepting = lambda: new_attempts_allowed(missing)
    rig.env.block_reason = lambda: new_attempts_block(missing)
    doc = rig.run_tour()
    assert doc["guests"]["fix-auth"]["status"] == "cancelled" and doc["guests"]["fix-auth"]["reason"] == "tours_disabled"


def test_the_production_env_asks_the_config_file_each_time(tmp_path):
    from datetime import UTC, datetime

    from motel.cabins.desk import CabinPaths
    from motel.tours.desk import make_env_factory
    from motel.tours.state import TourStore
    paths = CabinPaths(root=str(tmp_path / "root"))
    os.makedirs(os.path.dirname(paths.config_file), exist_ok=True)
    clock = lambda: datetime(2026, 11, 8, tzinfo=UTC)               # noqa: E731
    store = TourStore(str(tmp_path / "tours"), clock)
    from motel.tours.config import Limits
    env = make_env_factory(paths, store, Limits(enabled=True), None, clock, {}, transport=object(),
                           concierge_factory=lambda: object())()
    with open(paths.config_file, "w") as handle:
        handle.write("[tours]\nenabled = true\n[cabins]\nenabled = true\n")
    assert env.accepting() is True and env.block_reason() is None
    with open(paths.config_file, "w") as handle:
        handle.write("[tours]\nenabled = false\n")
    assert env.accepting() is False and env.block_reason() == "tours_disabled"
    with open(paths.config_file, "w") as handle:
        handle.write("[tours]\nenabled = true\n")
    assert env.accepting() is False and env.block_reason() == "cabins_disabled"
