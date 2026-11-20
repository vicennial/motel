"""``motel-desk cabin checkin --image`` and ``cabin images``: what a user may start from, checked at the root desk."""
import copy
import io
import json
import os

import pytest
from cabin_fakes import HOST, FakeHetzner, World, fake_keygen, pubkey, receipt

from motel import installer
from motel.cabins import desk
from motel.cabins.config import CabinPaths
from motel.cabins.controller import Controller
from motel.cabins.desk import Caller
from motel.cabins.provider import GuardedApi
from motel.cabins.util import sha256_text
from motel.tours import bake

TOKEN = "hcloud-token-abcdefghijklmnop"
TOML = """\
[host]
allow_users = ["alice"]

[cabins]
enabled = true
types = ["cpx32"]
max_eur_per_hour = 0.2
"""
OWN = {"managed-by": "motel", "role": "tour-image", "controller": str(HOST)}


class ImageHetzner(FakeHetzner):
    """The cabins fake plus snapshots, listed by label selector like the real API."""

    def __init__(self, clock):
        super().__init__(clock)
        self.images = {}

    def _get(self, path):
        base, _, query = path.partition("?")
        if base.strip("/") == "images" and "label_selector" in query:
            return self._list("images", self.images, query)
        return super()._get(path)

    def add_image(self, name, *, labels=None, status="available", recorded_in=None, kind="snapshot"):
        iid = self._id()
        merged = dict(OWN, **{"image-name": name}) if labels is None else dict(labels)
        self.images[iid] = {"id": iid, "type": kind, "architecture": "x86", "status": status, "deprecated": None,
                            "description": "motel tour image " + name, "labels": merged, "image_size": 1.5,
                            "disk_size": 40, "created": "2026-11-08T12:30:00Z", "protection": {"delete": False}}
        if recorded_in is not None:
            recorded_in.record(iid, name)
        return iid


class Env:
    def __init__(self, tmp_path):
        self.root = str(tmp_path / "root")
        self.paths = CabinPaths(self.root)
        self.w = World(str(tmp_path / "w"))
        self.w.hz = ImageHetzner(self.w.clock)
        os.makedirs(os.path.dirname(self.paths.config_file))
        with open(self.paths.config_file, "w") as handle:
            handle.write(TOML)
        token_path = self.paths.real("/etc/motel/credentials/cabins.token")
        os.makedirs(os.path.dirname(token_path), exist_ok=True)
        with open(token_path, "w") as handle:
            handle.write(TOKEN + "\n")
        doc = receipt()
        doc["hetzner"]["token_sha256"] = sha256_text(TOKEN)
        with open(self.paths.receipt_file, "w") as handle:
            json.dump(doc, handle)
        self.records = {}

    def record(self, image_id, name):
        self.records[str(image_id)] = {"name": name, "lease": "bake-0123456789ab", "recorded_at": "2026-11-08T12:31:00Z"}
        os.makedirs(self.paths.state_dir, exist_ok=True)
        with open(os.path.join(self.paths.state_dir, bake.IMAGES_FILE), "w") as handle:
            json.dump({"schema": "motel/tour-images/v1", "images": self.records, "intents": {}}, handle)

    def baked(self, name, **kw):
        return self.w.hz.add_image(name, recorded_in=self, **kw)

    def call(self, *argv, uid=1000, name="alice", **hooks):
        out, err = io.StringIO(), io.StringIO()
        wiring = {"hetzner_factory": lambda _t, h: GuardedApi(self.w.hz, h), "guest": self.w.guest,
                  "fetch": self.w.meta.__getitem__, "clock": self.w.clock, "sleep": self.w.clock.sleep,
                  "keygen": fake_keygen, "groups_fn": lambda _c: set()}
        wiring.update(hooks)
        rc = desk.run(list(argv), paths=self.paths, caller=Caller(uid=uid, gid=uid, name=name), out=out, err=err, **wiring)
        return rc, out.getvalue(), err.getvalue()

    def checkin(self, *extra, **kw):
        return self.call("checkin", "--task", "build", "--client-key", pubkey("U"), "--json", *extra, **kw)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def boots(env):
    return [w for w in env.w.hz.writes if w[0] == "POST" and w[1] == "/servers"]


# ------------------------------------------------------------------------------------------------ a valid image

def test_a_baked_image_by_name_boots_the_cabin_from_that_snapshot(env):
    iid = env.baked("omnigent")
    rc, out, _ = env.checkin("--image", "omnigent")
    cabin = json.loads(out)["cabin"]
    assert rc == 0 and len(boots(env)) == 1 and boots(env)[0][2]["image"] == iid
    assert cabin["offer"]["image_id"] == iid and cabin["offer"]["image_name"] == "omnigent"
    assert cabin["offer"]["image_type"] == "snapshot" and cabin["receipt"]["image"] == "omnigent"


def test_a_baked_image_by_id_works_and_the_stock_cabin_names_no_image(env):
    iid = env.baked("omnigent")
    rc, out, _ = env.checkin("--image", str(iid))
    assert rc == 0 and json.loads(out)["cabin"]["offer"]["image_name"] == "omnigent"
    rc, out, _ = env.checkin()
    cabin = json.loads(out)["cabin"]
    assert rc == 0 and "image" not in cabin["receipt"] and cabin["offer"]["image_type"] == "system"


def test_the_controller_gets_the_numeric_id_not_the_users_text(env, monkeypatch):
    iid = env.baked("omnigent")
    seen = {}

    def spy(self, *args, **kw):
        seen.update(kw)
        return {"lease_id": "x"}
    monkeypatch.setattr(Controller, "checkin", spy)
    rc, _out, _ = env.checkin("--image", "omnigent")
    assert rc == 0 and seen["image"] == iid and isinstance(seen["image"], int)


def test_the_human_output_names_the_image(env):
    env.baked("omnigent")
    rc, out, _ = env.call("checkin", "--task", "build", "--client-key", pubkey("U"), "--image", "omnigent")
    assert rc == 0 and "image: omnigent (baked on this host" in out


# ------------------------------------------------------------------------------------------------ refusals

@pytest.mark.parametrize("bad", ["Omnigent", "has space", "a;b", "../x", "-x", "x-", "ubuntu-24.04", "a" * 41, "0123", "x_y",
                                 "99999999999999999999", "ünï", ""])
def test_a_bad_image_text_is_a_usage_error_before_any_cloud_call(env, bad):
    env.baked("omnigent")
    rc, out, _ = env.checkin("--image", bad)
    assert rc == 2 and json.loads(out)["ok"] is False
    assert env.w.hz.reads == [] and env.w.hz.writes == []


def test_an_unknown_image_is_refused_and_nothing_is_booted(env):
    env.baked("omnigent")
    rc, out, _ = env.checkin("--image", "nothing-here")
    assert rc == 3 and "no baked image nothing-here" in json.loads(out)["error"] and boots(env) == []
    rc, out, _ = env.checkin("--image", "424242")
    assert rc == 3 and boots(env) == []


def test_a_snapshot_without_this_hosts_labels_is_never_selectable(env):
    foreign = env.w.hz.add_image("theirs", labels={"image-name": "theirs"})
    other_host = env.w.hz.add_image("other", labels=dict(OWN, controller="999", **{"image-name": "other"}))
    other_role = env.w.hz.add_image("notours", labels={"managed-by": "motel", "role": "backup", "controller": str(HOST),
                                                       "image-name": "notours"})
    for spec in ("theirs", "other", "notours", str(foreign), str(other_host), str(other_role)):
        rc, _out, _ = env.checkin("--image", spec)
        assert rc == 3, spec
    assert boots(env) == []


def test_a_labelled_snapshot_this_host_never_recorded_is_refused(env):
    unrecorded = env.w.hz.add_image("stray")
    for spec in ("stray", str(unrecorded)):
        rc, out, _ = env.checkin("--image", spec)
        assert rc == 3 and "no baked image" in json.loads(out)["error"]
    assert boots(env) == []


def test_a_labelled_but_not_available_image_is_refused(env):
    env.baked("half", status="creating")
    rc, _out, _ = env.checkin("--image", "half")
    assert rc == 3 and boots(env) == []


def test_a_system_image_is_never_selectable_by_name_or_id(env):
    # the configured base image has a name with a dot, a system image's numeric id is the cloud's own id
    for spec in ("ubuntu-24.04", "161547269", "debian-12", "ubuntu"):
        rc, _out, _ = env.checkin("--image", spec)
        assert rc in (2, 3), spec
    assert boots(env) == []


def test_a_system_image_that_carries_our_labels_is_still_not_a_snapshot_choice(env):
    sneaky = env.w.hz.add_image("sneaky", kind="system")
    env.record(sneaky, "sneaky")
    rc, _out, _ = env.checkin("--image", "sneaky")
    assert rc == 3 and boots(env) == []


def test_an_ambiguous_name_is_refused_and_the_id_still_works(env):
    first, second = env.baked("omnigent"), env.baked("omnigent")
    rc, out, _ = env.checkin("--image", "omnigent")
    error = json.loads(out)["error"]
    assert rc == 3 and "more than one image is named omnigent" in error and str(first) in error and str(second) in error
    assert boots(env) == []
    rc, _out, _ = env.checkin("--image", str(second))
    assert rc == 0 and boots(env)[0][2]["image"] == second


def test_a_user_outside_allow_users_cannot_use_an_image(env):
    env.baked("omnigent")
    rc, out, _ = env.checkin("--image", "omnigent", uid=1001, name="mallory")
    assert rc == 3 and "allow_users" in json.loads(out)["error"] and boots(env) == []
    rc, _out, _ = env.call("images", "--json", uid=1001, name="mallory")
    assert rc == 3


def test_any_allowed_user_may_use_any_image_baked_on_this_host(env):
    # The decision recorded in docs/cabins.md: images are the operator's, there is no per-user list.
    env.baked("omnigent")
    with open(env.paths.config_file, "w") as handle:
        handle.write(TOML.replace('["alice"]', '["alice", "bob"]'))
    rc, _out, _ = env.checkin("--image", "omnigent", uid=1002, name="bob")
    assert rc == 0


# ------------------------------------------------------------------------------------------------ cabin images

def test_images_lists_only_available_recorded_images_of_this_host(env):
    first = env.baked("omnigent")
    env.baked("builder")
    env.baked("half", status="creating")
    env.w.hz.add_image("stray")
    env.w.hz.add_image("theirs", labels={"image-name": "theirs"})
    rc, out, _ = env.call("images", "--json")
    doc = json.loads(out)
    assert rc == 0 and doc["ok"] is True
    assert [i["name"] for i in doc["images"]] == ["builder", "omnigent"]
    row = next(i for i in doc["images"] if i["id"] == first)
    assert row == {"id": first, "name": "omnigent", "created": "2026-11-08T12:30:00Z", "image_size_gb": 1.5,
                   "disk_size_gb": 40}


def test_images_human_output_and_the_empty_case(env):
    rc, out, _ = env.call("images")
    assert rc == 0 and "no baked images on this host" in out
    iid = env.baked("omnigent")
    rc, out, _ = env.call("images")
    assert rc == 0 and out.startswith("omnigent") and f"id {iid}" in out and "1.5 GB" in out


def test_images_is_read_only(env):
    env.baked("omnigent")
    env.w.hz.writes.clear()
    env.call("images", "--json")
    assert env.w.hz.writes == []


def test_user_images_never_modifies_the_provider_rows(env):
    env.baked("omnigent")
    before = copy.deepcopy(env.w.hz.images)
    bake.user_images(env.w.controller(uid=0))
    assert env.w.hz.images == before


# ------------------------------------------------------------------------------------------------ the sudoers rule

def test_the_sudoers_rule_pins_images_and_keeps_checkin_args_open():
    rule = installer.render_sudoers(["alice"], cabins=installer.CABINS_ON)
    assert "/usr/local/sbin/motel-desk cabin images *" in rule
    assert "/usr/local/sbin/motel-desk cabin checkin *" in rule          # --image needs no new rule
    cleanup = installer.render_sudoers(["alice"], cabins=installer.CABINS_CLEANUP)
    assert "cabin images" not in cleanup and "cabin checkin" not in cleanup
    assert "cabin images" not in installer.render_sudoers(["alice"])
