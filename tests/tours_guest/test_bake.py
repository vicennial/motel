"""Image bake against the cabins fake provider: cleanup runs, credentials are refused, the server is always deleted."""
import copy
import os
import subprocess

import pytest
from cabin_fakes import HOST, FakeHetzner, World, fake_keygen

from motel.cabins.errors import ApiError, Fail, Guarded, GuardRefused, Incomplete, PolicyRefused, Unavailable, Usage
from motel.tours import bake
from platform_marks import linux_only

GOOD_FINALIZE = (0, b"MOTEL_BAKE_CLEAN\n")


class ImageHetzner(FakeHetzner):
    """The cabins Hetzner fake plus images and server power state."""

    def __init__(self, clock):
        super().__init__(clock)
        self.images = {}
        self.image_polls_until_available = 1
        self.image_final_status = "available"

    def _get(self, path):
        base, _, query = path.partition("?")
        parts = base.strip("/").split("/")
        if parts[0] == "images" and (len(parts) == 2 or "role%3Dtour-image" in query):
            if len(parts) == 2:
                img = self.images.get(int(parts[1]))
                if img is None:
                    raise ApiError(404, "not_found")
                img["_polls"] = img.get("_polls", 0) + 1
                if img["_polls"] >= self.image_polls_until_available:
                    img["status"] = self.image_final_status
                return {"image": {k: v for k, v in copy.deepcopy(img).items() if not k.startswith("_")}}
            return self._list("images", self.images, query)
        out = super()._get(path)
        if parts[0] == "servers" and len(parts) == 2 and "server" in out:
            if self.servers.get(int(parts[1]), {}).get("_off"):
                out["server"]["status"] = "off"
        return out

    def _write(self, method, path, body):
        parts = path.strip("/").split("/")
        if method == "POST" and parts[0] == "servers" and parts[-1] == "create_image":
            iid = self._id()
            self.images[iid] = {"id": iid, "type": body["type"], "description": body["description"],
                                "labels": dict(body["labels"]), "status": "creating", "image_size": 1.5,
                                "disk_size": 40, "created": "2026-11-08T12:30:00Z", "protection": {"delete": False}}
            return {"image": copy.deepcopy({k: v for k, v in self.images[iid].items() if not k.startswith("_")}),
                    "action": {}}
        if method == "DELETE" and parts[0] == "images":
            del self.images[int(parts[1])]
            return {}
        return super()._write(method, path, body)

    def add_foreign_image(self, **labels):
        iid = self._id()
        self.images[iid] = {"id": iid, "type": "snapshot", "description": "someone else's", "labels": dict(labels),
                            "status": "available", "protection": {"delete": False}}
        return iid


class Runner:
    """Scripted stand-in for the ssh runner; records every script sent to the cabin."""

    def __init__(self, world, setup=(0, b"ok"), finalize=GOOD_FINALIZE, boom=None):
        self.world, self.setup, self.finalize, self.boom = world, setup, finalize, boom
        self.calls = []

    def __call__(self, argv, stdin, timeout):
        self.calls.append((argv, stdin, timeout))
        if self.boom and len(self.calls) == self.boom[0]:
            raise self.boom[1]
        if b"clean: cloud-init" in stdin:
            if self.finalize[0] == 0:
                for server in self.world.hz.servers.values():
                    server["_off"] = True                      # the scheduled power-off happened
            return self.finalize
        return self.setup


@pytest.fixture
def world(tmp_path):
    w = World(str(tmp_path))
    w.hz = ImageHetzner(w.clock)
    return w


@pytest.fixture
def script(tmp_path):
    path = tmp_path / "setup.sh"
    path.write_text("#!/bin/bash\napt-get install -y nodejs\nnpm install -g @anthropic-ai/claude-code\n")
    return str(path)


def do_bake(world, script, runner, name="tour-base", **kw):
    return bake.bake(world.controller(uid=0), script, name, run=runner, keygen=fake_keygen, **kw)


def hz_writes(world, method, prefix):
    return [w for w in world.hz.writes if w[0] == method and w[1].startswith(prefix)]


# ---------------------------------------------------------------------------------------------- success path

def test_bake_runs_setup_then_the_cleanup_list_then_snapshots_and_deletes_the_server(world, script):
    runner = Runner(world)
    record = do_bake(world, script, runner)
    assert record["name"] == "tour-base" and record["schema"] == "motel/tour-image/v1"
    # the setup script went to the cabin first, untouched, then one finalize session
    assert len(runner.calls) == 2
    assert runner.calls[0][1] == open(script, "rb").read()
    assert runner.calls[0][0][0] == "/usr/bin/ssh" and "-l" in runner.calls[0][0] and "cabin" in runner.calls[0][0]
    assert bake.REMOTE_RUN in runner.calls[0][0]
    # the image carries the motel labels and the lease it came from
    image = world.hz.images[record["id"]]
    assert image["type"] == "snapshot"
    assert image["labels"]["managed-by"] == "motel" and image["labels"]["role"] == "tour-image"
    assert image["labels"]["controller"] == str(HOST) and image["labels"]["image-name"] == "tour-base"
    assert image["labels"]["source-lease"] == record["lease"] and image["labels"]["baked"].endswith("Z")
    # the server was snapshotted only after it was off, and was deleted afterwards
    assert world.hz.servers == {} and world.hz.firewalls == {}
    kinds = [(m, p.rsplit("/", 1)[-1] if "actions" in p else p.split("/")[1]) for m, p, _ in world.hz.writes]
    assert kinds.index(("POST", "create_image")) < kinds.index(("DELETE", "servers"))
    assert world.store.load(record["lease"])["state"] == "released"


def test_the_finalize_session_cleans_then_scans_then_powers_off(world, script):
    runner = Runner(world)
    do_bake(world, script, runner)
    text = runner.calls[1][1].decode()
    wanted = ["cloud-init state", "ssh host keys", "the machine id", "shell and tool history", "root and home caches",
              "the journal and every log file", "bootstrap files and units"]
    positions = [text.index(item) for item in wanted]
    assert positions == sorted(positions)
    for needle in ("cloud-init clean --logs --seed", "rm -f /etc/cloud/cloud-init.disabled", "rm -f /etc/ssh/ssh_host_*",
                   ": > /etc/machine-id", ".bash_history", "rm -rf \"$h\"/.cache", "journalctl --vacuum-time=1s",
                   "find /var/log -type f -exec truncate -s 0", "/usr/local/bin/motel-guest", "/etc/ssh/cabin-keys"):
        assert needle in text, needle
    assert text.index("clean: cabin-bootstrap") < text.index("# scan:") < text.index("MOTEL_BAKE_CLEAN") < text.index("poweroff")
    assert "exit 3" in text
    # the credential check names the known patterns
    for pattern in ("*/.claude/.credentials.json", "*/.codex/auth.json", "*/.aws", "*/.config/gcloud", "*.pem", ".env"):
        assert pattern in text, pattern


@linux_only
def test_finalize_script_is_valid_shell():
    proc = subprocess.run(["bash", "-n"], input=bake.finalize_script().encode(), capture_output=True)
    assert proc.returncode == 0, proc.stderr.decode()


def test_images_lists_only_our_tour_images_and_delete_is_guarded(world, script):
    record = do_bake(world, script, Runner(world))
    foreign = world.hz.add_foreign_image(**{"managed-by": "someone"})
    lookalike = world.hz.add_foreign_image(**{"managed-by": "motel", "role": "tour-image", "controller": str(HOST)})
    controller = world.controller(uid=0)
    rows = bake.images(controller)
    assert [r["id"] for r in rows] == [record["id"]] and rows[0]["name"] == "tour-base"
    assert any("label_selector" in r and "tour-image" in r for r in world.hz.reads)
    for image_id in (foreign, lookalike):                       # not ours, or not ours in shape: never deleted
        with pytest.raises(Guarded):
            bake.delete(controller, image_id)
        assert image_id in world.hz.images
    assert bake.delete(controller, record["id"])["deleted"] is True
    assert record["id"] not in world.hz.images
    with pytest.raises(Usage):
        bake.delete(controller, "abc")


def test_a_duplicate_name_is_refused_before_any_cabin_is_created(world, script):
    do_bake(world, script, Runner(world))
    before = len(hz_writes(world, "POST", "/servers"))
    with pytest.raises(PolicyRefused):
        do_bake(world, script, Runner(world))
    assert len(hz_writes(world, "POST", "/servers")) == before


# ---------------------------------------------------------------------------------------------- refusal and failure

def test_a_credential_file_refuses_the_snapshot_and_still_deletes_the_server(world, script):
    runner = Runner(world, finalize=(3, b"CREDENTIAL /root/.claude/.credentials.json\nCREDENTIAL /opt/app/.env\n"))
    with pytest.raises(bake.BakeRefused) as caught:
        do_bake(world, script, runner)
    assert caught.value.paths == ("/opt/app/.env", "/root/.claude/.credentials.json") and caught.value.code == 3
    assert caught.value.cleanup == "released"
    assert world.hz.images == {}
    assert not [w for w in world.hz.writes if w[1].endswith("create_image")]
    assert world.hz.servers == {} and world.hz.firewalls == {}


def test_a_failing_setup_script_deletes_the_server_and_makes_no_image(world, script):
    runner = Runner(world, setup=(1, b"E: unable to locate package"))
    with pytest.raises(bake.BakeError) as caught:
        do_bake(world, script, runner)
    assert "exit 1" in str(caught.value) and "unable to locate package" in str(caught.value)
    assert len(runner.calls) == 1 and world.hz.servers == {} and world.hz.images == {}
    assert caught.value.cleanup == "released"


def test_a_failing_cleanup_makes_no_image(world, script):
    runner = Runner(world, finalize=(2, b"boom"))
    with pytest.raises(bake.BakeError):
        do_bake(world, script, runner)
    assert world.hz.servers == {} and world.hz.images == {}


def test_an_unexpected_exception_still_deletes_the_server(world, script):
    runner = Runner(world, boom=(2, RuntimeError("ssh blew up")))
    with pytest.raises(RuntimeError):
        do_bake(world, script, runner)
    assert world.hz.servers == {} and world.hz.firewalls == {} and world.hz.images == {}


def test_a_keyboard_interrupt_still_deletes_the_server(world, script):
    runner = Runner(world, boom=(1, KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        do_bake(world, script, runner)
    assert world.hz.servers == {}


def test_a_snapshot_that_fails_is_removed_and_the_server_is_deleted(world, script):
    world.hz.image_final_status = "unavailable"
    with pytest.raises(Fail):
        do_bake(world, script, Runner(world))
    assert world.hz.images == {} and world.hz.servers == {}


def test_a_server_that_never_powers_off_times_out_and_is_deleted(world, script):
    runner = Runner(world)
    original = runner.__call__

    def never_off(argv, stdin, timeout):
        rc = original(argv, stdin, timeout)
        for server in world.hz.servers.values():
            server.pop("_off", None)
        return rc
    with pytest.raises(Fail) as caught:
        do_bake(world, script, never_off)
    assert "power off" in str(caught.value) and world.hz.servers == {} and world.hz.images == {}


def test_a_checkout_that_cannot_finish_is_reported_not_hidden(world, script, monkeypatch):
    monkeypatch.setattr(bake, "_check_out", lambda controller, lease_id: "cleanup_pending")
    with pytest.raises(Incomplete) as caught:
        do_bake(world, script, Runner(world))
    assert "cleanup_pending" in str(caught.value)


def test_a_failed_bake_reports_the_cleanup_state_on_the_error(world, script, monkeypatch):
    monkeypatch.setattr(bake, "_check_out", lambda controller, lease_id: "cleanup_pending")
    with pytest.raises(bake.BakeError) as caught:
        do_bake(world, script, Runner(world, setup=(1, b"x")))
    assert caught.value.cleanup == "cleanup_pending"


def test_bad_inputs_are_refused_before_any_cabin_exists(world, script, tmp_path):
    for name in ("", "UPPER", "-x", "a" * 41, "a b"):
        with pytest.raises(Usage):
            do_bake(world, script, Runner(world), name=name)
    empty = tmp_path / "empty.sh"
    empty.write_text("   \n")
    link = tmp_path / "link.sh"
    os.symlink(script, link)
    for path in (str(empty), str(link), str(tmp_path / "missing.sh"), str(tmp_path)):
        with pytest.raises(Usage):
            do_bake(world, path, Runner(world))
    assert world.hz.servers == {} and not world.hz.writes


# ---------------------------------------------------------------------------------------------- the credential scan itself

def scan(tmp_path, build):
    for rel, text in build.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    script = bake.credential_scan_script(roots=[str(tmp_path)])
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return sorted(line[len(str(tmp_path)):] for line in proc.stdout.splitlines())


def test_the_scan_finds_known_credential_files_and_prints_only_paths(tmp_path):
    hits = scan(tmp_path, {
        "u/.claude/.credentials.json": '{"claudeAiOauth": {"accessToken": "SECRET-VALUE"}}',
        "u/.codex/auth.json": "SECRET-VALUE", "u/.aws/credentials": "SECRET-VALUE",
        "u/.config/gcloud/application_default_credentials.json": "SECRET-VALUE",
        "u/app/.env": "KEY=SECRET-VALUE", "u/app/.env.production": "KEY=SECRET-VALUE",
        "u/.ssh/id_ed25519": "SECRET-VALUE", "u/.netrc": "SECRET-VALUE",
        "srv/tls/server.pem": "-----BEGIN PRIVATE KEY-----\nSECRET-VALUE\n-----END PRIVATE KEY-----\n",  # gitleaks:allow (fake test value)
        "srv/tls/other.key": "-----BEGIN RSA PRIVATE KEY-----\nSECRET-VALUE\n",
    })
    flagged = [h for h in hits]
    for expected in ("/u/.claude/.credentials.json", "/u/.codex/auth.json", "/u/.aws", "/u/.config/gcloud",
                     "/u/app/.env", "/u/app/.env.production", "/u/.ssh/id_ed25519", "/u/.netrc", "/srv/tls/server.pem",
                     "/srv/tls/other.key"):
        assert expected in flagged, expected
    assert "SECRET-VALUE" not in "\n".join(hits)


def test_the_scan_ignores_harmless_files(tmp_path):
    hits = scan(tmp_path, {
        "u/app/.env.example": "KEY=", "u/app/.env.sample": "KEY=", "u/.ssh/id_ed25519.pub": "ssh-ed25519 AAAA",
        "etc/ca-bundle.pem": "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n",
        "u/notes.txt": "mentions .env and auth.json and .aws in text", "u/project/node_modules/x/test.pem":
        "-----BEGIN PRIVATE KEY-----\nfixture\n", "u/.claude/settings.json": "{}",  # gitleaks:allow (fake test value)
    })
    assert hits == []


def test_every_cleanup_step_is_documented_and_ordered_in_the_constant():
    ids = [step[0] for step in bake.CLEANUP_STEPS]
    assert ids == ["cloud-init", "ssh-host-keys", "machine-id", "shell-history", "caches", "logs", "cabin-bootstrap"]
    assert all(step[1] and step[2] for step in bake.CLEANUP_STEPS)


# ---------------------------------------------------------------------------------------------- provider image methods

def test_create_image_requires_a_powered_off_server_the_lease_owns(world):
    controller = world.controller(uid=0)
    view = world.checkin(controller)
    rec = world.store.load(view["lease_id"])
    with pytest.raises(GuardRefused, match="powered off"):
        controller.hz.create_image(rec, name="x1")
    world.hz.servers[rec["server_id"]]["_off"] = True
    with pytest.raises(GuardRefused, match="invalid image name"):
        controller.hz.create_image(rec, name="Bad Name")
    image = controller.hz.create_image(rec, name="x1")
    assert world.hz.images[image["id"]]["labels"]["image-name"] == "x1"
    forged = dict(rec, server_id=rec["server_id"] + 1)
    with pytest.raises((GuardRefused, ApiError)):
        controller.hz.create_image(forged, name="x2")


def test_image_writes_re_prove_the_project_is_dedicated(world, script):
    world.hz.visible.add(HOST)                     # the token can suddenly see the host: every write is refused
    controller = world.controller(uid=0)
    with pytest.raises(GuardRefused):
        controller.hz.delete_image(world.hz.add_foreign_image(**{"managed-by": "motel", "role": "tour-image",
                                                                  "controller": str(HOST), "image-name": "x"}))
    assert not hz_writes(world, "DELETE", "/images")


def test_protected_images_are_not_deleted(world):
    iid = world.hz.add_foreign_image(**{"managed-by": "motel", "role": "tour-image", "controller": str(HOST),
                                        "image-name": "keep"})
    world.hz.images[iid]["protection"] = {"delete": True}
    with pytest.raises(GuardRefused):
        world.controller(uid=0).hz.delete_image(iid)
    assert iid in world.hz.images


# ---------------------------------------------------------------------------------------------- review round

R6_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----\n"
R6_CONTROLS = {                                      # caught before the review as well
    "root/.claude/.credentials.json": "{}",
    "root/.ssh/id_ed25519": R6_KEY,
    "etc/app/server.key": R6_KEY,
}
R6_LEFTOVERS = {                                     # the nine the review found missing (synthetic values)
    "var/lib/tailscale/tailscaled.state": '{"_machinekey": "privkey:0000"}',
    "opt/app/.git/config": '[remote "origin"]\n\turl = https://x-access-token:ghs_EXAMPLEONLY@github.com/o/r.git\n',
    "etc/environment": 'ANTHROPIC_API_KEY="sk-ant-api03-EXAMPLEONLY"\n',
    "root/.bashrc": "export OPENAI_API_KEY=sk-proj-EXAMPLEONLY\n",
    "root/.ssh/github_deploy": R6_KEY,
    "root/.ssh/id_ed25519_deploy": R6_KEY,
    "root/.gemini/oauth_creds.json": '{"refresh_token": "EXAMPLEONLY"}',
    "root/.config/claude/credentials": "EXAMPLEONLY",
    "var/www/app/.env": "SECRET=EXAMPLEONLY\n",
}


def scan_production_roots(tmp_path, files):
    """Build a tree and scan it with the production root list moved under ``tmp_path``."""
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    roots = tuple(str(tmp_path / root.lstrip("/")) for root in bake.SCAN_ROOTS)
    proc = subprocess.run(["bash", "-c", bake.credential_scan_script(roots)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return {line[len(str(tmp_path)) + 1:] for line in proc.stdout.splitlines() if line}


def caught(hits, rel):
    return any(rel == hit or rel.startswith(hit + "/") for hit in hits)


@pytest.mark.parametrize("rel", sorted(R6_CONTROLS))
def test_r6_controls_are_still_caught(tmp_path, rel):
    assert caught(scan_production_roots(tmp_path, {**R6_CONTROLS, **R6_LEFTOVERS}), rel)


@pytest.mark.parametrize("rel", sorted(R6_LEFTOVERS))
def test_r6_credential_leftovers_are_caught(tmp_path, rel):
    assert caught(scan_production_roots(tmp_path, {**R6_CONTROLS, **R6_LEFTOVERS}), rel)


def test_the_scan_prints_paths_and_never_the_secret_values(tmp_path):
    hits = scan_production_roots(tmp_path, R6_LEFTOVERS)
    assert not any("EXAMPLEONLY" in hit or "ghs_" in hit for hit in hits)


@pytest.mark.parametrize("name,text", [
    ("a", "token = ghp_" + "A" * 36), ("b", "github_pat_" + "B" * 30), ("c", "SLACK=xoxb-123456789012-abcdefgh"),
    ("d", "aws_access_key_id = AKIA" + "Q" * 16), ("e", "-----BEGIN RSA PRIVATE KEY-----\n"),
    ("f", "key: sk-ant-api03-short"), ("g", "OPENAI=sk-" + "z" * 40)])
def test_known_token_shapes_in_ordinary_text_files_are_caught(tmp_path, name, text):
    assert scan_production_roots(tmp_path, {f"home/u/notes-{name}.txt": text}) == {f"home/u/notes-{name}.txt"}


def test_a_git_config_url_with_a_user_and_password_is_a_credential_but_plain_urls_are_not(tmp_path):
    hits = scan_production_roots(tmp_path, {
        "opt/a/.git/config": '[remote "o"]\n\turl = https://user:hunter2@example.org/r.git\n',
        "opt/b/.git/config": '[remote "o"]\n\turl = git@example.org:o/r.git\n[user]\n\tname = alice\n',
        "opt/c/.git/config": '[remote "o"]\n\turl = https://example.org/o/r.git\n',
        "opt/d/.git/config": '[remote "o"]\n\turl = ssh://git@example.org/o/r.git\n'})
    assert hits == {"opt/a/.git/config"}


@linux_only
def test_ordinary_text_with_look_alike_words_is_not_flagged(tmp_path):
    hits = scan_production_roots(tmp_path, {
        "home/u/readme.txt": "disk-usage task-list sk-1 whisk-broom ask-me-anything risk-assessment-and-more skiing-ant",
        "opt/app/data.csv": "id,name\n1,alice\n", "etc/hostname": "cabin\n",
        "home/u/big.txt": "sk-ant-api03-EXAMPLE " + "x" * (bake.CONTENT_MAX_KIB * 1024 + 10)})
    assert hits == set()                                  # the last file is over the size cap: skipped, documented


def test_var_lib_is_scanned_by_path_but_not_by_content(tmp_path):
    hits = scan_production_roots(tmp_path, {"var/lib/app/data.txt": "sk-ant-api03-EXAMPLEONLY",
                                            "var/lib/app/.env": "A=1"})
    assert hits == {"var/lib/app/.env"}


def test_a_bake_in_tailscale_mode_is_refused_before_anything_is_created(tmp_path, script):
    w = World(str(tmp_path), access="tailscale")
    w.hz = ImageHetzner(w.clock)
    with pytest.raises(PolicyRefused, match="tailscale"):
        bake.bake(w.controller(uid=0), script, "tour-base", run=Runner(w), keygen=fake_keygen)
    assert w.hz.servers == {} and not w.hz.writes and not w.hz.images


def test_the_bake_scan_refuses_a_leftover_found_by_content_and_names_only_the_path(world, script):
    err = None
    runner = Runner(world, finalize=(bake.SCAN_EXIT, b"CREDENTIAL /root/.bashrc\nCREDENTIAL /var/www/app/.env\n"))
    try:
        do_bake(world, script, runner)
    except bake.BakeRefused as exc:
        err = exc
    assert err is not None and err.paths == ("/root/.bashrc", "/var/www/app/.env")
    assert world.hz.servers == {} and not world.hz.images


# ---- the creation record (L4)

def records(world):
    import json
    path = os.path.join(world.store.root, "tour-images.json")
    return json.load(open(path))["images"] if os.path.exists(path) else {}


def test_an_image_is_recorded_when_it_is_created_and_the_record_is_a_private_file(world, script):
    record = do_bake(world, script, Runner(world))
    rows = records(world)
    assert list(rows) == [str(record["id"])] and rows[str(record["id"])]["name"] == "tour-base"
    assert rows[str(record["id"])]["lease"] == record["lease"]
    assert (os.stat(os.path.join(world.store.root, "tour-images.json")).st_mode & 0o777) == 0o600
    assert bake.images(world.controller(uid=0))[0]["recorded"] is True


def test_delete_needs_the_record_even_when_the_labels_match(world, script):
    labelled = world.hz.add_foreign_image(**{"managed-by": "motel", "role": "tour-image", "controller": str(HOST),
                                             "image-name": "orphan"})
    controller = world.controller(uid=0)
    with pytest.raises(Guarded, match="no creation record"):
        bake.delete(controller, labelled)
    assert labelled in world.hz.images and not hz_writes(world, "DELETE", "/images")
    assert bake.images(controller)[0]["recorded"] is False


def test_delete_removes_the_record_and_a_forged_record_does_not_help_a_foreign_image(world, script):
    record = do_bake(world, script, Runner(world))
    controller = world.controller(uid=0)
    foreign = world.hz.add_foreign_image(**{"managed-by": "someone"})
    path = os.path.join(world.store.root, "tour-images.json")
    import json
    doc = json.load(open(path))
    doc["images"][str(foreign)] = {"name": "x", "lease": "tour-0123456789ab"}      # someone edited the record
    open(path, "w").write(json.dumps(doc))
    with pytest.raises(Guarded):
        bake.delete(controller, foreign)                                          # the labels still say no
    assert foreign in world.hz.images
    assert bake.delete(controller, record["id"])["deleted"] is True
    assert str(record["id"]) not in records(world)


def test_a_failed_bake_leaves_no_record(world, script):
    world.hz.image_final_status = "unavailable"
    with pytest.raises(Unavailable):
        do_bake(world, script, Runner(world))
    assert records(world) == {} and not world.hz.images


def test_a_stale_record_for_an_image_gone_at_the_provider_is_dropped(world, script):
    record = do_bake(world, script, Runner(world))
    del world.hz.images[record["id"]]
    out = bake.delete(world.controller(uid=0), record["id"])
    assert out["deleted"] is True and out["already_gone"] is True and records(world) == {}


def test_a_symlinked_or_damaged_record_file_is_refused(world, script):
    path = os.path.join(world.store.root, "tour-images.json")
    os.symlink("/etc/hostname", path)
    with pytest.raises(Guarded):
        bake.images(world.controller(uid=0))
    os.unlink(path)
    open(path, "w").write("{not json")
    with pytest.raises(Guarded):
        bake.delete(world.controller(uid=0), 5)


# ---- the intent record (R-L4): the snapshot call whose answer is lost

def intents(world):
    import json
    path = os.path.join(world.store.root, "tour-images.json")
    return json.load(open(path)).get("intents", {}) if os.path.exists(path) else {}


class LostAnswer(ImageHetzner):
    """Creates the image at the provider, then fails the answer to the caller."""

    def __init__(self, clock):
        super().__init__(clock)
        self.lose = True

    def _write(self, method, path, body):
        out = super()._write(method, path, body)
        if self.lose and method == "POST" and path.endswith("create_image"):
            raise ApiError(502, "bad_gateway")
        return out


@pytest.fixture
def lossy(tmp_path):
    w = World(str(tmp_path))
    w.hz = LostAnswer(w.clock)
    return w


def test_the_intent_is_written_before_the_snapshot_call(world, script, monkeypatch):
    seen = []
    original = world.hz._write

    def spy(method, path, body):
        if path.endswith("create_image"):
            seen.append(intents(world))
        return original(method, path, body)
    monkeypatch.setattr(world.hz, "_write", spy)
    record = do_bake(world, script, Runner(world))
    assert len(seen) == 1 and list(seen[0].values()) == [{"name": "tour-base", "recorded_at": seen[0][record["lease"]]["recorded_at"]}]
    assert intents(world) == {} and str(record["id"]) in records(world)          # settled by the answer


def test_a_lost_answer_is_cleaned_up_by_labels_and_the_intent_is_settled(lossy, script):
    with pytest.raises(ApiError):
        do_bake(lossy, script, Runner(lossy))
    assert not lossy.hz.images and intents(lossy) == {} and records(lossy) == {}


def test_a_lost_answer_whose_cleanup_also_fails_still_lets_the_image_be_deleted_by_the_intent(lossy, script, monkeypatch):
    controller = lossy.controller(uid=0)
    original = type(controller.hz).list_images
    calls = {"n": 0}

    def flaky(self, *a, **k):
        calls["n"] += 1
        if calls["n"] > 1:                      # the first call is bake's own duplicate-name check
            raise Unavailable("provider down")
        return original(self, *a, **k)
    monkeypatch.setattr(type(controller.hz), "list_images", flaky)
    with pytest.raises(ApiError):
        bake.bake(controller, script, "tour-base", run=Runner(lossy), keygen=fake_keygen)
    monkeypatch.undo()
    (image_id,) = lossy.hz.images
    assert len(intents(lossy)) == 1 and records(lossy) == {}
    controller = lossy.controller(uid=0)
    assert bake.images(controller)[0]["recorded"] is True
    assert bake.delete(controller, image_id)["deleted"] is True
    assert not lossy.hz.images and intents(lossy) == {}


def test_a_pending_intent_does_not_cover_an_image_with_another_name_or_lease(lossy, script, monkeypatch):
    controller = lossy.controller(uid=0)
    original = type(controller.hz).list_images
    state = {"n": 0}

    def flaky(self, *a, **k):
        state["n"] += 1
        if state["n"] > 1:
            raise Unavailable("down")
        return original(self, *a, **k)
    monkeypatch.setattr(type(controller.hz), "list_images", flaky)
    with pytest.raises(ApiError):
        bake.bake(controller, script, "tour-base", run=Runner(lossy), keygen=fake_keygen)
    monkeypatch.undo()
    base_labels = dict(next(iter(lossy.hz.images.values()))["labels"])
    other_name = lossy.hz.add_foreign_image(**{**base_labels, "image-name": "other"})
    other_lease = lossy.hz.add_foreign_image(**{**base_labels, "source-lease": "tour-ffffffffffff"})
    controller = lossy.controller(uid=0)
    for image_id in (other_name, other_lease):
        with pytest.raises(Guarded, match="no creation record"):
            bake.delete(controller, image_id)
        assert image_id in lossy.hz.images
