"""Regression tests from the engine review: what the engine decides itself versus what a guest claims, retries,
the wait contract, collection edge cases and settlement."""
import hashlib
import json
import os

import doubles
import pytest
from rig import SyncExecutor

from motel.tours import artifacts as art
from motel.tours import ownlogin, recovery
from motel.tours.errors import NotFound
from motel.tours.supervisor import Supervisor
from platform_marks import linux_only


def failed_prestart_claim(card, files):
    """The agent ran (the engine had already saved command_started_at), then rewrote out/run.json."""
    out, _status = doubles.default_guest(card, files)
    run = json.loads(out["run.json"])
    run.update(termination="failed", reason="runner_error", command_started=False, exit_code=None,
               bundle_sha256=None, artifacts=[], changed_files=[])
    return {"run.json": json.dumps(run).encode(), "stdout.log": b"", "stderr.log": b""}, 0


# -- M1: the engine, not the guest, decides whether the command started ---------------------------------------------

def test_a_guest_written_not_started_claim_never_triggers_a_second_run(rig):
    rig.transport.guest = failed_prestart_claim
    doc = rig.run_tour()
    attempts = rig.attempts(doc["id"])
    assert len(attempts) == 1 and attempts[0]["command_started_at"] is not None
    assert len(rig.transport.leases_run) == 1 and len(rig.concierge.attempts) == 1
    assert doc["guests"]["fix-auth"]["status"] == "failed" and rig.servers_left() == []


def test_the_guest_is_stopped_before_its_output_is_listed_on_every_collection(rig):
    rig.run_tour()
    order = rig.log.kinds()
    assert order.index("run") < order.index("stop") < order.index("list_out") < order.index("fetch")


def test_a_failure_the_engine_saw_before_launching_the_runner_is_retried_once(rig):
    rig.transport.fail_preflight = 1
    doc = rig.run_tour()
    assert len(rig.attempts(doc["id"])) == 2 and doc["guests"]["fix-auth"]["status"] == "done"
    assert doc["guests"]["fix-auth"]["retries_used"] == 1


def test_a_second_pre_start_failure_is_final(rig):
    rig.transport.fail_preflight = 2
    doc = rig.run_tour()
    assert len(rig.attempts(doc["id"])) == 2 and doc["guests"]["fix-auth"]["status"] == "failed"
    assert rig.servers_left() == []


# -- L1: what motel measured and what the runner reported ------------------------------------------------------------

def test_head_and_changed_files_are_computed_from_the_bundle_not_taken_from_run_json(rig):
    def guest(card, files):
        out, status = doubles.default_guest(card, files)
        run = json.loads(out["run.json"])
        run.update(head="c" * 40, changed_files=["etc/passwd"])
        out["run.json"] = json.dumps(run).encode()
        return out, status
    rig.transport.guest = guest
    doc = rig.run_tour()
    record = rig.result_record(doc["id"], "fix-auth")
    assert record["verified"]["head"] == "b" * 40 and record["verified"]["changed_files"] == ["src/x.py"]
    assert "etc/passwd" not in json.dumps(record["verified"]) and "head" not in record["runner_reported"]
    assert any("different head" in n for n in record["notes"])
    assert set(record["runner_reported"]) == {"runner_exit", "exit_code", "termination"}


def test_a_bundle_that_does_not_verify_is_discarded(rig):
    rig.inspect_error = "the result bundle does not verify against the base commit"
    doc = rig.run_tour()
    record = rig.result_record(doc["id"], "fix-auth")
    assert record["verified"]["bundle"] is None and any("does not verify" in v for v in record["violations"])
    assert not os.path.exists(os.path.join(rig.store.results_dir(doc["id"], "fix-auth"), record["attempt"], "result.bundle"))


# -- L2 / L3: a guest that will be retried never looks finished -----------------------------------------------------

def test_wait_never_sees_a_terminal_status_for_a_guest_that_is_about_to_be_retried(rig, monkeypatch):
    rig.transport.fail_preflight = 1
    tid = rig.start()
    seen = []
    original = rig.store.event

    def event(tour_id, name, **fields):
        original(tour_id, name, **fields)
        if name == "attempt_done" and not seen:
            seen.append(rig.service.wait(rig.alice, tid, "all", timeout=0))
    monkeypatch.setattr(rig.store, "event", event)
    Supervisor(rig.env, tid, executor_factory=SyncExecutor).run()
    assert seen[0]["satisfied"] is False and seen[0]["tour"]["guests"][0]["status"] == "queued"
    assert rig.store.load(tid)["guests"]["fix-auth"]["status"] == "done"


def test_a_pre_start_attempt_left_by_a_dead_supervisor_is_retried_after_the_restart(rig, monkeypatch):
    tid = rig.start()
    from motel.tours import pipeline

    class Boom(BaseException):
        pass

    def die(self, ctx):
        raise Boom()
    monkeypatch.setattr(pipeline.AttemptRunner, "_phase_book", die)
    try:
        Supervisor(rig.env, tid, executor_factory=SyncExecutor).run()
    except Boom:
        pass
    rec = rig.attempts(tid)[0]
    rec.update(status="booking", phase="reserve", cleanup="deleted", finished_at=None)
    rig.store.save_attempt(tid, rec)

    def back(doc):
        doc["guests"]["fix-auth"]["status"] = "booking"
        doc["status"] = "running"
        doc["finished_at"] = None
        doc["settled"] = []                                       # a real crash happens before the attempt settles
    rig.store.update(tid, back)
    monkeypatch.undo()
    rig.supervise(tid)                                            # the restarted supervisor
    doc = rig.store.load(tid)
    assert doc["guests"]["fix-auth"]["status"] == "done" and doc["guests"]["fix-auth"]["retries_used"] == 1
    assert len(rig.attempts(tid)) == 2


def test_a_guest_that_failed_before_the_start_and_was_not_requeued_is_requeued_on_restart(rig):
    rig.transport.fail_preflight = 1
    tid = rig.start()
    runner = rig.runner()
    runner.run(tid, "fix-auth", 1)                                # fails before the start; settles as "queued" (retry)

    def stale(d):                                                 # the state an older supervisor could leave behind
        d["guests"]["fix-auth"].update(status="failed", retries_used=0)
    rig.store.update(tid, stale)
    assert recovery.requeue_failed_before_start(runner, tid) == ["fix-auth"]
    assert rig.store.load(tid)["guests"]["fix-auth"]["status"] == "queued"
    assert recovery.requeue_failed_before_start(runner, tid) == []         # only once: retries_used is now 1


# -- L4: collection edge cases ----------------------------------------------------------------------------------------

def test_a_duplicated_artifact_declaration_keeps_the_first_good_copy(rig):
    def guest(card, files):
        out, status = doubles.default_guest(card, files)
        run = json.loads(out["run.json"])
        run["artifacts"] = run["artifacts"] * 2
        out["run.json"] = json.dumps(run).encode()
        return out, status
    rig.transport.guest = guest
    doc = rig.run_tour(artifacts=["reports/*.json"])
    record = rig.result_record(doc["id"], "fix-auth")
    assert [a["path"] for a in record["verified"]["artifacts"]] == ["reports/a.json"]
    base = os.path.join(rig.store.results_dir(doc["id"], "fix-auth"), record["attempt"])
    assert os.path.exists(os.path.join(base, "artifacts", "reports", "a.json"))
    assert any("more than once" in v for v in record["violations"])
    assert rig.service.fetch(rig.alice, doc["id"], "fix-auth", "reports/a.json").data == b'{"ok": true}'


def test_a_deeply_nested_result_json_is_refused_without_an_internal_error(rig):
    def guest(card, files):
        out, status = doubles.default_guest(card, files)
        out["RESULT.json"] = b"[" * 30000 + b"]" * 30000
        return out, status
    rig.transport.guest = guest
    doc = rig.run_tour(artifacts=["reports/*.json"])
    record = rig.result_record(doc["id"], "fix-auth")
    assert record["status"] == "done" and record["guest_claims"]["result"] is None
    assert any("nested too deeply" in v for v in record["violations"])
    assert [a["path"] for a in record["verified"]["artifacts"]] == ["reports/a.json"]      # later files still collected


def test_a_file_and_a_directory_of_one_name_do_not_break_collection(rig):
    def guest(card, files):
        out, status = doubles.default_guest(card, files)
        a, b = b"1", b"2"
        out["artifacts/reports/x.json"] = a
        out["artifacts/reports/x.json/y.json"] = b
        run = json.loads(out["run.json"])
        run["artifacts"] += [{"path": "reports/x.json", "bytes": 1, "sha256": hashlib.sha256(a).hexdigest()},
                             {"path": "reports/x.json/y.json", "bytes": 1, "sha256": hashlib.sha256(b).hexdigest()}]
        out["run.json"] = json.dumps(run).encode()
        return out, status
    rig.transport.guest = guest
    doc = rig.run_tour(artifacts=["reports/*.json"])
    record = rig.result_record(doc["id"], "fix-auth")
    assert record["status"] == "done" and any("clashes" in v for v in record["violations"])
    assert sorted(a["path"] for a in record["verified"]["artifacts"]) == ["reports/a.json", "reports/x.json"]


def test_one_item_that_cannot_be_written_does_not_drop_the_others(rig, monkeypatch):
    from motel.tours import pipeline
    real = pipeline.write_new_file

    def flaky(dfd, name, chunks, mode=0o600):
        if name == "a.json":
            raise OSError("disk trouble")
        return real(dfd, name, chunks, mode)
    monkeypatch.setattr(pipeline, "write_new_file", flaky)
    doc = rig.run_tour(artifacts=["reports/*.json"])
    record = rig.result_record(doc["id"], "fix-auth")
    assert record["verified"]["bundle"] is not None and record["verified"]["artifacts"] == []
    assert any("a.json" in v for v in record["violations"])


def test_json_nesting_has_a_limit_but_ordinary_documents_parse():
    assert art.loads_limited(b'{"a": [1, {"b": "]]]"}]}') == {"a": [1, {"b": "]]]"}]}
    with pytest.raises(ValueError):
        art.loads_limited(b"[" * 40 + b"]" * 40)
    assert art.loads_limited(b'"' + b"[" * 100 + b'"') == "[" * 100              # brackets inside a string do not count


def test_a_long_log_is_truncated_not_dropped(rig):
    def guest(card, files):
        out, status = doubles.default_guest(card, files)
        out["stdout.log"] = b"x" * (art.MAX_LOG + 500)
        return out, status
    rig.transport.guest = guest
    doc = rig.run_tour()
    record = rig.result_record(doc["id"], "fix-auth")
    log = os.path.join(rig.store.results_dir(doc["id"], "fix-auth"), record["attempt"], "stdout.log")
    assert os.path.getsize(log) == art.MAX_LOG and record["logs"]["stdout"] is True


# -- M2 (engine half): RESULT.json is fetched only when the runner says it validated it ---------------------------------

def test_result_json_is_ignored_unless_run_json_reports_it_validated(rig):
    def guest(card, files):
        out, status = doubles.default_guest(card, files)
        run = json.loads(out["run.json"])
        run["result_json"] = False
        out["run.json"] = json.dumps(run).encode()
        return out, status
    rig.transport.guest = guest
    doc = rig.run_tour()
    record = rig.result_record(doc["id"], "fix-auth")
    assert record["guest_claims"]["result"] is None and "RESULT.json" not in rig.log.fetched()
    assert any("RESULT.json" in n for n in record["notes"])


# -- L9: the own-login token never survives in any collected item ----------------------------------------------------------

OWN = [{"id": "me", "prompt": "do it", "auth": "own-login", "secrets": []}]


def enable(rig):
    rig.limits = rig.limits.__class__(**{**rig.limits.__dict__, "own_login_enabled": True, "own_login_allow_users": ("alice",)})
    rig.service.limits = rig.env.limits = rig.limits
    ownlogin.set_token(rig.token_file, (doubles.OWN_TOKEN + "\n").encode())


def token_guest(**places):
    def guest(card, files):
        out, status = doubles.default_guest(card, files)
        run = json.loads(out["run.json"])
        for path, data in places.items():
            out[path] = data
        out["run.json"] = json.dumps(run).encode()
        return out, status
    return guest


def test_result_json_with_the_token_in_an_escaped_spelling_is_withheld(rig):
    enable(rig)
    escaped = "".join(f"\\u{ord(c):04x}" for c in doubles.OWN_TOKEN)
    rig.transport.guest = token_guest(**{"RESULT.json": ('{"note": "' + escaped + '"}').encode()})
    doc = rig.run_tour(OWN)
    record = rig.result_record(doc["id"], "me")
    assert record["guest_claims"]["result"] is None and any("RESULT.json" in v and "withheld" in v for v in record["violations"])
    assert doubles.OWN_TOKEN not in rig.tree_text() and doc["guests"]["me"]["status"] == "failed"


def test_an_artifact_or_bundle_that_contains_the_token_is_withheld(rig):
    enable(rig)

    def guest(card, files):
        out, status = doubles.default_guest(card, files)
        leak = b"prefix " + doubles.OWN_TOKEN.encode() + b" suffix"
        out["artifacts/reports/a.json"] = leak
        run = json.loads(out["run.json"])
        run["artifacts"] = [{"path": "reports/a.json", "bytes": len(leak), "sha256": hashlib.sha256(leak).hexdigest()}]
        bundle = doubles.bundle_bytes(doubles.OWN_TOKEN.encode())
        out["result.bundle"] = bundle
        run["bundle_sha256"] = hashlib.sha256(bundle).hexdigest()
        out["run.json"] = json.dumps(run).encode()
        return out, status
    rig.transport.guest = guest
    doc = rig.run_tour(OWN, artifacts=["reports/*.json"])
    record = rig.result_record(doc["id"], "me")
    assert record["verified"]["artifacts"] == [] and record["verified"]["bundle"] is None
    assert doubles.OWN_TOKEN not in rig.tree_text()


def test_a_log_cut_in_the_middle_of_the_token_does_not_keep_the_start_of_it():
    token = doubles.OWN_TOKEN
    cut = b"hello " + token.encode()[:15]
    assert ownlogin.redact(cut, token, tail=True) == b"hello [redacted]"
    assert ownlogin.redact(b"hello " + token.encode() + b" bye", token, tail=True) == b"hello [redacted] bye"
    assert ownlogin.redact(b"ends with a", token, tail=True) == b"ends with a"      # a short coincidence is not cut


def test_a_token_split_across_chunks_is_still_seen_by_the_scan():
    token, found = doubles.OWN_TOKEN, []
    data = b"zz" + token.encode() + b"zz"
    list(ownlogin.scan([data[:10], data[10:20], data[20:]], token, found))
    assert found


def test_recovery_deletes_the_runner_log_of_an_own_login_attempt(rig):
    enable(rig)
    tid = rig.start(OWN)
    rec = {"id": "a-0000000001", "tour": tid, "guest": "me", "n": 1, "status": "running", "cleanup": "deleted", "phase": "running",
           "lease_id": None, "command_started_at": "2026-11-09T10:00:00Z", "auth": "own-login", "reserved_eur": "0"}
    rig.store.save_attempt(tid, rec)
    folder = os.path.join(rig.store.results_dir(tid, "me", create=True), rec["id"])
    os.makedirs(folder)
    with open(os.path.join(folder, "runner.log"), "wb") as handle:
        handle.write(b"partial " + doubles.OWN_TOKEN.encode()[:12])

    def register(doc):
        doc["guests"]["me"]["attempts"].append(rec["id"])
    rig.store.update(tid, register)
    recovery.recover_attempt(rig.runner(), rig.store.load(tid), rec)
    assert not os.path.exists(os.path.join(folder, "runner.log"))


# -- real git: head and changed files come from the bundle itself ---------------------------------------------------------

@linux_only
def test_the_real_inspection_reads_head_and_changed_files_from_a_real_bundle(tmp_path):
    import subprocess

    from motel.tours import bundle
    git_env = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(tmp_path), "GIT_AUTHOR_NAME": "a",
               "GIT_AUTHOR_EMAIL": "a@example.invalid", "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@example.invalid"}

    def git(*args, cwd):
        return subprocess.run(["git", *args], cwd=cwd, env=git_env, check=True, capture_output=True, text=True).stdout.strip()
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    (repo / "a.txt").write_text("one\n")
    git("add", ".", cwd=repo)
    git("commit", "-q", "-m", "base", cwd=repo)
    base = git("rev-parse", "HEAD", cwd=repo)
    base_bundle = tmp_path / "base.bundle"
    bundle.make_bundle(str(repo), base, str(base_bundle))
    tid, gid = "t-0123456789", "fixer"
    branch = f"motel/{tid}/{gid}"
    git("checkout", "-q", "-b", branch, cwd=repo)
    (repo / "b.txt").write_text("two\n")
    (repo / "a.txt").write_text("changed\n")
    git("add", ".", cwd=repo)
    git("commit", "-q", "-m", "work", cwd=repo)
    head = git("rev-parse", "HEAD", cwd=repo)
    result_bundle = tmp_path / "result.bundle"
    git("bundle", "create", str(result_bundle), f"{base}..{branch}", cwd=repo)
    work = tmp_path / "tmp"
    work.mkdir()
    out = bundle.inspect_result(str(base_bundle), str(result_bundle), base, branch, work_root=str(work))
    assert out["head"] == head and sorted(out["changed_files"]) == ["a.txt", "b.txt"] and out["ahead"] is True
    assert os.listdir(work) == []                                                # the scratch repository is gone
    other = "t-0123456789/other"
    with pytest.raises(Exception, match="branch"):
        bundle.inspect_result(str(base_bundle), str(result_bundle), base, f"motel/{other}", work_root=str(work))
    junk = tmp_path / "junk.bundle"
    junk.write_bytes(b"# v2 git bundle\nnonsense")
    with pytest.raises(Exception, match="verify|branch|no ref"):
        bundle.inspect_result(str(base_bundle), str(junk), base, branch, work_root=str(work))


def test_a_missing_input_is_reported_not_crashed_on(rig):
    rig.inspect_error = "the caller's base history could not be loaded for inspection"
    doc = rig.run_tour()
    assert doc["guests"]["fix-auth"]["status"] in ("done", "failed")
    with pytest.raises(NotFound):
        rig.service.fetch(rig.alice, doc["id"], "fix-auth", "nothing/here")


# -- max_output_tokens: a spec field that reaches the card only when it is set ---------------------------------------------

def test_max_output_tokens_is_validated_and_reaches_the_card_only_when_set(rig):
    from motel.tours import spec
    from motel.tours.errors import Usage
    for bad in (0, 255, 128_001, True, "8000"):
        with pytest.raises(Usage, match="max_output_tokens"):
            spec.parse({"tour": {"name": "x"}, "defaults": {"max_output_tokens": bad},
                        "guests": [{"id": "a", "prompt": "p", "secrets": ["anthropic"], "budget_usd": 1}]})
    rig.run_tour()
    assert "max_output_tokens" not in rig.transport.cards[0]
    rig.run_tour([{"id": "fix-auth", "prompt": "p", "secrets": ["anthropic"], "budget_usd": 0.5}], max_output_tokens=4000)
    assert rig.transport.cards[1]["max_output_tokens"] == 4000
    rig.run_tour([{"id": "fix-auth", "prompt": "p", "secrets": ["anthropic"], "budget_usd": 0.5, "max_output_tokens": 2000}],
                 max_output_tokens=4000)
    assert rig.transport.cards[2]["max_output_tokens"] == 2000


def test_a_runner_that_withheld_output_for_a_secret_leak_is_reported_as_such(rig):
    enable(rig)

    def guest(card, files):
        out, status = doubles.default_guest(card, files)
        run = json.loads(out["run.json"])
        run.update(bundle_sha256=None, artifacts=[], termination="failed", reason="secret_leak",
                   rejected=[{"path": "result.bundle", "reason": "secret_leak"}])
        out["run.json"] = json.dumps(run).encode()
        del out["result.bundle"]
        return out, 1
    rig.transport.guest = guest
    doc = rig.run_tour(OWN)
    record = rig.result_record(doc["id"], "me")
    assert doc["guests"]["me"]["status"] == "failed" and record["verified"]["bundle"] is None
    assert record["run"]["reason"] == "secret_leak" and record["run"]["rejected"] == [{"path": "result.bundle", "reason": "secret_leak"}]
    assert len(rig.attempts(doc["id"])) == 1                      # never retried: the runner had been launched
