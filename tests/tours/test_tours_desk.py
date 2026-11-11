"""The `motel-desk tour` verbs: parsing, operator-only verbs, exit codes, raw replies, the wait contract."""
import io
import json

import doubles
import pytest

from motel.cabins.config import CabinPaths
from motel.tours import desk as tour_desk
from motel.tours import slots
from platform_marks import linux_only


def desk(rig, argv, caller=None, stdin=b"", **hooks):
    out, err = io.StringIO(), io.StringIO()
    out.buffer = io.BytesIO()                      # RawReply writes bytes here
    code = tour_desk.run(argv, paths=CabinPaths(root=str(rig.tmp / "root")), caller=caller or rig.alice, out=out, err=err,
                         service=rig.service, limits=rig.limits, stdin=io.BytesIO(stdin), **hooks)
    return code, out, err


def doc(out):
    return json.loads(out.getvalue())


def test_start_reads_the_request_from_stdin_and_status_ls_work(rig):
    code, out, _ = desk(rig, ["start", "--json"], stdin=json.dumps(rig.wire()).encode())
    assert code == 0 and doc(out)["ok"] is True
    tid = doc(out)["tour"]["id"]
    assert rig.launched == [tid]
    code, out, _ = desk(rig, ["status", tid])
    assert code == 0 and doc(out)["tour"]["status"] == "queued"
    code, out, _ = desk(rig, ["ls"])
    assert [t["id"] for t in doc(out)["tours"]] == [tid]


def test_an_unreadable_request_is_a_usage_error(rig):
    code, _out, err = desk(rig, ["start"], stdin=b"{nope")
    assert code == 2 and "unreadable" in err.getvalue()


@pytest.mark.parametrize("argv", [["supervise", "t-1"], ["reap"], ["bake", "img", "--script", "/x"], ["image", "ls"],
                                  ["own-login", "status"]])
def test_operator_verbs_are_refused_to_users(rig, argv):
    code, _out, err = desk(rig, argv)
    assert code == 5 or "operator command" in err.getvalue()
    assert "operator command" in err.getvalue()


def test_root_may_run_operator_verbs(rig):
    code, out, _ = desk(rig, ["reap", "--json"], caller=rig.root)
    assert code == 0 and doc(out)["applied"] is False
    code, out, _ = desk(rig, ["own-login", "set", "--stdin", "--json"], caller=rig.root, stdin=(doubles.OWN_TOKEN + "\n").encode())
    assert code == 0 and doc(out)["own_login"]["present"] is True and doubles.OWN_TOKEN not in json.dumps(doc(out))
    code, out, _ = desk(rig, ["own-login", "status"], caller=rig.root)
    assert doc(out)["own_login"]["present"] is True
    code, out, _ = desk(rig, ["own-login", "clear"], caller=rig.root)
    assert doc(out)["own_login"]["present"] is False


def test_images_go_through_the_guest_part_with_a_controller(rig, monkeypatch):
    calls = []

    class Bake:
        @staticmethod
        def bake(ctl, script, name, **kw):
            calls.append(("bake", script, name, kw))
            return {"name": name}

        @staticmethod
        def images(ctl):
            calls.append(("images",))
            return []

        @staticmethod
        def delete(ctl, image):
            calls.append(("delete", image))
            return image
    monkeypatch.setattr(tour_desk, "build_controller", lambda *a, **k: object())
    code, out, _ = desk(rig, ["bake", "img", "--script", "/srv/setup.sh", "--type", "cpx32", "--json"], caller=rig.root, bake=Bake)
    assert code == 0 and calls[0] == ("bake", "/srv/setup.sh", "img", {"server_type": "cpx32"})
    desk(rig, ["image", "ls"], caller=rig.root, bake=Bake)
    desk(rig, ["image", "rm", "4711"], caller=rig.root, bake=Bake)
    code, _out, err = desk(rig, ["image", "rm", "img-1"], caller=rig.root, bake=Bake)
    assert code == 2 and "positive integer" in err.getvalue()
    assert calls[1:] == [("images",), ("delete", 4711)]


def test_image_ls_shows_which_images_this_host_recorded_and_rm_explains_an_unrecorded_one(rig, monkeypatch):
    from motel.cabins.errors import Guarded, PolicyRefused

    class Bake:
        @staticmethod
        def images(ctl):
            return [{"id": 1, "recorded": True, "name": "mine"}, {"id": 2, "recorded": False, "name": "other"}]

        @staticmethod
        def delete(ctl, image):
            raise Guarded(f"image {image} has no creation record on this host; refusing to delete it")

        @staticmethod
        def bake(ctl, script, name, **kw):
            raise PolicyRefused("tour images are not baked in tailscale mode", "ssh-allowlist")
    monkeypatch.setattr(tour_desk, "build_controller", lambda *a, **k: object())
    code, out, _ = desk(rig, ["image", "ls", "--json"], caller=rig.root, bake=Bake)
    assert code == 0 and [(i["id"], i["recorded"]) for i in doc(out)["images"]] == [(1, True), (2, False)]
    code, out, err = desk(rig, ["image", "rm", "2", "--json"], caller=rig.root, bake=Bake)
    assert code != 0
    text = (out.getvalue() + err.getvalue())
    assert "not baked on this host" in text and "recorded: false" in text and "provider console" in text
    code, out, err = desk(rig, ["bake", "img", "--script", "/srv/setup.sh", "--json"], caller=rig.root, bake=Bake)
    assert code == PolicyRefused.code and "tailscale" in out.getvalue() + err.getvalue()


def test_wait_exits_9_when_the_timeout_comes_first_and_0_when_done(rig):
    tid = rig.start()
    code, out, _ = desk(rig, ["wait", tid, "--timeout", "1", "--json"])
    assert code == 9 and doc(out)["satisfied"] is False
    rig.supervise(tid)
    code, out, _ = desk(rig, ["wait", tid, "--any", "--timeout", "1"])
    assert code == 0 and doc(out)["satisfied"] is True


def test_fetch_writes_the_raw_bytes_and_result_and_logs_are_json(rig):
    tid = rig.run_tour()["id"]
    code, out, _ = desk(rig, ["fetch", tid, "fix-auth"])
    assert code == 0 and out.buffer.getvalue().startswith(b"# v2 git bundle")
    code, out, _ = desk(rig, ["result", tid, "fix-auth"])
    assert doc(out)["result"]["runner_reported"]["exit_code"] == 0
    code, out, _ = desk(rig, ["logs", tid, "fix-auth", "--stream", "stderr", "--tail", "10"])
    assert code == 0 and doc(out)["untrusted"] is True


@linux_only
def test_a_user_outside_allow_users_is_refused(rig):
    tid = rig.start()
    code, _out, err = desk(rig, ["status", tid], caller=rig.bob)
    assert code != 0 and "not in allow_users" in err.getvalue()


def test_unknown_verbs_and_options_are_usage_errors_not_tracebacks(rig):
    for argv in (["bogus"], ["status"], ["wait", "t-1", "--any", "--all"], ["logs", "t-1", "g", "--stream", "x"], ["start", "--shell"]):
        code, _out, err = desk(rig, argv)
        assert code == 2 and "Traceback" not in err.getvalue()


def test_the_systemd_launcher_runs_a_transient_unit_with_a_fixed_command():
    calls = []

    class Done:
        returncode = 0
        stderr = ""
    launch = tour_desk.systemd_launcher("/usr/local/sbin/motel-desk", runner=lambda argv, **kw: calls.append((argv, kw)) or Done())
    assert launch("t-abc") == "motel-tour-t-abc"
    argv = calls[0][0]
    assert argv[0].endswith("systemd-run") and argv[1:3] == ["--unit", "motel-tour-t-abc"]
    assert argv[-4:] == ["/usr/local/sbin/motel-desk", "tour", "supervise", "t-abc"]
    assert tour_desk.systemd_unit_active(runner=lambda argv, **kw: type("R", (), {"returncode": 0})())("t-abc") is True
    assert tour_desk.systemd_unit_active(runner=lambda argv, **kw: type("R", (), {"returncode": 3})())("t-abc") is False


def test_slots_are_exclusive_and_released_on_close(tmp_path):
    pool = slots.SlotPool(str(tmp_path / "s"), 2, sleep=lambda s: None)
    a, b = pool.try_acquire(), pool.try_acquire()
    assert a and b and pool.try_acquire() is None
    a.release()
    c = pool.try_acquire()
    assert c is not None and pool.acquire(lambda: True) is None
    for held in (b, c):
        held.release()
