"""Second engine review: bundle inspection in production, the sandbox around git, quotas under concurrency,
operators, SIGTERM requeue, listener loss."""
import os
import subprocess

import pytest

from motel.cabins.desk import CabinPaths
from motel.tours import bundle
from motel.tours import desk as tour_desk
from motel.tours.errors import Broken
from platform_marks import linux_only

GIT_ENV = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@example.invalid",
           "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@example.invalid"}
TID, GID = "t-0123456789", "fixer"
BRANCH = f"motel/{TID}/{GID}"
TOKEN = "sk-ant-oat01-" + "Q" * 40 + "fake"


def _git(tmp_path, *args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, env={**GIT_ENV, "HOME": str(tmp_path)}, check=True, capture_output=True,
                          text=True).stdout.strip()


def make_bundles(tmp_path, files):
    """A repository with a base commit, and a result bundle whose branch adds ``files`` ({name: bytes})."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(tmp_path, "init", "-q", "-b", "main", cwd=repo)
    (repo / "a.txt").write_text("one\n")
    _git(tmp_path, "add", ".", cwd=repo)
    _git(tmp_path, "commit", "-q", "-m", "base", cwd=repo)
    base = _git(tmp_path, "rev-parse", "HEAD", cwd=repo)
    base_bundle = tmp_path / "base.bundle"
    bundle.make_bundle(str(repo), base, str(base_bundle))
    _git(tmp_path, "checkout", "-q", "-b", BRANCH, cwd=repo)
    for name, data in files.items():
        (repo / name).write_bytes(data)
    _git(tmp_path, "add", ".", cwd=repo)
    _git(tmp_path, "commit", "-q", "-m", "work", cwd=repo)
    result = tmp_path / "result.bundle"
    _git(tmp_path, "bundle", "create", str(result), f"{base}..{BRANCH}", cwd=repo)
    return str(base_bundle), str(result), base


# -- N1: the production environment inspects every bundle -------------------------------------------------------------

def test_the_production_env_inspects_the_bundle(rig):
    factory = tour_desk.make_env_factory(CabinPaths(root=str(rig.tmp / "root")), rig.store, rig.limits, rig.service.cabin_cfg,
                                         rig.clock, {}, transport=object(), concierge_factory=lambda: object())
    env = factory()
    assert callable(env.inspect_bundle)


@linux_only
def test_the_production_inspector_keeps_its_scratch_outside_the_root_only_state_and_cleans_up(rig, tmp_path):
    base_bundle, result, base = make_bundles(tmp_path, {"b.txt": b"two\n"})
    inspect = tour_desk._inspector(CabinPaths(root=str(tmp_path / 'sys')))
    out = inspect(base_bundle, result, base, BRANCH, "a-1", None)
    assert sorted(out["changed_files"]) == ["b.txt"] and out["token_found"] is False
    root = tmp_path / "sys" / tour_desk.INSPECT_ROOT.lstrip("/")
    assert os.listdir(root) == [] and (root.stat().st_mode & 0o777) == 0o711
    assert not str(root).startswith(rig.store.root)


# -- N6: a token committed in a (compressed) object is found ------------------------------------------------------------

@linux_only
def test_a_token_inside_a_commit_is_found_by_the_object_scan(tmp_path):
    base_bundle, result, base = make_bundles(tmp_path, {"leak.txt": ("token=" + TOKEN + "\n").encode() * 50})
    work = tmp_path / "work"
    hit = bundle.inspect_result(base_bundle, result, base, BRANCH, work_root=str(work), token=TOKEN)
    assert hit["token_found"] is True and hit["head"]
    clean = bundle.inspect_result(base_bundle, result, base, BRANCH, work_root=str(work), token="sk-ant-oat01-" + "Z" * 40)
    assert clean["token_found"] is False
    assert bundle.inspect_result(base_bundle, result, base, BRANCH, work_root=str(work))["token_found"] is False
    assert os.listdir(work) == []


@linux_only
def test_a_token_in_a_file_name_is_found_too(tmp_path):
    base_bundle, result, base = make_bundles(tmp_path, {"x-" + TOKEN + ".txt": b"hi\n"})
    out = bundle.inspect_result(base_bundle, result, base, BRANCH, work_root=str(tmp_path / "w"), token=TOKEN)
    assert out["token_found"] is True


def test_a_committed_token_withholds_the_bundle_in_the_pipeline(rig):
    from test_ownlogin import enable
    enable(rig)
    rig.inspect_token_found = True
    doc = rig.run_tour([{"id": "me", "prompt": "x", "auth": "own-login", "secrets": []}])
    record = rig.result_record(doc["id"], "me")
    assert record["verified"]["bundle"] is None and doc["guests"]["me"]["status"] == "failed"
    assert any("own-login token; withheld" in v for v in record["violations"])
    assert rig.inspect_tokens and rig.inspect_tokens[0]          # the scan was asked for with the token


def test_an_inspection_that_fails_fails_the_guest_closed(rig):
    rig.inspect_error = "the result bundle does not verify against the base commit"
    doc = rig.run_tour()
    assert doc["guests"]["fix-auth"]["status"] == "failed"
    assert "could not be inspected" in rig.result_record(doc["id"], "fix-auth")["error"]


# -- the sandbox ---------------------------------------------------------------------------------------------------------

def test_git_never_runs_as_root_and_a_missing_user_fails_closed(monkeypatch, tmp_path):
    base_bundle, result, base = make_bundles(tmp_path, {"b.txt": b"x\n"})
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    with pytest.raises(Broken, match="does not exist"):
        bundle.inspect_result(base_bundle, result, base, BRANCH, work_root=str(tmp_path / "w"),
                              sandbox=bundle.Sandbox(user="motel-inspect-nobody-here"))
    import pwd
    monkeypatch.setattr(pwd, "getpwnam", lambda name: pwd.struct_passwd(("root", "x", 0, 0, "", "/root", "/bin/sh")))
    with pytest.raises(Broken, match="must not be root"):
        bundle.inspect_result(base_bundle, result, base, BRANCH, work_root=str(tmp_path / "w"))
    assert not (tmp_path / "w").exists() or os.listdir(tmp_path / "w") == []


@linux_only
def test_git_is_started_with_limits_dropped_groups_and_one_pack_thread(monkeypatch, tmp_path):
    seen = []
    real = subprocess.Popen

    def popen(argv, **kw):
        seen.append((argv, kw))
        return real(argv, **kw)
    monkeypatch.setattr(subprocess, "Popen", popen)
    base_bundle, result, base = make_bundles(tmp_path, {"b.txt": b"x\n"})
    bundle.inspect_result(base_bundle, result, base, BRANCH, work_root=str(tmp_path / "w"))
    argv, kw = seen[-1]
    text = " ".join(argv)
    assert argv[0] == "prlimit" and "--as=" in text and "--cpu=60" in text and "--fsize=" in text and "--nofile=256" in text
    assert "pack.threads=1" in text and "core.bigFileThreshold=" in text and "core.hooksPath=/dev/null" in text
    assert kw["start_new_session"] is True and kw["env"]["GIT_CONFIG_GLOBAL"] == "/dev/null"


def test_the_process_limit_applies_to_the_dedicated_user_only(tmp_path):
    sandbox = bundle.Sandbox()
    assert not any(x.startswith("--nproc") for x in bundle._Git(sandbox, str(tmp_path), None).argv(["status"]))
    assert "--nproc=256" in bundle._Git(sandbox, str(tmp_path), (987, 986)).argv(["status"])


def test_the_privilege_drop_arguments_are_set_when_the_desk_is_root(monkeypatch, tmp_path):
    seen = []
    base_bundle, result, base = make_bundles(tmp_path, {"b.txt": b"x\n"})

    class Stop(Exception):
        pass

    def popen(argv, **kw):
        seen.append(kw)
        raise Stop

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    import pwd
    monkeypatch.setattr(pwd, "getpwnam", lambda name: pwd.struct_passwd((name, "x", 987, 986, "", "/nonexistent", "/usr/sbin/nologin")))
    monkeypatch.setattr(os, "chown", lambda *a, **k: None)
    monkeypatch.setattr(os, "fchown", lambda *a, **k: None)
    monkeypatch.setattr(subprocess, "Popen", popen)
    with pytest.raises(Stop):
        bundle.inspect_result(base_bundle, result, base, BRANCH, work_root=str(tmp_path / "w"))
    assert seen[0]["user"] == 987 and seen[0]["group"] == 986 and seen[0]["extra_groups"] == []
    assert os.listdir(tmp_path / "w") == []          # the scratch directory is removed even when git cannot start


@pytest.mark.parametrize("sandbox,match", [
    (bundle.Sandbox(total_s=0.0), "too long"),
    (bundle.Sandbox(output_bytes=10), "printed too much"),
    (bundle.Sandbox(copy_bytes=100), "too large"),
    (bundle.Sandbox(scratch_files=3), "too large"),
])
@linux_only
def test_a_bound_that_is_hit_stops_git_and_fails_closed(tmp_path, sandbox, match):
    base_bundle, result, base = make_bundles(tmp_path, {"b.txt": b"x\n" * 1000})
    work = tmp_path / "w"
    with pytest.raises(Broken, match=match):
        bundle.inspect_result(base_bundle, result, base, BRANCH, work_root=str(work), sandbox=sandbox)
    assert os.listdir(work) == []


@linux_only
def test_too_much_new_data_to_scan_fails_closed(tmp_path):
    base_bundle, result, base = make_bundles(tmp_path, {"big.bin": os.urandom(200_000)})
    with pytest.raises(Broken, match="too much new data"):
        bundle.inspect_result(base_bundle, result, base, BRANCH, work_root=str(tmp_path / "w"), token=TOKEN,
                              sandbox=bundle.Sandbox(scan_bytes=1000))


def test_a_runaway_git_is_killed_at_the_deadline(monkeypatch, tmp_path):
    sandbox = bundle.Sandbox(step_s=0.5)
    git = bundle._Git(sandbox, str(tmp_path), None)
    monkeypatch.setattr(git, "argv", lambda args: ["sleep", "30"])
    with pytest.raises(Broken, match="too long"):
        git(["x"])


# -- N2: the ceilings hold when starts run at the same time --------------------------------------------------------------

def _paused_start(rig, ceiling, **kw):
    """Run one start that stops inside store.create (after the quota check) and a second start meanwhile."""
    import threading
    rig.set_limits(**{ceiling[0]: ceiling[1]})
    real_create = rig.service.store.create
    inside, go, results = threading.Event(), threading.Event(), {}

    def create(tid, doc):
        if not inside.is_set():
            inside.set()
            assert go.wait(20)
        return real_create(tid, doc)
    rig.service.store.create = create

    def second():
        try:
            results["second"] = rig.start(**kw)
        except Exception as exc:  # noqa: BLE001
            results["second"] = exc

    first = threading.Thread(target=lambda: results.setdefault("first", rig.start(**kw)))
    first.start()
    assert inside.wait(20)
    other = threading.Thread(target=second)
    other.start()
    other.join(0.5)
    blocked = other.is_alive()                  # the second start waits at the lock; it has not passed the quota check
    go.set()
    first.join(20)
    other.join(20)
    return blocked, results


def test_two_interleaved_starts_cannot_both_pass_the_active_ceiling(rig):
    from motel.tours.errors import Refused
    blocked, results = _paused_start(rig, ("max_active_tours_per_user", 1))
    assert blocked
    assert isinstance(results["first"], str) and isinstance(results["second"], Refused)
    active = [t for t in rig.store.ids() if rig.store.load(t)["status"] not in ("done", "failed", "cancelled")]
    assert active == [results["first"]]


def test_two_interleaved_starts_cannot_both_pass_the_staged_bytes_ceiling(rig):
    from motel.tours.errors import Refused
    rig.set_limits(max_active_tours_per_user=5, max_staged_mb_per_user=1)
    rig.store.staged_bytes = lambda tid: 1 << 20              # each unfinished tour holds 1 MiB: only one fits under the ceiling
    blocked, results = _paused_start(rig, ("max_active_tours_per_user", 5))
    assert blocked and isinstance(results["first"], str) and isinstance(results["second"], Refused)
    assert "staged" in str(results["second"])


def test_staging_takes_the_host_lock_around_the_free_space_check(rig, monkeypatch):
    order = []
    real = rig.store.admission

    import contextlib

    @contextlib.contextmanager
    def admission(name, timeout=None):
        order.append(("lock", name))
        with real(name, timeout):
            yield
        order.append(("unlock", name))
    monkeypatch.setattr(rig.store, "admission", admission)
    rig.service.free_bytes = lambda path: (order.append(("free",)) or 10 ** 12)
    rig.start()
    names = [e for e in order if e[0] == "lock"]
    assert names[0][1].startswith("user-") and names[1] == ("lock", "host")
    assert order.index(("free",)) > order.index(("lock", "host")) and order.index(("free",)) < order.index(("unlock", "host"))
    assert order.index(("unlock", "host")) < order.index(("unlock", names[0][1]))


# -- N3: an operator who is not a tour user may read and cancel every tour -----------------------------------------------

@linux_only
def test_an_admin_who_is_not_in_allow_users_sees_and_cancels_every_tour_through_sudo(rig):
    import json

    from test_tours_desk import desk as run_desk

    from motel.cabins.desk import Caller
    tid = rig.start()                                    # alice's tour
    rig.supervise(tid)
    carol = Caller(uid=4242, gid=4242, name="carol", operator=True)    # what real_caller makes of `sudo` by an admin
    rig.service.operator_fn = lambda c: tour_desk.is_operator(c, rig.service.cabin_cfg, lambda c: set())   # as execute wires it
    assert tour_desk.is_operator(carol, rig.service.cabin_cfg, lambda c: set())
    code, out, err = run_desk(rig, ["ls", "--all", "--json"], caller=carol)
    assert code == 0, err.getvalue()
    assert [t["id"] for t in json.loads(out.getvalue())["tours"]] == [tid]
    code, out, _ = run_desk(rig, ["ls", "--json"], caller=carol)
    assert json.loads(out.getvalue())["tours"] == []     # without --all it lists her own, and she has none
    for argv in (["status", tid], ["wait", tid, "--timeout", "1"], ["result", tid, "fix-auth"], ["logs", tid, "fix-auth"]):
        code, out, err = run_desk(rig, argv + ["--json"], caller=carol)
        assert code == 0, (argv, out.getvalue(), err.getvalue())
    code, out, err = run_desk(rig, ["cancel", tid, "--json"], caller=carol)
    assert code == 0 and json.loads(out.getvalue())["ok"] is True


@linux_only
def test_that_admin_still_cannot_start_or_fetch_and_a_non_operator_cannot_ls_all(rig):
    from test_tours_desk import desk as run_desk

    from motel.cabins.desk import Caller
    carol = Caller(uid=4242, gid=4242, name="carol", operator=True)
    for argv in (["start", "--json"], ["fetch", "t-0123456789", "fixer"]):
        code, out, err = run_desk(rig, argv, caller=carol, stdin=b"{}")
        assert code != 0 and "not in allow_users" in out.getvalue() + err.getvalue()
    tid = rig.start()
    rig.supervise(tid)
    mallory = Caller(uid=4343, gid=4343, name="mallory", operator=False)
    code, out, err = run_desk(rig, ["ls", "--all", "--json"], caller=mallory)
    assert code != 0 and "not in allow_users" in out.getvalue() + err.getvalue()


def test_the_sudoers_rule_offers_the_read_verbs_to_tour_users_and_never_the_operator_verbs():
    from motel import installer
    text = installer.render_sudoers(["alice"], cabins=True, tours=True)
    for verb in sorted(tour_desk.OPERATOR_MAY_USE):
        assert f"motel-desk tour {verb} *" in text or f"motel-desk tour {verb}," in text, verb
    for verb in tour_desk.OPERATOR_VERBS:
        assert f"motel-desk tour {verb}" not in text, verb     # admins reach these through their own sudo rights


# -- N5: a SIGTERM stop before the command is free ----------------------------------------------------------------------

def _stop_before_the_command(rig):
    from rig import SyncExecutor

    from motel.tours.supervisor import Supervisor
    original = rig.transport.preflight

    def preflight(target, forward_port, **kw):
        original(target, forward_port, **kw)
        rig.env.stop.set()
    rig.transport.preflight = preflight
    tid = rig.start()
    Supervisor(rig.env, tid, executor_factory=SyncExecutor).run()
    rig.transport.preflight = original
    rig.env.stop.clear()
    return tid


def test_a_stop_before_the_command_does_not_use_the_guests_retry(rig):
    tid = _stop_before_the_command(rig)
    guest = rig.store.load(tid)["guests"]["fix-auth"]
    assert guest["status"] == "queued" and guest.get("retries_used", 0) == 0
    with open(rig.store.path(tid, "events.jsonl")) as handle:
        assert any('"requeued_on_stop"' in line for line in handle)
    rig.transport.fail_preflight = 1                      # the restarted supervisor then still has its one retry
    rig.supervise(tid)
    final = rig.store.load(tid)
    assert final["status"] == "done" and final["guests"]["fix-auth"]["retries_used"] == 1


def test_reap_restarts_a_deliberately_stopped_supervisor_without_counting_it(rig):
    tid = _stop_before_the_command(rig)
    sup = rig.store.load(tid)["supervisor"]
    assert sup["stopped"] and sup["restarts"] == 0
    rig.unit_is_active = False
    first = rig.service.reap(True)
    assert {"tour": tid, "action": "restart_supervisor"} in first["actions"] and rig.launched == [tid, tid]
    sup = rig.store.load(tid)["supervisor"]
    assert sup["restarts"] == 0 and sup["stopped"] is None
    rig.service.reap(True)                                  # a crash (no stop recorded) does count
    assert rig.store.load(tid)["supervisor"]["restarts"] == 1


def test_a_supervisor_that_started_clears_its_stop_mark(rig):
    tid = _stop_before_the_command(rig)
    rig.supervise(tid)
    assert rig.store.load(tid)["supervisor"]["stopped"] is None


# -- F1 (round 4): what the user receives is rebuilt from the objects that were scanned -----------------------------------

def make_repo_bundles(tmp_path, *, runner_style=False):
    """A repository with a base and a result commit; returns (repo, base_bundle, base, head, result_bundle_path)."""
    base_bundle, result, base = make_bundles(tmp_path, {"ok.txt": b"fine\n"})
    repo = tmp_path / "repo"
    head = _git(tmp_path, "rev-parse", BRANCH, cwd=repo)
    if runner_style:        # what the runner does: `git bundle create out base..HEAD` on the branch: the ref is HEAD
        _git(tmp_path, "checkout", "-q", BRANCH, cwd=repo)
        result = str(tmp_path / "runner.bundle")
        _git(tmp_path, "bundle", "create", result, f"{base}..HEAD", cwd=repo)
    return repo, base_bundle, base, head, result


@linux_only
def test_the_runners_own_bundle_names_only_head_and_is_accepted_and_delivered_under_the_branch_name(tmp_path):
    repo, base_bundle, base, head, result = make_repo_bundles(tmp_path, runner_style=True)
    assert _git(tmp_path, "bundle", "list-heads", result, cwd=repo).split()[1] == "HEAD"
    out = bundle.inspect_result(base_bundle, result, base, BRANCH, work_root=str(tmp_path / "w"), deliver_to=str(tmp_path / "out.bundle"))
    assert out["head"] == head and out["delivered"]["bytes"] == os.path.getsize(tmp_path / "out.bundle")
    assert _git(tmp_path, "bundle", "list-heads", str(tmp_path / "out.bundle"), cwd=repo).split()[1] == f"refs/heads/{BRANCH}"
    clone = tmp_path / "clone"                                   # `tour fetch` imports the delivered file as it is
    clone.mkdir()
    _git(tmp_path, "init", "-q", cwd=clone)
    subprocess.run(["git", "fetch", "-q", base_bundle, "refs/heads/*:refs/base/*"], cwd=clone, check=True, env={**GIT_ENV, "HOME": str(tmp_path)})
    assert bundle.import_bundle(str(tmp_path / "out.bundle"), BRANCH, str(clone)) == head


@linux_only
def test_a_token_on_an_extra_ref_is_refused_and_never_delivered(tmp_path):
    repo, base_bundle, base, head, _ = make_repo_bundles(tmp_path)
    _git(tmp_path, "checkout", "-q", "-b", "side", base, cwd=repo)
    (repo / "leak.txt").write_text("token=" + TOKEN + "\n")
    _git(tmp_path, "add", ".", cwd=repo)
    _git(tmp_path, "commit", "-q", "-m", "side", cwd=repo)
    result = tmp_path / "two-refs.bundle"
    _git(tmp_path, "bundle", "create", str(result), f"{base}..{BRANCH}", f"{base}..side", cwd=repo)
    assert "refs/heads/side" in _git(tmp_path, "bundle", "list-heads", str(result), cwd=repo)
    with pytest.raises(Broken, match="other than the guest's branch"):
        bundle.inspect_result(base_bundle, str(result), base, BRANCH, work_root=str(tmp_path / "w"), token=TOKEN,
                              deliver_to=str(tmp_path / "out.bundle"))
    assert not (tmp_path / "out.bundle").exists()


def craft_bundle_with_an_unreferenced_blob(tmp_path, repo, base, head, blob_text):
    """A bundle whose pack also holds a blob that no ref reaches (git bundle create never writes one; a guest can)."""
    env = {**GIT_ENV, "HOME": str(tmp_path)}
    blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=repo, input=blob_text, capture_output=True, check=True, env=env).stdout.decode().strip()
    objects = subprocess.run(["git", "rev-list", "--objects", head, "--not", base], cwd=repo, capture_output=True, check=True, env=env).stdout
    pack = subprocess.run(["git", "pack-objects", "--stdout", "-q"], cwd=repo, input=objects + blob.encode() + b"\n", capture_output=True,
                          check=True, env=env).stdout
    path = tmp_path / "padded.bundle"
    path.write_bytes(f"# v2 git bundle\n-{base} base\n{head} refs/heads/{BRANCH}\n\n".encode() + pack)
    return str(path), blob


@linux_only
def test_a_token_in_a_packed_object_no_ref_reaches_is_not_in_the_delivered_bundle(tmp_path):
    repo, base_bundle, base, head, _ = make_repo_bundles(tmp_path)
    padded, blob = craft_bundle_with_an_unreferenced_blob(tmp_path, repo, base, head, ("token=" + TOKEN + "\n").encode())
    out = bundle.inspect_result(base_bundle, padded, base, BRANCH, work_root=str(tmp_path / "w"), token=TOKEN,
                                deliver_to=str(tmp_path / "out.bundle"))
    assert out["token_found"] is False and out["head"] == head          # the extra blob is not part of the result
    env = {**GIT_ENV, "HOME": str(tmp_path)}
    for name, expected in (("padded.bundle", True), ("out.bundle", False)):
        fresh = tmp_path / ("fresh-" + name)
        fresh.mkdir()
        subprocess.run(["git", "init", "-q", "--bare", str(fresh)], check=True, env=env)
        subprocess.run(["git", "fetch", "-q", str(base_bundle), "refs/heads/*:refs/base/*"], cwd=fresh, check=True, env=env)
        subprocess.run(["git", "fetch", "-q", str(tmp_path / name), f"refs/heads/{BRANCH}:refs/r"], cwd=fresh, check=True, env=env)
        has = subprocess.run(["git", "cat-file", "-e", blob], cwd=fresh, env=env).returncode == 0
        assert has is expected, name


def test_the_pipeline_hands_over_the_rebuilt_bundle_and_records_its_digest(rig):
    import hashlib
    rebuilt = b"# v2 git bundle\nrebuilt-by-motel"

    def inspect(base_bundle, result_bundle, base_sha, branch, attempt, token=None, deliver_to=None):
        with open(deliver_to, "wb") as handle:
            handle.write(rebuilt)
        return {"head": "b" * 40, "changed_files": [], "changed_total": 0, "ahead": True, "token_found": False,
                "delivered": {"bytes": len(rebuilt), "sha256": hashlib.sha256(rebuilt).hexdigest()}}
    rig.env.inspect_bundle = inspect
    doc = rig.run_tour()
    record = rig.result_record(doc["id"], "fix-auth")
    assert record["verified"]["bundle"] == {"bytes": len(rebuilt), "sha256": hashlib.sha256(rebuilt).hexdigest()}
    assert rig.service.fetch(rig.alice, doc["id"], "fix-auth").data == rebuilt
    folder = os.path.join(rig.store.results_dir(doc["id"], "fix-auth"), record["attempt"])
    assert not any(n.endswith(".rebuilt") for n in os.listdir(folder))


# -- F2 (round 4): a stop that also breaks the step that was running is still a stop -------------------------------------

@pytest.mark.parametrize("failure", ["transport", "fail", "generic"])
def test_a_stop_that_also_breaks_the_pre_start_step_does_not_use_the_retry(rig, failure):
    from rig import SyncExecutor

    from motel.tours.errors import Broken
    from motel.tours.supervisor import Supervisor
    from motel.tours.transport import TransportError
    error = {"transport": TransportError("ssh died"), "fail": Broken("the cabin went away"), "generic": RuntimeError("boom")}[failure]

    def preflight(target, forward_port, **kw):
        rig.env.stop.set()                     # SIGTERM arrives ...
        raise error                            # ... and kills the ssh process of this step
    original = rig.transport.preflight
    rig.transport.preflight = preflight
    tid = rig.start()
    Supervisor(rig.env, tid, executor_factory=SyncExecutor).run()
    guest = rig.store.load(tid)["guests"]["fix-auth"]
    assert guest["status"] == "queued" and guest.get("retries_used", 0) == 0 and rig.servers_left() == []
    rig.env.stop.clear()
    rig.transport.preflight = original
    rig.supervise(tid)
    final = rig.store.load(tid)
    assert final["status"] == "done" and final["guests"]["fix-auth"].get("retries_used", 0) == 0


def test_the_same_failure_without_a_stop_still_uses_the_retry(rig):
    rig.transport.fail_preflight = 1
    doc = rig.run_tour()
    assert doc["guests"]["fix-auth"]["retries_used"] == 1 and doc["status"] == "done"


# -- F3 (round 4): the inspection setup is checked before a paid run ------------------------------------------------------

def _fake_root(monkeypatch, name="motel-inspect", uid=987, gid=986):
    import pwd
    monkeypatch.setattr(pwd, "getpwnam", lambda n: pwd.struct_passwd((n, "x", uid, gid, "", "/nonexistent", "/usr/sbin/nologin"))
                        if n == name else (_ for _ in ()).throw(KeyError(n)))


@linux_only
def test_setup_problems_name_the_user_the_directory_and_the_programs(tmp_path, monkeypatch):
    work = tmp_path / "inspect"
    assert bundle.setup_problems(str(work), euid=1000) == []                      # an unprivileged desk runs git as itself
    _fake_root(monkeypatch, name="someone-else")
    problems = bundle.setup_problems(str(work), euid=0)
    assert any("does not exist" in p and "install --upgrade" in p for p in problems)
    assert any(str(work) in p and "does not exist" in p for p in problems)
    _fake_root(monkeypatch)
    work.mkdir(mode=0o700)
    os.chmod(work, 0o700)
    problems = bundle.setup_problems(str(work), euid=0)
    assert any("mode 0711" in p and "install --upgrade" in p for p in problems) and not any("user" in p for p in problems)
    gone = bundle.setup_problems(str(work), euid=1000, which=lambda program, path=None: None)
    assert [p.split(" is ")[0] for p in gone] == ["git", "prlimit"]
    _fake_root(monkeypatch, uid=0, gid=0)
    assert any("is root" in p for p in bundle.setup_problems(str(work), euid=0))


@linux_only
def test_a_root_owned_0711_directory_and_an_unprivileged_user_pass(tmp_path, monkeypatch):
    work = tmp_path / "inspect"
    work.mkdir()
    os.chmod(work, 0o711)
    _fake_root(monkeypatch)
    monkeypatch.setattr(os, "lstat", lambda p, _real=os.lstat: _stat_as_root(_real(p)) if str(p) == str(work) else _real(p))
    assert bundle.setup_problems(str(work), euid=0) == []


def _stat_as_root(st):
    return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, 0, 0, st.st_size, st.st_atime, st.st_mtime, st.st_ctime))


def test_tour_start_refuses_before_anything_is_staged_when_the_inspection_user_is_missing(rig):
    from motel.tours.errors import Refused
    rig.service.setup_check = lambda: ["the inspection user 'motel-inspect' does not exist; run: sudo motel install --upgrade"]
    with pytest.raises(Refused) as err:
        rig.start()
    assert Refused.code == 3 and "install --upgrade" in str(err.value) and "install --upgrade" in err.value.hint
    assert rig.store.ids() == [] and rig.servers_left() == [] and rig.concierge.attempts == {}


@linux_only
def test_doctor_checks_the_inspection_setup_when_tours_are_on(monkeypatch):
    import io
    import json

    from test_tours_config import CABINS

    from helpers import BASE_TOML, Fixture
    from motel import desk
    fx = Fixture(BASE_TOML + CABINS + "[tours]\nenabled = true\n")
    try:
        def checks():
            out, err = io.StringIO(), io.StringIO()
            desk.run(["doctor", "--json"], paths=fx.paths, caller=fx.caller, make_provider=lambda cfg, paths: fx.provider,
                     sysops=fx.sysops, clock=fx.clock, out=out, err=err, concierge_health=lambda: {"lost_listeners": {}})
            return {c["check"]: c for c in json.loads(out.getvalue())["checks"]}
        assert checks()["tour bundle inspection is set up"]["level"] == "ok"
        monkeypatch.setattr(bundle, "setup_problems", lambda *a, **k: ["the inspection user 'motel-inspect' does not exist; run: sudo motel install --upgrade"])
        broken = checks()["tour bundle inspection is set up"]
        assert broken["level"] == "fail" and "install --upgrade" in broken["detail"]
    finally:
        fx.close()
    off = Fixture(BASE_TOML + CABINS)
    try:
        out, err = io.StringIO(), io.StringIO()
        desk.run(["doctor", "--json"], paths=off.paths, caller=off.caller, make_provider=lambda cfg, paths: off.provider,
                 sysops=off.sysops, clock=off.clock, out=out, err=err)
        assert "tour bundle inspection is set up" not in {c["check"] for c in json.loads(out.getvalue())["checks"]}
    finally:
        off.close()


# -- F5 (round 4): stale inspection scratch is reaped --------------------------------------------------------------------

OLD = 3600


def _scratch(root, attempt, suffix="abcd1234", age=OLD, files=("r/x",)):
    path = root / f"{attempt}-{suffix}"
    for rel in files:
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text("x")
    stamp = __import__("time").time() - age
    os.utime(path, (stamp, stamp))
    return path


def _statuses(**by_id):
    return by_id.get


def test_scratch_is_removed_only_when_old_enough_and_its_attempt_is_finished_or_unknown(tmp_path):
    import time
    root = tmp_path / "inspect"
    root.mkdir()
    finished = _scratch(root, "a-0123456789")
    unknown = _scratch(root, "a-aaaaaaaaaa")
    running = _scratch(root, "a-bbbbbbbbbb")
    young = _scratch(root, "a-cccccccccc", age=60)
    odd_name = _scratch(root, "not-an-attempt")
    status = _statuses(**{"a-0123456789": "failed", "a-bbbbbbbbbb": "running", "a-cccccccccc": "failed"})
    actions = bundle.reap_scratch(str(root), status, now=time.time(), apply=False)
    assert {a["dir"] for a in actions} == {finished.name, unknown.name} and all(p.exists() for p in (finished, unknown, running))
    actions = bundle.reap_scratch(str(root), status, now=time.time(), apply=True)
    assert not finished.exists() and not unknown.exists()
    assert running.exists() and young.exists() and odd_name.exists()           # running, too young, and not ours


def test_reaping_never_follows_a_link_and_only_touches_direct_children(tmp_path):
    import time
    root = tmp_path / "inspect"
    root.mkdir()
    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    link = root / "a-0123456789-abcd1234"              # a name that matches, but it is a symlink
    link.symlink_to(outside)
    stamp = time.time() - OLD
    os.utime(link, (stamp, stamp), follow_symlinks=False)
    nested = _scratch(root, "a-aaaaaaaaaa")
    (nested / "r" / "a-bbbbbbbbbb-abcd1234").mkdir()        # a matching name deeper down is not a direct child
    (nested / "inner").symlink_to(outside)
    os.utime(nested, (time.time() - OLD, time.time() - OLD))
    bundle.reap_scratch(str(root), _statuses(), now=time.time(), apply=True)
    assert (outside / "keep.txt").read_text() == "keep" and link.is_symlink()
    assert not nested.exists() and (outside / "keep.txt").exists()          # the link inside was removed, not followed


def test_reap_removes_stale_scratch_through_the_service_and_leaves_a_running_attempt_alone(rig, tmp_path):
    root = tmp_path / "inspect-root"
    root.mkdir()
    rig.service.inspect_root = str(root)
    tid = rig.start()
    running = rig.store.attempts(tid) or []
    assert running == []
    stale = _scratch(root, "a-0123456789")                  # no tour knows this attempt
    dry = rig.service.reap(False)
    assert {"action": "remove_inspect_scratch", "dir": stale.name, "attempt": "a-0123456789", "attempt_status": None} in dry["actions"]
    assert stale.exists()
    rig.service.reap(True)
    assert not stale.exists()


def test_a_restarted_supervisor_removes_its_own_tours_scratch_only(rig, tmp_path):
    root = tmp_path / "inspect-root"
    root.mkdir()
    rig.env.inspect_root = str(root)
    tid = rig.start()
    from motel.tours import recovery
    rec = {"id": "a-0123456789", "tour": tid, "guest": "fix-auth", "n": 1, "status": "failed", "cleanup": "deleted", "phase": "queued",
           "lease_id": None, "command_started_at": None, "pre_start": True, "reserved_eur": "0", "cost_eur": None, "usage": None,
           "error": None, "auth": "api-key", "created_at": "", "finished_at": None, "revoke_pending": False}
    rig.store.save_attempt(tid, rec)
    rig.store.update(tid, lambda d: d["guests"]["fix-auth"]["attempts"].append(rec["id"]))
    mine = _scratch(root, "a-0123456789", age=5)          # even a fresh one: this supervisor starts, nothing of its own runs
    other = _scratch(root, "a-ffffffffff", age=5)
    assert recovery
    rig.supervise(tid)
    assert not mine.exists() and other.exists()


def test_the_scratch_walk_stops_early_over_the_file_limit(tmp_path):
    for i in range(50):
        (tmp_path / f"d{i}").mkdir()
        (tmp_path / f"d{i}" / "f").write_text("x")
    size, files = bundle._scratch_use(str(tmp_path), 10)
    assert 10 < files < 100
    assert bundle._scratch_use(str(tmp_path))[1] == 100


def test_a_start_that_cannot_get_the_host_lock_says_the_host_is_busy(rig):
    import threading

    from motel.tours.errors import Refused
    rig.service.admission_wait_s = 0.2
    held, release = threading.Event(), threading.Event()

    def hold():
        with rig.store.admission("host"):
            held.set()
            release.wait(10)
    thread = threading.Thread(target=hold)
    thread.start()
    assert held.wait(10)
    try:
        with pytest.raises(Refused, match="busy"):
            rig.start()
    finally:
        release.set()
        thread.join()
    assert rig.store.ids() == [] or all(rig.store.load(t)["status"] != "staging" for t in rig.store.ids())
