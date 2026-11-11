"""A guest that is stuck booking says why, and a cancel or a stop does not have to sit out the bootstrap deadline."""
from datetime import timedelta

STUCK = {"schema": "motel-guest/v1", "bootstrap": "running",
         "boot": {"stage": "tour-firewall", "cloud_init": "error", "last": "failed: line 28: systemctl enable --now motel-tour-firewall.service"}}
ONE = [{"id": "g", "adapter": "command", "argv": ["true"], "secrets": []}]
STAGE = "stage tour-firewall; cloud-init error; last line: failed: line 28: systemctl enable --now motel-tour-firewall.service"


def after_calls(rig, n, action):
    """Run ``action`` at the n-th guest query of the whole test (a cancel, a stop) while the cabin is still booking."""
    original, count = rig.world.guest.call, [0]

    def call(rec, *words, **kw):
        count[0] += 1
        if count[0] == n:
            action()
        return original(rec, *words, **kw)
    rig.world.guest.call = call


def no_cabin_resource_left(rig):
    hz = rig.world.hz
    return not hz.servers and not hz.firewalls and not hz.primary_ips and not hz.volumes


# ------------------------------------------------------------------ the leak: a cancel while the cabin is booking

def test_a_cancel_while_the_cabin_is_booking_deletes_it_at_once_not_at_the_bootstrap_deadline(rig):
    rig.world.guest.ready_doc = STUCK
    tid = rig.start(ONE)
    after_calls(rig, 3, lambda: rig.store.request_cancel(tid))
    started = rig.clock()
    rig.supervise(tid)
    doc = rig.store.load(tid)
    assert doc["status"] == "cancelled" and doc["guests"]["g"]["status"] == "cancelled" and doc["guests"]["g"]["cleanup"] == "deleted"
    assert rig.clock() - started < timedelta(minutes=3)                 # the deadline is 20 minutes
    assert no_cabin_resource_left(rig)
    assert len(rig.attempts(tid)) == 1 and "run" not in rig.log.kinds()
    assert rig.attempts(tid)[0]["boot"]["ready"]["boot"]["stage"] == "tour-firewall"


def test_a_cancel_during_the_retrys_bootstrap_leaves_nothing_behind(rig):
    """The first cabin times out (20 minutes), the retry's cabin is booking when the cancel comes: both are gone."""
    rig.world.guest.ready_doc = STUCK
    tid = rig.start(ONE)
    after_calls(rig, 125, lambda: rig.store.request_cancel(tid))        # 120 polls fill the first deadline; this is the retry's 5th
    rig.supervise(tid)
    doc = rig.store.load(tid)
    attempts = rig.attempts(tid)
    assert [a["status"] for a in attempts] == ["failed", "cancelled"] and all(a["cleanup"] == "deleted" for a in attempts)
    assert doc["status"] == "cancelled" and doc["guests"]["g"]["status"] == "cancelled" and doc["guests"]["g"]["retries_used"] == 1
    assert no_cabin_resource_left(rig)
    assert doc["budget"]["reserved_eur"] == "0"


def test_a_stop_while_the_cabin_is_booking_requeues_the_guest_without_using_its_retry(rig):
    rig.world.guest.ready_doc = STUCK
    tid = rig.start(ONE)
    after_calls(rig, 3, rig.env.stop.set)
    rig.supervise(tid)
    doc = rig.store.load(tid)
    assert doc["status"] == "running" and doc["guests"]["g"]["status"] == "queued" and doc["guests"]["g"].get("retries_used", 0) == 0
    assert no_cabin_resource_left(rig)
    [attempt] = rig.attempts(tid)
    assert attempt["status"] == "failed" and attempt["cleanup"] == "deleted" and attempt["pre_start"] is True


# ------------------------------------------------------------------ the attempt record, `tour status`, `tour logs`

def stuck_tour(rig):
    rig.world.guest.ready_doc = STUCK
    tid = rig.start(ONE)
    rig.supervise(tid)
    return tid


def test_an_attempt_that_never_got_ready_records_why(rig):
    tid = stuck_tour(rig)
    attempts = rig.attempts(tid)
    assert [a["status"] for a in attempts] == ["failed", "failed"]            # the retry timed out too
    for a in attempts:
        assert f"bootstrap deadline passed: guest bootstrap running ({STAGE})" in a["error"]
        boot = a["boot"]
        assert boot["polls"] >= 100 and boot["summary"].startswith("guest bootstrap running") and boot["ready"]["boot"]["cloud_init"] == "error"
        assert boot["channel"] is None and boot["at"]


def test_the_attempt_record_names_the_last_guest_channel_error_and_the_probe(rig):
    from motel.cabins.errors import GuestUnknown

    def refuse(rec, *words, timeout=60):
        raise GuestUnknown("guest query exit 255: ssh: connect to host 203.0.113.10 port 22: Connection refused", rc=255,
                           detail="ssh: connect to host 203.0.113.10 port 22: Connection refused")
    rig.world.guest.call = refuse
    original = rig.env.controller_factory
    rig.env.controller_factory = lambda owner, cleanup_only=False: _with_probe(original(owner, cleanup_only))
    tid = rig.start(ONE)
    rig.supervise(tid)
    boot = rig.attempts(tid)[0]["boot"]
    assert boot["channel"]["rc"] == 255 and boot["channel"]["tcp22"] == "timeout" and "Connection refused" in boot["channel"]["error"]
    assert "tcp/22 timeout" in rig.attempts(tid)[0]["error"]


def _with_probe(controller):
    controller.tcp_probe = lambda ip, port: "timeout"
    return controller


def test_tour_status_shows_why_a_guest_is_not_running(rig):
    rig.world.guest.ready_doc = STUCK
    tid = rig.start(ONE)
    after_calls(rig, 5, lambda: seen.append(rig.service.status(rig.alice, tid)))
    seen = []
    rig.supervise(tid)
    [attempt] = seen[0]["tour"]["guests"][0]["attempt_list"]
    assert attempt["phase"] == "checkin" and attempt["boot"]["summary"].endswith(f"({STAGE})") and attempt["error"] is None
    final = rig.service.status(rig.alice, tid)["tour"]["guests"][0]["attempt_list"]
    assert [a["status"] for a in final] == ["failed", "failed"] and all("bootstrap deadline passed" in a["error"] for a in final)
    assert all(a["created_at"] and a["intent_at"] and a["finished_at"] for a in final)


def test_the_text_status_adds_a_line_for_a_guest_that_never_ran(rig):
    from motel.tours.cli import render_tour
    tid = stuck_tour(rig)
    text = render_tour(rig.service.status(rig.alice, tid)["tour"])
    assert "bootstrap deadline passed" in text and "checkin" in text


def test_tour_logs_of_a_guest_that_never_ran_report_the_boot(rig):
    tid = stuck_tour(rig)
    for reply in (rig.service.logs(rig.alice, tid, "g"), rig.service.logs(rig.alice, tid, "g", stream="boot")):
        assert reply["stream"] == "boot"                                 # not "stdout": it is not the command's output
        assert reply["untrusted"] is True and reply["boot"]["boot"]["ready"]["boot"]["stage"] == "tour-firewall"
        assert STAGE in reply["text"] and reply["text"].count("attempt a-") == 2 and "bootstrap deadline passed" in reply["text"]


def test_the_boot_report_is_dropped_once_the_bootstrap_succeeds(rig):
    """A guest that was still booking for a few polls and then came up carries no boot report (docs/cli-json.md: only while unfinished)."""
    rig.world.guest.ready_doc = STUCK
    after_calls(rig, 4, lambda: setattr(rig.world.guest, "ready_doc", None))
    doc = rig.run_tour(ONE)
    assert doc["status"] == "done"
    assert all("boot" not in a for a in rig.attempts(doc["id"]))
    status = rig.service.status(rig.alice, doc["id"])["tour"]
    assert all(a["boot"] is None for g in status["guests"] for a in g["attempt_list"])


def test_tour_logs_of_a_guest_that_ran_stay_its_own_output(rig):
    doc = rig.run_tour(ONE)
    assert doc["status"] == "done"
    reply = rig.service.logs(rig.alice, doc["id"], "g")
    assert "boot" not in reply and "attempt a-" not in reply["text"]
