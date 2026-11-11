"""SDK: Motel.tour(), Tour.as_completed/results/cancel/fetch, against a fake `motel` that replays canned answers."""
import json
import os
import sys

import pytest

from motel import Motel
from motel.sdk import BadReply, NoVacancy, Timeout, UsageError

FAKE = """#!{python}
import json, os, sys
d = os.environ["FAKE_DIR"]
args = [a for a in sys.argv[1:] if a != "--json"]
stdin = sys.stdin.read() if args[:2] == ["tour", "start"] and args[2:3] == ["-"] else ""
with open(d + "/log.jsonl", "a") as f:
    f.write(json.dumps({{"args": sys.argv[1:], "stdin": stdin, "cwd": os.getcwd()}}) + "\\n")
rules = json.load(open(d + "/rules.json"))
for i, rule in enumerate(rules):
    if args[:len(rule["match"])] == rule["match"]:
        seq = rule["seq"]
        n = rule.get("_n", 0)
        step = seq[min(n, len(seq) - 1)]
        rule["_n"] = n + 1
        json.dump(rules, open(d + "/rules.json", "w"))
        sys.stdout.write(step.get("stdout", ""))
        sys.exit(step.get("exit", 0))
sys.stderr.write("motel: no rule for %s\\n" % args)
sys.exit(2)
"""

TID = "t-3fa91c0b72"


def tour_doc(**statuses):
    guests = [{"id": g, "status": s, "cleanup": "deleted" if s != "running" else "live", "attempts": 1} for g, s in statuses.items()]
    done = all(s not in ("running", "queued") for s in statuses.values())
    return {"id": TID, "name": "demo", "status": "done" if done else "running", "cleanup": "deleted" if done else "live", "guests": guests}


def result_doc(guest, status="done", exit_code=0):
    return {"ok": True, "result": {"tour": TID, "guest": guest, "status": status, "cleanup": "deleted", "auth": "concierge",
                                   "branch": f"motel/{TID}/{guest}", "violations": [],
                                   "verified": {"exit_code": exit_code, "bundle": {"bytes": 1, "sha256": "a" * 64}, "cost_eur": "0.01"},
                                   "guest_claims": {"untrusted": True, "result": {"summary": f"{guest} says hi"}}}}


class Fake:
    def __init__(self, tmp_path):
        self.dir = tmp_path
        path = tmp_path / "motel"
        path.write_text(FAKE.format(python=sys.executable))
        path.chmod(0o755)
        self.client = Motel(str(path), env={**os.environ, "FAKE_DIR": str(tmp_path)})

    def rules(self, *rules):
        (self.dir / "rules.json").write_text(json.dumps([{"match": m, "seq": seq} for m, seq in rules]))

    def log(self):
        return [json.loads(line) for line in (self.dir / "log.jsonl").read_text().splitlines()]


@pytest.fixture
def fake(tmp_path):
    return Fake(tmp_path)


def out(doc, code=0):
    return {"stdout": json.dumps(doc), "exit": code}


def test_tour_starts_with_a_mapping_on_stdin(fake):
    fake.rules((["tour", "start", "-"], [out({"ok": True, "tour": tour_doc(a="queued", b="queued"), "warnings": ["w"]})]))
    spec = {"tour": {"name": "demo"}, "guests": [{"id": "a", "prompt": "x"}, {"id": "b", "prompt": "y"}]}
    tour = fake.client.tour(spec)
    assert tour.id == TID and [g.id for g in tour.guests] == ["a", "b"]
    call = fake.log()[0]
    assert call["args"] == ["tour", "start", "-", "--json"] and json.loads(call["stdin"]) == spec


def test_tour_starts_with_a_path_and_rejects_other_types(fake, tmp_path):
    fake.rules((["tour", "start"], [out({"ok": True, "tour": tour_doc(a="queued")})]))
    fake.client.tour(str(tmp_path / "tour.toml"))
    assert fake.log()[0]["args"] == ["tour", "start", str(tmp_path / "tour.toml"), "--json"]
    with pytest.raises(UsageError):
        fake.client.tour(42)
    with pytest.raises(UsageError):
        fake.client.tour({"guests": [object()]})


def test_a_refusal_raises_with_the_ceiling_in_the_message(fake):
    fake.rules((["tour", "start"], [{"stdout": json.dumps({"ok": False, "error": "ttl is above the ceiling of 2h", "hint": "lower ttl",
                                                           "code": 3}), "exit": 3}]))
    with pytest.raises(NoVacancy, match="ceiling of 2h"):
        fake.client.tour({"guests": [{"id": "a", "prompt": "x"}]})


def test_a_reply_without_a_tour_is_a_bad_reply(fake):
    fake.rules((["tour", "start"], [out({"ok": True})]))
    with pytest.raises(BadReply):
        fake.client.tour({"guests": [{"id": "a", "prompt": "x"}]})


def test_as_completed_yields_in_finishing_order_and_marks_claims_untrusted(fake):
    fake.rules(
        (["tour", "start", "-"], [out({"ok": True, "tour": tour_doc(a="running", b="running")})]),
        (["tour", "status"], [out({"ok": True, "tour": tour_doc(a="running", b="running")}),
                              out({"ok": True, "tour": tour_doc(a="running", b="done")}),
                              out({"ok": True, "tour": tour_doc(a="failed", b="done")})]),
        (["tour", "result", TID, "b"], [out(result_doc("b"))]),
        (["tour", "result", TID, "a"], [out(result_doc("a", "failed", 1))]))
    tour = fake.client.tour({"guests": [{"id": "a", "prompt": "x"}, {"id": "b", "prompt": "y"}]})
    sleeps = []
    results = list(tour.as_completed(poll_s=2, sleep=sleeps.append))
    assert [r.guest for r in results] == ["b", "a"] and sleeps == [2, 2]
    assert results[0].ok and results[0].has_commits and not results[1].ok
    assert results[0].guest_claims == {"untrusted": True, "result": {"summary": "b says hi"}}
    assert results[0].branch == f"motel/{TID}/b" and results[0].verified["cost_eur"] == "0.01"


def test_results_come_back_in_guest_order_and_as_completed_can_time_out(fake):
    fake.rules(
        (["tour", "start", "-"], [out({"ok": True, "tour": tour_doc(a="running", b="running")})]),
        (["tour", "status"], [out({"ok": True, "tour": tour_doc(a="done", b="done")})]),
        (["tour", "result"], [out(result_doc("a")), out(result_doc("b"))]))
    tour = fake.client.tour({"guests": [{"id": "a", "prompt": "x"}, {"id": "b", "prompt": "y"}]})
    assert [r.guest for r in tour.results(poll_s=0)] == ["a", "b"]
    fake.rules((["tour", "start", "-"], [out({"ok": True, "tour": tour_doc(a="running")})]),
               (["tour", "status"], [out({"ok": True, "tour": tour_doc(a="running")})]))
    slow = fake.client.tour({"guests": [{"id": "a", "prompt": "x"}]})
    clock = iter([0, 1, 100, 200])
    with pytest.raises(Timeout, match="keeps running"):
        list(slow.as_completed(timeout=10, sleep=lambda s: None, monotonic=lambda: next(clock)))


def test_wait_tolerates_exit_9_and_reports_false(fake):
    fake.rules((["tour", "start", "-"], [out({"ok": True, "tour": tour_doc(a="running")})]),
               (["tour", "wait"], [out({"ok": True, "satisfied": False, "tour": tour_doc(a="running")}, 9),
                                   out({"ok": True, "satisfied": True, "tour": tour_doc(a="done")})]))
    tour = fake.client.tour({"guests": [{"id": "a", "prompt": "x"}]})
    assert tour.wait(30) is False and tour.wait(30, any_guest=True) is True
    waits = [c["args"] for c in fake.log() if c["args"][1] == "wait"]
    assert waits == [["tour", "wait", TID, "--all", "--timeout", "30", "--json"], ["tour", "wait", TID, "--any", "--timeout", "30", "--json"]]


def test_cancel_and_fetch(fake, tmp_path):
    fake.rules((["tour", "start", "-"], [out({"ok": True, "tour": tour_doc(a="running")})]),
               (["tour", "cancel"], [out({"ok": True, "cancelling": True, "tour": tour_doc(a="cancelled")})]),
               (["tour", "fetch"], [out({"ok": True, "branch": f"motel/{TID}/a", "tip": "b" * 40, "artifacts": []})]))
    tour = fake.client.tour({"guests": [{"id": "a", "prompt": "x"}]})
    assert tour.cancel().guests[0].status == "cancelled" and tour.guests[0].finished
    reply = tour.fetch("a", str(tmp_path / "arts"), cwd=str(tmp_path))
    assert reply["branch"] == f"motel/{TID}/a"
    call = [c for c in fake.log() if c["args"][1] == "fetch"][0]
    assert call["args"][:5] == ["tour", "fetch", TID, "a", "--artifacts"] and call["cwd"] == str(tmp_path.resolve())


def test_ids_are_validated_before_anything_runs(fake):
    fake.rules()
    with pytest.raises(UsageError):
        fake.client.tour_status("not-a-tour")
    fake.rules((["tour", "start", "-"], [out({"ok": True, "tour": tour_doc(a="running")})]))
    tour = fake.client.tour({"guests": [{"id": "a", "prompt": "x"}]})
    for bad in ("../x", "A b", "--help", ""):
        with pytest.raises(UsageError):
            tour.result(bad)


def test_ls_and_status(fake):
    fake.rules((["tour", "ls"], [out({"ok": True, "tours": [tour_doc(a="done")]})]),
               (["tour", "status"], [out({"ok": True, "tour": tour_doc(a="done")})]))
    assert [t.id for t in fake.client.tours()] == [TID]
    assert fake.client.tour_status(TID).guests[0].finished is True
