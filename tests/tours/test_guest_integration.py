"""The engine against the guest part's real egress profile, runner contract and failure reasons."""
import json

import doubles
import pytest
from rig import file_block

from motel.cabins.userdata import USER_DATA_LIMIT
from motel.tours import artifacts as art
from motel.tours.errors import Refused

TWO = [{"id": "a", "prompt": "do a", "secrets": ["anthropic"], "budget_usd": 0.5}]


def failing_guest(reason, started=False, exit_code=None, **extra):
    def guest(card, files):
        out, status = doubles.default_guest(card, files)
        run = json.loads(out["run.json"])
        run.update(termination="failed", reason=reason, command_started=started, exit_code=exit_code, bundle_sha256=None,
                   artifacts=[], changed_files=[], **extra)
        out = {"run.json": json.dumps(run).encode(), "stdout.log": b"", "stderr.log": b""}
        return out, status
    return guest


# -- the cabin is built with the guest part's profile ----------------------------------------------------------------

def test_the_cabin_user_data_carries_the_tour_profile(rig):
    rig.run_tour(egress=["github.com"])
    extras = rig.render_extras[0]
    assert extras is not None and "github.com" in extras.files
    assert "inet cabin_tour" in file_block(extras.files, "/etc/motel-cabin/tour.nft.in")     # the template; two tokens are filled at boot
    assert extras.key_options == f',port-forwarding,permitlisten="127.0.0.1:{rig.limits.concierge_port}"'
    assert rig.extras[0] == {"hosts": ["github.com"], "dns_resolvers": list(rig.world.cfg.dns_resolvers),
                             "concierge_port": rig.limits.concierge_port, "own_login": False}


def test_a_guest_without_secrets_gets_no_reverse_forward(rig):
    rig.run_tour([{"id": "lint", "adapter": "command", "argv": ["make"], "secrets": []}])
    assert rig.extras[0]["concierge_port"] is None and rig.render_extras[0].key_options == ""
    assert ("preflight", None) in rig.log


def test_an_own_login_guest_gets_the_anthropic_host_and_no_forward(rig):
    from test_ownlogin import OWN, enable
    enable(rig)
    rig.run_tour(OWN)
    assert rig.extras[0]["own_login"] is True and rig.extras[0]["concierge_port"] is None
    assert "api.anthropic.com" in rig.render_extras[0].files and rig.render_extras[0].key_options == ""
    assert ("preflight", None) in rig.log


def fill(budget, count, reserve=()):
    """``count`` valid host names that use the egress byte budget to the last few bytes (``reserve``: hosts that count too)."""
    from motel.tours.guest import EGRESS_HOST_OVERHEAD, egress_cost
    each = (budget - egress_cost(reserve)) // count - EGRESS_HOST_OVERHEAD
    hosts = []
    for i in range(count):
        stem = f"h{i:02d}."
        name = stem + "a" * max(1, min(60, each - len(stem) - 4)) + ".com"
        while len(name) < each:
            name = "b" * min(60, each - len(name) - 1) + "." + name
        hosts.append(name[:253])
    assert len(set(hosts)) == count
    return hosts


DEFAULT_PACKAGES = ["git", "rsync", "ca-certificates", "curl"]      # cabins.guest_packages when not configured
MARGIN = 200                                  # bytes of room left under the guest part's target


@pytest.fixture(scope="module")
def production_keys(tmp_path_factory):
    """Keys made the way the controller makes them: ssh-keygen ed25519, comment = the cabin name."""
    import secrets
    import shutil

    from motel.cabins import userdata
    if not shutil.which("ssh-keygen") or not __import__("os").path.exists("/usr/bin/ssh-keygen"):
        pytest.skip("ssh-keygen is not installed")
    lease = f"a-{secrets.token_hex(5)}-{secrets.token_hex(6)}"            # attempt id, then the controller's suffix
    name = "motel-" + lease
    directory = str(tmp_path_factory.mktemp("keys"))
    host_private, host_public = userdata.ssh_keygen(directory, "host_ed25519", name)
    _, client_public = userdata.ssh_keygen(directory, "client", "client")
    _, controller_public = userdata.ssh_keygen(directory, "controller", name + "-controller")
    return {"lease": lease, "name": name, "host_private": host_private, "host_public": host_public,
            "client_public": client_public, "controller_public": controller_public}


def render_with(keys, hosts, **kw):
    """The real cloud-init, the real guest agent, the real tour extras, a real host key and the production defaults:
    the default guest packages, the default resolvers and the longest host addresses (full-length IPv6)."""
    from motel.cabins import userdata
    from motel.tours import guest
    resolvers = ["185.12.64.1", "185.12.64.2"]
    extras = guest.userdata_extras(hosts, resolvers, **kw)
    from cabin_fakes import read_lib
    return userdata.render_user_data(
        read_lib("cloud-init.yaml"), rec={"lease_id": keys["lease"], "name": keys["name"], "expires_at": "2026-11-08T14:00:00Z"},
        mode="ssh-allowlist", host_private=keys["host_private"], host_public=keys["host_public"],
        client_public=keys["client_public"], controller_public=keys["controller_public"],
        guest_agent=read_lib("guest_agent.py", "rb"), packages=list(DEFAULT_PACKAGES),
        sources=["199.199.199.199/32", "2a01:4ff8:ffff:ffff:ffff:ffff:ffff:ffff/128"], idle_seconds=1800, dns=resolvers,
        extras=extras)


@pytest.mark.parametrize("count,own_login", [(64, False), (16, False), (63, True)])
def test_the_user_data_with_the_egress_budget_full_stays_within_the_guest_parts_target(production_keys, count, own_login):
    """The worst cases with this tree's real guest agent, a real host key, the default packages and a full-length IPv6
    address: 64 hosts, 16 hosts (the longest names) and 63 plus api.anthropic.com for own-login, each using the whole
    byte budget. The target is not relaxed and at least MARGIN bytes stay free."""
    from motel.tours.guest import MAX_EGRESS_BYTES, OWN_LOGIN_HOSTS, USER_DATA_TARGET, egress_cost
    hosts = fill(MAX_EGRESS_BYTES, count, OWN_LOGIN_HOSTS if own_login else ())
    cost = egress_cost(sorted(set(hosts) | (set(OWN_LOGIN_HOSTS) if own_login else set())))
    assert MAX_EGRESS_BYTES - 2 * count < cost <= MAX_EGRESS_BYTES
    kw = {"own_login": True} if own_login else {"concierge_port": 17781}
    size = len(render_with(production_keys, hosts, **kw).encode())
    assert size <= USER_DATA_TARGET - MARGIN, (size, USER_DATA_TARGET)


def test_the_size_tests_use_the_packages_a_default_cabin_installs():
    from motel.cabins.config import parse_section
    assert list(parse_section({"enabled": True, "types": ["cpx32"]}).guest_packages) == DEFAULT_PACKAGES


def test_a_cabin_with_the_most_hosts_boots_through_the_whole_engine(rig):
    hosts = [f"host{i}.example.com" for i in range(64)]
    rig.limits = rig.limits.__class__(**{**rig.limits.__dict__, "egress_allow": ("*.example.com",)})
    rig.service.limits = rig.limits
    rig.env.limits = rig.limits
    doc = rig.run_tour(egress=hosts)
    assert doc["status"] == "done"
    assert rig.render_sizes and rig.render_sizes[0] < USER_DATA_LIMIT, rig.render_sizes


def test_own_login_leaves_room_for_the_anthropic_host(rig):
    from test_ownlogin import enable
    enable(rig)
    rig.limits = rig.limits.__class__(**{**rig.limits.__dict__, "egress_allow": ("*.example.com",)})
    rig.service.limits = rig.limits
    hosts = [f"host{i}.example.com" for i in range(64)]
    with pytest.raises(Exception, match="too many or too long.*api.anthropic.com counted"):
        rig.start([{"id": "me", "prompt": "x", "auth": "own-login", "secrets": [], "egress": hosts}])


def test_more_than_64_egress_hosts_are_refused_before_a_cabin_exists(rig):
    hosts = [f"host{i}.example.com" for i in range(65)]
    rig.limits = rig.limits.__class__(**{**rig.limits.__dict__, "egress_allow": ("*.example.com",)})
    rig.service.limits = rig.limits
    with pytest.raises(Exception, match="64|egress"):
        rig.start(egress=hosts)
    assert rig.servers_left() == []


def test_egress_names_over_the_byte_budget_are_refused_with_exit_3(rig):
    from motel.tours.errors import Refused
    from motel.tours.guest import MAX_EGRESS_BYTES
    rig.limits = rig.limits.__class__(**{**rig.limits.__dict__, "egress_allow": ("*.example.com",)})
    rig.service.limits = rig.limits
    hosts = [f"{'a' * 60}{i:02d}.example.com" for i in range(40)]      # 40 hosts, but far over the byte budget
    assert sum(len(h) for h in hosts) > MAX_EGRESS_BYTES
    with pytest.raises(Refused) as err:
        rig.start(egress=hosts)
    assert err.value.code == 3 if hasattr(err.value, "code") else True
    assert rig.servers_left() == []


def test_the_runner_and_its_adapters_are_copied_to_the_runner_directory(rig):
    rig.run_tour()
    assert [e[1] for e in rig.log if e[0] == "put"][:2] == ["adapters.py", "runner.py"]


# -- readiness: both directions --------------------------------------------------------------------------------------

def test_a_tour_cabin_whose_agent_reports_another_forward_never_runs(rig):
    original = rig.env.extras

    def wrong(**kw):
        extras = original(**kw)
        rig.world.guest.forward_port = 9999               # the guest allows a different port than the one requested
        return extras
    rig.env.extras = wrong
    doc = rig.run_tour()
    assert doc["guests"]["fix-auth"]["status"] == "failed" and "run" not in rig.log.kinds()
    assert rig.servers_left() == [] and "isolation check failed" in json.dumps(rig.store.attempts(doc["id"]))


def test_a_tour_cabin_whose_agent_reports_no_forward_never_runs(rig):
    original = rig.env.extras

    def none(**kw):
        extras = original(**kw)
        rig.world.guest.forward_port = None
        return extras
    rig.env.extras = none
    doc = rig.run_tour()
    assert doc["guests"]["fix-auth"]["status"] == "failed" and "run" not in rig.log.kinds()


def test_a_plain_cabin_that_reports_a_forward_fails_readiness(rig):
    rig.world.guest.forward_port = 7781
    with pytest.raises(Exception, match="not requested"):
        rig.world.checkin()
    rig.world.guest.forward_port = None
    assert rig.world.checkin()["state"] == "active"


# -- the runner's failure reasons ------------------------------------------------------------------------------------

@pytest.mark.parametrize("reason", ["input_missing", "runner_error", "git_timeout", "adapter_missing", "invalid_card",
                                    "base_missing", "overlay_failed", "own_login_missing_token", "git_missing"])
def test_a_runner_reported_failure_is_never_retried_once_the_engine_launched_the_runner(rig, reason):
    """invariant 6: only a failure the engine itself saw before launching the runner may be retried."""
    rig.transport.guest = failing_guest(reason)
    doc = rig.run_tour()
    attempts = rig.attempts(doc["id"])
    assert len(attempts) == 1 and attempts[0]["command_started_at"] is not None
    assert len(rig.transport.leases_run) == 1
    assert doc["guests"]["fix-auth"]["status"] == "failed"
    assert "run" in rig.log.kinds() and rig.servers_left() == []
    record = rig.result_record(doc["id"], "fix-auth")
    assert record["run"]["reason"] == reason and reason in record["error"]


def test_a_failure_after_the_command_started_is_never_retried_even_with_a_retryable_reason(rig):
    rig.transport.guest = failing_guest("runner_error", started=True, exit_code=1)
    doc = rig.run_tour()
    assert len(rig.attempts(doc["id"])) == 1


def test_a_missing_run_json_with_runner_exit_70_is_reported_as_such(rig):
    def guest(card, files):
        return {"stdout.log": b"", "stderr.log": b""}, 70
    rig.transport.guest = guest
    doc = rig.run_tour()
    record = rig.result_record(doc["id"], "fix-auth")
    assert "exit 70" in record["error"] and record["runner_reported"]["exit_code"] is None
    assert len(rig.attempts(doc["id"])) == 1


# -- refusals and budget ---------------------------------------------------------------------------------------------

def guest_with_refusals(refusals, exit_code=1):
    def guest(card, files):
        out, status = doubles.default_guest(card, files)
        run = json.loads(out["run.json"])
        run.update(exit_code=exit_code, concierge_refusals=refusals)
        out["run.json"] = json.dumps(run).encode()
        return out, status
    return guest


def test_refusals_are_surfaced_and_a_budget_refusal_that_the_ledger_confirms_means_over_budget(rig):
    refusals = [{"type": "motel_budget_exhausted", "status": 403, "count": 2}, {"type": "motel_concurrency", "status": 429, "count": 3}]
    rig.transport.guest = guest_with_refusals(refusals)
    original = rig.concierge.issue

    def issue(**kw):
        token = original(**kw)
        rig.concierge.spend(kw["attempt"], 0.0, refused=2)         # the concierge itself counted the refusals
        return token
    rig.concierge.issue = issue
    doc = rig.run_tour()
    assert doc["guests"]["fix-auth"]["status"] == "over_budget"
    record = rig.result_record(doc["id"], "fix-auth")
    assert record["concierge_refusals"] == {"reported_by_runner": refusals, "counted_by_concierge": 2}


def test_a_denied_feature_is_a_failure_with_a_reason_never_over_budget(rig):
    rig.transport.guest = guest_with_refusals([{"type": "motel_feature_denied", "status": 403, "count": 1}])
    original = rig.concierge.issue

    def issue(**kw):
        token = original(**kw)
        rig.concierge.spend(kw["attempt"], 0.0, refused=1)
        return token
    rig.concierge.issue = issue
    doc = rig.run_tour()
    assert doc["guests"]["fix-auth"]["status"] == "failed"
    assert "motel_feature_denied" in rig.result_record(doc["id"], "fix-auth")["error"]


def test_a_budget_refusal_that_only_the_guest_reports_does_not_change_the_status(rig):
    rig.transport.guest = guest_with_refusals([{"type": "motel_budget_exhausted", "status": 403, "count": 1}])
    doc = rig.run_tour()
    assert doc["guests"]["fix-auth"]["status"] == "failed"
    assert rig.result_record(doc["id"], "fix-auth")["concierge_refusals"]["counted_by_concierge"] == 0


def test_a_successful_run_with_a_refusal_stays_done(rig):
    rig.transport.guest = guest_with_refusals([{"type": "motel_concurrency", "status": 429, "count": 4}], exit_code=0)
    doc = rig.run_tour()
    assert doc["guests"]["fix-auth"]["status"] == "done"


def test_run_json_fields_from_the_guest_are_sanitised():
    doc = {"schema": art.RUN_SCHEMA, "attempt": "a-1", "termination": "failed", "exit_code": None, "reason": "Bad Reason!",
           "command_started": "yes", "concierge_refusals": [{"type": "motel_ok", "status": 99, "count": -1}, {"type": "evil"},
                                                            "x", {"type": "motel_" + "a" * 80}],
           "rejected": [{"path": "a\x1b[31m", "reason": "symlink"}, 5]}
    clean = art.validate_run_doc(doc, "a-1")
    assert clean["reason"] is None and clean["command_started"] is None
    assert clean["concierge_refusals"] == [{"type": "motel_ok", "status": None, "count": 0}]
    assert clean["rejected"] == [{"path": "a[31m", "reason": "symlink"}]


def guest_with_run_fields(**fields):
    def guest(card, files):
        out, status = doubles.default_guest(card, files)
        run = json.loads(out["run.json"])
        run.update(fields)
        out["run.json"] = json.dumps(run).encode()
        return out, status
    return guest


def test_new_run_json_fields_are_typed_and_kept():
    doc = {"schema": art.RUN_SCHEMA, "attempt": "a-1", "termination": "exited", "exit_code": 0, "stragglers_killed": 2,
           "stragglers_left": "many", "pruned_out": True,
           "rejected": [{"path": "result.bundle", "reason": "git_broken", "detail": "bad\x1b[31m object"},
                        {"path": "result.bundle", "reason": "scan_failed", "detail": 5}]}
    clean = art.validate_run_doc(doc, "a-1")
    assert (clean["stragglers_killed"], clean["stragglers_left"], clean["pruned_out"]) == (2, 0, 0)
    assert clean["rejected"] == [{"path": "result.bundle", "reason": "git_broken", "detail": "bad[31m object"},
                                 {"path": "result.bundle", "reason": "scan_failed"}]


def test_processes_the_runner_could_not_kill_are_a_violation_and_stay_in_the_record(rig):
    rig.transport.guest = guest_with_run_fields(termination="failed", reason="stragglers_left", stragglers_left=3,
                                                stragglers_killed=1, pruned_out=2)
    doc = rig.run_tour()
    assert doc["guests"]["fix-auth"]["status"] == "failed"
    record = rig.result_record(doc["id"], "fix-auth")
    assert any("could not be killed" in v for v in record["violations"])
    assert record["run"]["reason"] == "stragglers_left"
    assert (record["run"]["stragglers_left"], record["run"]["stragglers_killed"], record["run"]["pruned_out"]) == (3, 1, 2)


@pytest.mark.parametrize("reason,extra", [("stragglers_left", {"stragglers_left": 2}), ("out_not_clean", {"detail": "OSError"})])
def test_a_guest_whose_out_directory_is_not_clean_has_failed_and_nothing_is_collected_from_it(rig, reason, extra):
    def guest(card, files):
        out, status = doubles.default_guest(card, files)
        run = json.loads(out["run.json"])
        run.update(termination="failed", reason=reason, exit_code=0, bundle_sha256=None, result_json=False, artifacts=[], **extra)
        out["run.json"] = json.dumps(run).encode()
        return out, status                      # the guest still leaves result.bundle and RESULT.json lying in out/
    rig.transport.guest = guest
    doc = rig.run_tour()
    assert doc["guests"]["fix-auth"]["status"] == "failed"
    record = rig.result_record(doc["id"], "fix-auth")
    assert record["verified"]["bundle"] is None and record["guest_claims"]["result"] is None
    assert record["run"]["reason"] == reason and any("nothing was collected" in v for v in record["violations"])
    assert reason in record["error"]
    if reason == "out_not_clean":
        assert record["run"]["detail"] == "OSError"


def test_a_clean_up_reason_overrides_declared_output(rig):
    def guest(card, files):
        out, status = doubles.default_guest(card, files)
        run = json.loads(out["run.json"])
        run.update(reason="out_not_clean")                  # but it still declares a bundle: not trusted
        out["run.json"] = json.dumps(run).encode()
        return out, status
    rig.transport.guest = guest
    doc = rig.run_tour()
    record = rig.result_record(doc["id"], "fix-auth")
    assert doc["guests"]["fix-auth"]["status"] == "failed" and record["verified"]["bundle"] is None


# -- concierge listeners lost in a restart ---------------------------------------------------------------------------

def test_a_listener_lost_after_a_clean_exit_is_only_a_note(rig):
    rig.transport.on_run = lambda card: rig.concierge.lose_listener(card["attempt"], "port 18003 was taken")
    doc = rig.run_tour()
    assert doc["guests"]["fix-auth"]["status"] == "done"
    record = rig.result_record(doc["id"], "fix-auth")
    assert record["reason"] is None and any("concierge_listener_lost" in n and "clean exit" in n for n in record["notes"])


def test_a_listener_lost_in_a_concierge_restart_fails_the_attempt_and_is_not_retried(rig):
    rig.transport.guest = guest_with_run_fields(exit_code=1)       # the runner did not report a clean exit
    rig.transport.on_run = lambda card: rig.concierge.lose_listener(card["attempt"], "port 18003 was taken")
    doc = rig.run_tour()
    g = doc["guests"]["fix-auth"]
    assert g["status"] == "failed" and g.get("retries_used", 0) == 0 and len(g["attempts"]) == 1
    record = rig.result_record(doc["id"], "fix-auth")
    assert record["status"] == "failed" and record["reason"] == "concierge_listener_lost"
    assert "concierge_listener_lost" in record["error"] and "port 18003" in record["error"]
    view = rig.service.status(rig.alice, doc["id"])["tour"]
    assert view["lost_listeners"] == {g["attempts"][0]: "port 18003 was taken"}


def test_a_concierge_restart_that_did_not_preserve_the_socket_fails_the_attempt(rig):
    rig.transport.guest = guest_with_run_fields(exit_code=1)
    rig.transport.on_run = lambda card: rig.concierge.lose_listener(card["attempt"])      # the default reason: not_preserved
    doc = rig.run_tour()
    record = rig.result_record(doc["id"], "fix-auth")
    assert doc["guests"]["fix-auth"]["status"] == "failed" and record["reason"] == "concierge_listener_lost"
    assert "not_preserved" in record["error"]
    assert list(rig.service.status(rig.alice, doc["id"])["tour"]["lost_listeners"].values()) == ["not_preserved"]


def test_a_listener_that_is_gone_before_the_command_starts_is_retried_once(rig):
    from motel.concierge.errors import AdminRefused
    real = rig.concierge.endpoint
    calls = []

    def endpoint(*, attempt):
        calls.append(attempt)
        if len(calls) == 1:
            raise AdminRefused("no open listener", "gone")
        return real(attempt=attempt)
    rig.concierge.endpoint = endpoint
    doc = rig.run_tour()
    g = doc["guests"]["fix-auth"]
    assert g["status"] == "done" and g["retries_used"] == 1 and len(g["attempts"]) == 2


def test_status_lists_only_the_lost_listeners_of_its_own_attempts(rig):
    doc = rig.run_tour()
    rig.concierge.lost_listeners["t-someone-else-a1"] = "not mine"
    assert rig.service.status(rig.alice, doc["id"])["tour"]["lost_listeners"] == {}
    rig.concierge.unavailable = True
    assert rig.service.status(rig.alice, doc["id"])["tour"]["lost_listeners"] is None      # asked, but unreachable


# -- tailscale -------------------------------------------------------------------------------------------------------

def test_tailscale_cabins_are_refused_at_the_desk(rig):
    rig.service.cabin_cfg = type("Cfg", (), {"access": "tailscale", "types": ("cpx32",)})()
    with pytest.raises(Refused, match="ssh-allowlist"):
        rig.service.start(rig.alice, rig.wire())
    assert rig.store.ids() == [] and rig.servers_left() == []


# -- a per-attempt concierge listener ----------------------------------------------------------------------------------

def test_each_attempt_forwards_to_its_own_concierge_listener(rig):
    original, ports = rig.concierge.endpoint, []

    def endpoint(*, attempt):
        ports.append(original(attempt=attempt)["port"])
        return {"port": ports[-1]}
    rig.concierge.endpoint = endpoint
    doc = rig.run_tour()
    assert ports and rig.transport.forward_to and set(rig.transport.forward_to) == set(ports)
    assert doc["guests"]["fix-auth"]["status"] == "done"


def test_a_guest_with_no_concierge_forwards_nothing(rig):
    doc = rig.run_tour([{"id": "plain", "adapter": "command", "argv": ["true"], "secrets": [], "budget_usd": 0}])
    assert set(rig.transport.forward_to) == {None} and doc["guests"]["plain"]["status"] == "done"


def test_a_bad_listener_port_is_a_pre_start_failure(rig):
    rig.concierge.endpoint = lambda *, attempt: {"port": 0}
    doc = rig.run_tour()
    assert doc["guests"]["fix-auth"]["status"] == "failed"
    assert rig.transport.leases_run == []


def test_a_concierge_with_no_free_port_is_a_retryable_pre_start_failure(rig):
    from motel.concierge.errors import AdminRefused
    original = rig.concierge.issue
    calls = []

    def issue(**kw):
        calls.append(kw["attempt"])
        if len(calls) == 1:
            raise AdminRefused("no listener port is free", "busy")
        return original(**kw)
    rig.concierge.issue = issue
    doc = rig.run_tour()
    assert len(rig.attempts(doc["id"])) == 2 and doc["guests"]["fix-auth"]["status"] == "done"
