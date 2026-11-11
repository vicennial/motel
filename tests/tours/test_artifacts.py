"""Artifact validation: pure planning, and what the pipeline does with a hostile out/ directory."""
import hashlib
import json

import doubles
import pytest

from motel.tours import artifacts as art

SHA = "a" * 64


def run_doc(**over):
    base = {"exit_code": 0, "termination": "exited", "started": "", "finished": "", "head": None, "bundle_sha256": None,
            "changed_files": [], "artifacts": [], "stdout_bytes": 0, "stderr_bytes": 0}
    base.update(over)
    return base


def entries(**kinds):
    return [art.Entry(path=p.replace("__", "/"), kind=k[0], size=k[1]) for p, k in kinds.items()]


@pytest.mark.parametrize("path,ok", [("a.json", True), ("reports/a.json", True), ("a/b/c.txt", True), ("", False),
                                     ("/etc/passwd", False), ("../x", False), ("a/../x", False), ("a//b", False),
                                     ("a/./b", False), ("a b", False), ("a\nb", False), ("a/" + "x" * 300, False),
                                     ("~/x", False), (".", False), ("a\\b", False), (None, False), (3, False)])
def test_safe_relpath(path, ok):
    assert art.safe_relpath(path) is ok


def test_only_regular_files_are_planned():
    listing = [art.Entry("result.bundle", "f", 10), art.Entry("stdout.log", "l", 5), art.Entry("artifacts/p", "p", 0),
               art.Entry("artifacts/s", "s", 0), art.Entry("artifacts/d", "b", 0), art.Entry("artifacts/ok.txt", "f", 3)]
    doc = run_doc(bundle_sha256=SHA, artifacts=[{"path": "ok.txt", "bytes": 3, "sha256": SHA},
                                                {"path": "p", "bytes": 0, "sha256": SHA},
                                                {"path": "s", "bytes": 0, "sha256": SHA}])
    plan = art.plan_collection(listing, doc, ["*"], 1 << 20)
    assert [i.remote for i in plan.items] == ["result.bundle", "artifacts/ok.txt"]
    text = "\n".join(plan.violations)
    assert "pipe" in text and "socket" in text and "block device" in text and "symbolic link" in text


def test_declared_artifacts_must_match_the_requested_patterns():
    listing = [art.Entry("artifacts/a.json", "f", 3), art.Entry("artifacts/b.exe", "f", 3)]
    doc = run_doc(artifacts=[{"path": "a.json", "bytes": 3, "sha256": SHA}, {"path": "b.exe", "bytes": 3, "sha256": SHA}])
    plan = art.plan_collection(listing, doc, ["*.json"], 1 << 20)
    assert [i.remote for i in plan.items] == ["artifacts/a.json"]
    assert any("b.exe" in v and "patterns" in v for v in plan.violations)


def test_undeclared_files_are_ignored_and_traversal_is_refused():
    listing = [art.Entry("artifacts/a.json", "f", 3), art.Entry("artifacts/x.json", "f", 3), art.Entry("../escape", "f", 1)]
    doc = run_doc(artifacts=[{"path": "a.json", "bytes": 3, "sha256": SHA}, {"path": "../escape", "bytes": 1, "sha256": SHA}])
    plan = art.plan_collection(listing, doc, ["*"], 1 << 20)
    assert [i.remote for i in plan.items] == ["artifacts/a.json"]
    assert any("unsafe" in v for v in plan.violations) and any("undeclared" in n for n in plan.notes)


def test_count_and_size_caps():
    many = [{"path": f"f{i}.txt", "bytes": 1, "sha256": SHA} for i in range(5)]
    listing = [art.Entry(f"artifacts/f{i}.txt", "f", 1) for i in range(5)]
    plan = art.plan_collection(listing, run_doc(artifacts=many), ["*"], 1 << 20, max_files=3)
    assert len(plan.items) == 3 and any("more than 3" in v for v in plan.violations)
    big = [art.Entry("artifacts/big", "f", 100)]
    plan = art.plan_collection(big, run_doc(artifacts=[{"path": "big", "bytes": 100, "sha256": SHA}]), ["*"], 50)
    assert plan.items == [] and any("over the size limit" in v for v in plan.violations)


def test_a_declared_file_that_is_missing_is_a_violation():
    plan = art.plan_collection([], run_doc(bundle_sha256=SHA), ["*"], 1 << 20)
    assert any("missing" in v for v in plan.violations)


@pytest.mark.parametrize("change", [{"schema": "x"}, {"attempt": "other"}, {"termination": "weird"}, {"exit_code": "0"},
                                    {"head": "zz"}, {"bundle_sha256": "short"}, {"changed_files": "x"},
                                    {"artifacts": [{"path": "a", "sha256": "bad"}]}, {"artifacts": ["a"]}])
def test_bad_run_json_is_rejected(change):
    doc = {"schema": art.RUN_SCHEMA, "attempt": "a-1", "termination": "exited", "exit_code": 0}
    doc.update(change)
    with pytest.raises(art.BadRunDoc):
        art.validate_run_doc(doc, "a-1")


# -- through the pipeline ----------------------------------------------------------------------------------------------

def hostile_guest(extra, **declared):
    def guest(card, files):
        out, status = doubles.default_guest(card, files)
        out.update(extra)
        run = json.loads(out["run.json"])
        run.update(declared)
        out["run.json"] = json.dumps(run).encode()
        return out, status
    return guest


def test_special_files_in_out_are_never_fetched(rig):
    rig.transport.guest = hostile_guest({"artifacts/evil": ("l", b"/etc/shadow"), "artifacts/pipe": ("p", b""), "stderr.log": ("l", b"x")},
                                        artifacts=[{"path": "evil", "bytes": 1, "sha256": SHA}])
    doc = rig.run_tour(artifacts=["*"])
    record = rig.result_record(doc["id"], "fix-auth")
    assert record["violations"] and "evil" not in [a["path"] for a in record["verified"]["artifacts"]]
    assert ("fetch", "artifacts/evil") not in rig.log and ("fetch", "stderr.log") not in rig.log


def test_a_digest_mismatch_is_a_violation_and_the_file_is_dropped(rig):
    def guest(card, files):
        out, status = doubles.default_guest(card, files)
        run = json.loads(out["run.json"])
        run["artifacts"][0]["sha256"] = hashlib.sha256(b"something else").hexdigest()
        out["run.json"] = json.dumps(run).encode()
        return out, status
    rig.transport.guest = guest
    doc = rig.run_tour(artifacts=["reports/*.json"])
    record = rig.result_record(doc["id"], "fix-auth")
    assert record["verified"]["artifacts"] == [] and any("digest" in v or "sha256" in v for v in record["violations"])


def test_a_bundle_that_is_not_a_git_bundle_is_a_violation(rig):
    def guest(card, files):
        out, status = doubles.default_guest(card, files)
        out["result.bundle"] = b"not a bundle at all"
        run = json.loads(out["run.json"])
        run["bundle_sha256"] = hashlib.sha256(out["result.bundle"]).hexdigest()
        out["run.json"] = json.dumps(run).encode()
        return out, status
    rig.transport.guest = guest
    doc = rig.run_tour()
    record = rig.result_record(doc["id"], "fix-auth")
    assert record["verified"]["bundle"] is None and record["violations"]


def test_a_missing_or_malformed_run_json_leaves_the_exit_status_unverified(rig):
    def guest(card, files):
        out, status = doubles.default_guest(card, files)
        out["run.json"] = b"{not json"
        return out, status
    rig.transport.guest = guest
    doc = rig.run_tour()
    record = rig.result_record(doc["id"], "fix-auth")
    assert record["runner_reported"]["exit_code"] is None and record["violations"]
    assert doc["guests"]["fix-auth"]["status"] == "failed"


def test_an_oversize_artifact_is_refused(rig):
    rig.limits = rig.limits.__class__(**{**rig.limits.__dict__, "max_result_mb": 1})
    rig.env.limits = rig.limits
    big = b"x" * (2 * 1024 * 1024)
    rig.transport.guest = hostile_guest({"artifacts/reports/big.json": big},
                                        artifacts=[{"path": "reports/big.json", "bytes": len(big), "sha256": hashlib.sha256(big).hexdigest()}])
    doc = rig.run_tour(artifacts=["reports/*.json"])
    record = rig.result_record(doc["id"], "fix-auth")
    assert all(a["path"] != "reports/big.json" for a in record["verified"]["artifacts"])
    assert any("size" in v for v in record["violations"])
