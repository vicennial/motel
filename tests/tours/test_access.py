"""Results belong to the user who started the tour. The state directory is root-only; users read through desk verbs."""
import json
import os
import stat

import pytest

from motel.tours.errors import Incomplete, NotFound, Usage

TOKENS = []


def finished(rig):
    return rig.run_tour(artifacts=["reports/*.json"])


def test_the_owner_reads_result_logs_and_bytes(rig):
    doc = finished(rig)
    tid = doc["id"]
    result = rig.service.result(rig.alice, tid, "fix-auth")
    assert result["result"]["runner_reported"]["exit_code"] == 0 and "UNTRUSTED" not in json.dumps(result["result"]["verified"])
    assert "guest_claims" in result["result"] and "untrusted" in result["note"]
    logs = rig.service.logs(rig.alice, tid, "fix-auth", "stdout")
    assert logs["untrusted"] is True and "agent says hello" in logs["text"]
    raw = rig.service.fetch(rig.alice, tid, "fix-auth")
    assert raw.data.startswith(b"# v2 git bundle")
    raw = rig.service.fetch(rig.alice, tid, "fix-auth", "reports/a.json")
    assert raw.data == b'{"ok": true}'


@pytest.mark.parametrize("verb", ["status", "result", "logs", "fetch", "cancel", "wait"])
def test_another_user_gets_not_found_for_every_verb(rig, verb):
    tid = finished(rig)["id"]
    calls = {"status": lambda c: rig.service.status(c, tid), "result": lambda c: rig.service.result(c, tid, "fix-auth"),
             "logs": lambda c: rig.service.logs(c, tid, "fix-auth"), "fetch": lambda c: rig.service.fetch(c, tid, "fix-auth"),
             "cancel": lambda c: rig.service.cancel(c, tid), "wait": lambda c: rig.service.wait(c, tid)}
    with pytest.raises(NotFound) as err:
        calls[verb](rig.bob)
    assert tid in err.value.message and "owner" not in err.value.message.lower()   # the same answer as for a tour that is not there
    with pytest.raises(NotFound):
        rig.service.status(rig.bob, "t-0000000000")
    calls[verb](rig.alice)                                                          # the owner is fine


def test_root_may_read_any_tour_and_ls_lists_only_your_own(rig):
    tid = finished(rig)["id"]
    assert rig.service.status(rig.root, tid)["tour"]["id"] == tid
    assert [t["id"] for t in rig.service.ls(rig.alice)["tours"]] == [tid]
    assert rig.service.ls(rig.bob)["tours"] == []
    assert [t["id"] for t in rig.service.ls(rig.root, all_=True)["tours"]] == [tid]


def test_files_on_disk_are_owner_only(rig):
    tid = finished(rig)["id"]
    modes = []
    for base in (rig.store.path(tid),):
        for root, dirs, files in os.walk(base):
            modes += [stat.S_IMODE(os.stat(os.path.join(root, n)).st_mode) for n in files + dirs]
    assert modes and all(m & 0o077 == 0 for m in modes)


def test_a_guest_without_a_result_is_incomplete_not_empty(rig):
    tid = rig.start()
    with pytest.raises(Incomplete):
        rig.service.result(rig.alice, tid, "fix-auth")
    with pytest.raises(NotFound):
        rig.service.result(rig.alice, tid, "no-such-guest")


def test_artifact_names_cannot_escape_the_results_directory(rig):
    tid = finished(rig)["id"]
    for bad in ("../result.json", "/etc/passwd", "a/../../b", ""):
        with pytest.raises((Usage, NotFound)):
            rig.service.fetch(rig.alice, tid, "fix-auth", bad)


def test_a_symlink_planted_in_the_results_is_not_followed(rig, tmp_path):
    tid = finished(rig)["id"]
    secret = tmp_path / "secret.txt"
    secret.write_text("hunter2")
    base = os.path.join(rig.store.results_dir(tid, "fix-auth"), rig.store.load(tid)["guests"]["fix-auth"]["result"]["attempt"])
    os.symlink(str(secret), os.path.join(base, "artifacts", "reports", "link.json"))
    with pytest.raises(NotFound):
        rig.service.fetch(rig.alice, tid, "fix-auth", "reports/link.json")


def test_logs_are_capped_and_stripped_of_control_characters(rig):
    def guest(card, files):
        from doubles import default_guest
        out, status = default_guest(card, files)
        out["stdout.log"] = b"A" * 5000 + b"\x1b[31mred\x07\n"
        return out, status
    rig.transport.guest = guest
    tid = rig.run_tour()["id"]
    logs = rig.service.logs(rig.alice, tid, "fix-auth", "stdout", tail=100)
    assert logs["truncated"] is True and len(logs["text"]) <= 100 and "\x1b" not in logs["text"] and "\x07" not in logs["text"]
