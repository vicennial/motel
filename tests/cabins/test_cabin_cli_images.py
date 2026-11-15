"""``motel cabin checkin|run --image`` and ``motel cabin images``: the client side, with a fake process runner."""
import io
import json
import os

import pytest
from cabin_fakes import pubkey

from motel.cabins import cli
from motel.receipts import cabin_receipt, render_cabin_receipt

LEASE = "swift-gull-0123456789ab"
NAME = "motel-" + LEASE
OFFER = {"server_type": "cpx32", "total_hourly_net_eur": "0.0318", "image_id": 4711, "image_name": "omnigent",
         "image_type": "snapshot"}
VIEW = {"lease_id": LEASE, "name": NAME, "state": "active", "address": "203.0.113.7", "host_key": pubkey("H"),
        "offer": OFFER, "expires_at": "2026-11-08T14:00:00Z",
        "ssh": {"user": "cabin", "host": "203.0.113.7", "port": 22, "host_key_alias": NAME, "host_key": pubkey("H")},
        "access": {"mode": "ssh-allowlist"}, "time_left_s": 7200}
IMAGES = [{"id": 4711, "name": "omnigent", "created": "2026-11-08T12:30:00Z", "image_size_gb": 1.5, "disk_size_gb": 40}]
OLD_DESK = {"ok": False, "code": 2, "error": "unrecognized arguments: --image omnigent", "hint": "motel cabin --help"}


class Proc:
    def __init__(self, rc=0, stdout=b""):
        self.returncode, self.stdout = rc, stdout


class Runner:
    def __init__(self, checkin=None, images=None, view=VIEW):
        self.calls, self.checkin, self.images, self.view = [], checkin, images, view

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        if argv[:2] == ["/usr/bin/sudo", "-n"]:
            verb = argv[4]
            if verb == "checkin":
                rc, doc = self.checkin or (0, {"ok": True, "cabin": self.view})
                return Proc(rc, json.dumps(doc).encode())
            if verb == "images":
                rc, doc = self.images or (0, {"schema": "motel/v1", "ok": True, "images": IMAGES})
                return Proc(rc, json.dumps(doc).encode() if doc is not None else b"")
            if verb == "checkout":
                done = dict(self.view, state="released", cost_estimate_net_eur="0.0318",
                            billing={"billed_hours": 1}, created_at="2026-11-08T12:00:00Z", released_at="2026-11-08T12:10:00Z")
                done["receipt"] = cabin_receipt(done)
                return Proc(0, json.dumps({"ok": True, "cabin": done}).encode())
        if argv[0] == "git":
            return Proc(0, b"a.py\0")
        return Proc(0)

    def desk_calls(self):
        return [a[4:] for a in self.calls if a[:2] == ["/usr/bin/sudo", "-n"]]


def main(tmp_path, argv, runner=None):
    runner = runner or Runner()
    out, err = io.StringIO(), io.StringIO()
    (tmp_path / "work").mkdir(parents=True, exist_ok=True)
    (tmp_path / "work" / "a.py").write_text("x")

    def make_key(directory):
        with open(os.path.join(directory, "id_ed25519"), "w") as handle:
            handle.write("PRIVATE")
        return pubkey("U")
    rc = cli.main(argv, runner=runner, root=str(tmp_path / "state"), make_key=make_key, out=out, err=err,
                  cwd=str(tmp_path / "work"), desk_exists=lambda: True)
    return rc, out.getvalue(), err.getvalue(), runner


def test_checkin_passes_the_image_to_the_desk_and_shows_it(tmp_path):
    rc, out, err, runner = main(tmp_path, ["checkin", "--image", "omnigent", "--task", "ci"])
    argv = runner.desk_calls()[0]
    assert rc == 0 and argv[:1] == ["checkin"] and argv[argv.index("--image") + 1] == "omnigent"
    assert out.strip() == LEASE and "image omnigent" in err


def test_checkin_json_carries_the_image_in_the_offer_and_the_receipt(tmp_path):
    view = dict(VIEW, receipt=cabin_receipt(dict(VIEW, created_at="2026-11-08T12:00:00Z")))
    rc, out, _err, _ = main(tmp_path, ["checkin", "--image", "omnigent", "--json"], Runner(view=view))
    doc = json.loads(out)
    assert rc == 0 and doc["offer"]["image_name"] == "omnigent" and doc["receipt"]["image"] == "omnigent"


def test_without_the_flag_no_image_goes_to_the_desk_and_the_stock_cabin_is_unchanged(tmp_path):
    stock = dict(VIEW, offer={"server_type": "cpx32", "total_hourly_net_eur": "0.0318", "image_name": "ubuntu-24.04",
                              "image_type": "system"})
    rc, _out, err, runner = main(tmp_path, ["checkin"], Runner(view=stock))
    assert rc == 0 and "--image" not in runner.desk_calls()[0] and "image " not in err


def test_run_passes_the_image_and_reports_it_in_the_json_line_and_the_receipt(tmp_path):
    rc, out, err, runner = main(tmp_path, ["run", "--image", "4711", "--json", "--", "true"])
    argv = runner.desk_calls()[0]
    assert rc == 0 and argv[argv.index("--image") + 1] == "4711"
    summary = json.loads(out.strip().splitlines()[-1])
    assert summary["image"] == "omnigent" and summary["receipt"]["image"] == "omnigent"
    assert "image omnigent" in err and "  image         omnigent" in err


def test_run_without_an_image_has_no_image_key(tmp_path):
    stock = dict(VIEW, offer={"server_type": "cpx32", "total_hourly_net_eur": "0.0318"})
    _rc, out, _err, _ = main(tmp_path, ["run", "--json", "--", "true"], Runner(view=stock))
    assert "image" not in json.loads(out.strip().splitlines()[-1])


@pytest.mark.parametrize("bad", ["Omnigent", "a b", "ubuntu-24.04", "x" * 41, "-x", "0123"])
def test_the_client_refuses_a_bad_image_text_before_it_calls_the_desk(tmp_path, bad):
    rc, _out, err, runner = main(tmp_path, ["checkin", "--image", bad])
    assert rc == 2 and "image" in err and runner.desk_calls() == []


def test_run_in_an_existing_cabin_does_not_take_an_image(tmp_path):
    rc, _out, err, runner = main(tmp_path, ["run", "--cabin", LEASE, "--image", "omnigent", "--", "true"])
    assert rc == 2 and "--image" in err and runner.desk_calls() == []


def test_an_old_desk_gives_a_clear_error_and_no_cabin(tmp_path):
    runner = Runner(checkin=(2, OLD_DESK))
    rc, out, err, _ = main(tmp_path, ["checkin", "--image", "omnigent"], runner)
    assert rc == 2 and out == "" and "older than this motel" in err and "does not know --image" in err
    assert "motel install --upgrade" in err and "no cabin was started" in err
    assert os.listdir(tmp_path / "state") == []                 # nothing kept: there is no cabin to keep


def test_an_old_desk_error_is_clear_in_json_too_and_run_stops_before_any_sync(tmp_path):
    runner = Runner(checkin=(2, OLD_DESK))
    rc, out, _err, runner = main(tmp_path, ["run", "--image", "omnigent", "--json", "--", "true"], runner)
    doc = json.loads(out)
    assert rc == 2 and doc["ok"] is False and "does not know --image" in doc["error"]
    assert runner.desk_calls()[0][0] == "checkin" and len(runner.desk_calls()) == 1
    assert not [a for a in runner.calls if a[0] in ("/usr/bin/rsync", "/usr/bin/ssh")]


def test_an_unrelated_checkin_refusal_keeps_its_own_message(tmp_path):
    runner = Runner(checkin=(3, {"ok": False, "error": "no baked image nope is available on this host",
                                 "hint": "motel cabin images", "code": 3}))
    rc, _out, err, _ = main(tmp_path, ["checkin", "--image", "nope"], runner)
    assert rc == 3 and "no baked image nope" in err and "older than" not in err


# ------------------------------------------------------------------------------------------------ cabin images

def test_images_prints_a_table_and_json(tmp_path):
    rc, out, _err, runner = main(tmp_path, ["images"])
    assert rc == 0 and runner.desk_calls() == [["images", "--json"]]
    assert out.startswith("omnigent") and "id 4711" in out and "1.5 GB" in out
    rc, out, _err, _ = main(tmp_path, ["images", "--json"])
    assert rc == 0 and json.loads(out)["images"] == IMAGES


def test_images_with_none_baked_says_how_an_operator_makes_one(tmp_path):
    runner = Runner(images=(0, {"schema": "motel/v1", "ok": True, "images": []}))
    rc, out, _err, _ = main(tmp_path, ["images"], runner)
    assert rc == 0 and "no baked images on this host" in out and "tour bake" in out


def test_images_on_a_desk_without_the_verb_says_to_upgrade(tmp_path):
    runner = Runner(images=(2, {"ok": False, "error": "argument verb: invalid choice: 'images'", "hint": "motel cabin --help"}))
    rc, _out, err, _ = main(tmp_path, ["images"], runner)
    assert rc == 2 and "older than this motel" in err and "no `images` verb" in err and "install --upgrade" in err


def test_images_when_sudo_refuses_the_new_verb_says_to_upgrade(tmp_path):
    runner = Runner(images=(1, None))               # sudo: the rule does not allow `cabin images`: no stdout at all
    rc, _out, err, _ = main(tmp_path, ["images"], runner)
    assert rc == 1 and "sudoers rule is older than this motel" in err and "install --upgrade" in err


def test_the_receipt_rows_name_the_image_only_when_there_is_one():
    stock = dict(VIEW, offer={"server_type": "cpx32", "total_hourly_net_eur": "0.0318", "image_type": "system",
                              "image_name": "ubuntu-24.04"}, created_at="2026-11-08T12:00:00Z")
    assert "image" not in cabin_receipt(stock)
    assert not [r for r in render_cabin_receipt(cabin_receipt(stock)) if "image" in r]
    baked = dict(stock, offer=OFFER)
    assert cabin_receipt(baked)["image"] == "omnigent"
    assert any("image" in r and "omnigent" in r for r in render_cabin_receipt(cabin_receipt(baked)))
