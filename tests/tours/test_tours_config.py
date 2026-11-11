"""[tours] in the core config (validated by the concierge's parser), [tours.own_login], and the doctor."""
import io
import json
import os
import tomllib

import pytest

from helpers import BASE_TOML, Fixture  # noqa: E402
from motel import desk, installer  # noqa: E402
from motel.config import Config  # noqa: E402
from motel.tours import config as tconfig  # noqa: E402

CABINS = '\n[cabins]\nenabled = true\ntypes = ["cpx32"]\nmax_eur_per_hour = 0.2\n'


def toml(tours: str) -> str:
    import tomllib
    return tomllib.loads(BASE_TOML + CABINS + tours)


def test_a_tours_section_is_accepted_and_read_through_the_concierge_parser():
    cfg = Config.from_dict(toml('[tours]\nenabled = true\nmax_ttl = "90m"\nmax_eur_per_tour = 2.5\negress_allow = ["GitHub.com"]\n'
                                'concierge_max_request_mb = 4\n'
                                '[tours.bindings.anthropic]\nupstream = "https://api.example.com"\nauth = "x-api-key"\n'
                                'key_file = "/etc/motel/llm-keys/anthropic.key"\npaths = ["/v1/messages"]\nmax_concurrent = 4\n'))
    limits = cfg.tours
    assert limits.enabled and limits.max_ttl.total_seconds() == 5400 and str(limits.max_eur_per_tour) == "2.5"
    assert limits.egress_allow == ("github.com",)                    # host names are matched in lower case
    assert limits.bindings == ("anthropic",) and limits.own_login_enabled is False


def test_without_a_tours_table_there_are_no_limits():
    assert Config.from_dict(toml("")).tours is None


def test_the_concierge_only_keys_are_not_rejected():
    cfg = Config.from_dict(toml('[tours]\nenabled = true\nconcierge_upstream_timeout_s = 60\n'))
    assert cfg.tours.enabled is True


def test_a_bad_tours_section_is_reported_but_does_not_break_the_config():
    cfg = Config.from_dict(toml('[tours]\nenabled = true\nmax_guests_per_tour = 0\n'))
    assert cfg.tours is None and "max_guests_per_tour" in cfg.tours_problem          # rooms and cleanup keep working
    cfg = Config.from_dict(toml('[tours]\nenabled = true\n[tours.bindings.x]\nupstream = "http://example.com"\nauth = "bearer"\n'
                                'key_file = "/k"\npaths = ["/v1/x"]\n'))
    assert cfg.tours is None and "plain http" in cfg.tours_problem
    assert Config.from_dict(toml('[tours]\nenabled = true\n')).tours_problem is None


def test_own_login_defaults_and_overrides():
    cfg = Config.from_dict(toml('[tours]\nenabled = true\n[tours.own_login]\nenabled = true\nmax_concurrent = 1\nmax_ttl = "30m"\n'
                                'claude_token_file = "/etc/motel/credentials/own-login/t"\n'))
    limits = cfg.tours
    assert limits.own_login_enabled and limits.own_login_max_concurrent == 1 and limits.own_login_max_ttl.total_seconds() == 1800
    assert limits.own_login_token_file == "/etc/motel/credentials/own-login/t"
    default = Config.from_dict(toml("[tours]\nenabled = true\n")).tours
    assert (default.own_login_enabled, default.own_login_max_concurrent, default.own_login_max_ttl.total_seconds()) == (False, 2, 7200)
    assert default.own_login_token_file == "/etc/motel/credentials/own-login/claude.token"


@pytest.mark.parametrize("body", ['enabled = "yes"', "bogus = 1", "max_concurrent = 0", "max_concurrent = true", 'max_ttl = 5',
                                  'max_ttl = "soon"', 'claude_token_file = "relative"', 'claude_token_file = 3'])
def test_a_bad_own_login_table_is_refused_for_starting_and_reported_by_the_config(body):
    table = tomllib.loads(f"enabled = true\n[own_login]\n{body}\n")
    with pytest.raises(Exception, match="own_login"):
        tconfig.Limits.from_table(table)
    cfg = Config.from_dict(toml(f"[tours]\nenabled = true\n[tours.own_login]\n{body}\n"))
    assert cfg.tours is None and "own_login" in cfg.tours_problem


def test_load_limits_needs_an_enabled_section(tmp_path):
    path = tmp_path / "motel.toml"
    path.write_text(BASE_TOML)
    with pytest.raises(Exception, match="tours are off"):
        tconfig.load_limits(str(path))
    path.write_text(BASE_TOML + CABINS + "[tours]\nenabled = false\n")
    with pytest.raises(Exception, match="disabled"):
        tconfig.load_limits(str(path))
    assert tconfig.load_limits(str(path), require_enabled=False).enabled is False
    with pytest.raises(Exception, match="does not exist"):
        tconfig.load_limits(str(tmp_path / "missing.toml"))


def test_limits_from_a_plain_dict_use_the_contract_keys():
    limits = tconfig.Limits.from_section({"enabled": True, "max_ttl": "1h", "bindings": {"a": {}}, "own_login": {"enabled": True}})
    assert limits.max_ttl.total_seconds() == 3600 and limits.bindings == ("a",) and limits.own_login_enabled


def doctor(fx):
    code, out, _ = fx.desk("doctor", "--json")
    return {c["check"]: c for c in json.loads(out)["checks"]}


def test_doctor_warns_only_when_own_login_is_enabled():
    fx = Fixture(BASE_TOML + CABINS + "[tours]\nenabled = true\n[tours.own_login]\nenabled = true\n")
    try:
        assert doctor(fx)["own-login is enabled"]["level"] == "warn"
    finally:
        fx.close()
    fx = Fixture(BASE_TOML + CABINS + "[tours]\nenabled = true\n")
    try:
        assert "own-login is enabled" not in doctor(fx)
    finally:
        fx.close()


def test_doctor_expects_the_tour_sudoers_rule_when_tours_are_on():
    fx = Fixture(BASE_TOML + CABINS + "[tours]\nenabled = true\n")
    try:
        path = fx.paths.real("/etc/sudoers.d/motel")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as handle:
            handle.write(installer.render_sudoers(["alice", "@agents"], cabins=True))
        assert doctor(fx)["sudoers rule matches allow_users"]["level"] == "fail"
        with open(path, "w") as handle:
            handle.write(installer.render_sudoers(["alice", "@agents"], cabins=True, tours=True))
        assert doctor(fx)["sudoers rule matches allow_users"]["level"] == "ok"
    finally:
        fx.close()


def test_doctor_lists_the_listeners_the_concierge_lost(monkeypatch):
    fx = Fixture(BASE_TOML + CABINS + "[tours]\nenabled = true\n")
    try:
        def checks(health):
            out, err = io.StringIO(), io.StringIO()
            desk.run(["doctor", "--json"], paths=fx.paths, caller=fx.caller,
                     make_provider=lambda cfg, paths: fx.provider, sysops=fx.sysops, clock=fx.clock,
                     out=out, err=err, concierge_health=health)
            return {c["check"]: c for c in json.loads(out.getvalue())["checks"]}
        assert checks(lambda: {"lost_listeners": {}})["concierge listeners"]["level"] == "ok"
        lost = checks(lambda: {"lost_listeners": {"a-77": "its port could not be bound again"}})["concierge listeners"]
        assert lost["level"] == "warn" and "a-77" in lost["detail"] and "concierge_listener_lost" in lost["detail"]

        def down():
            raise OSError("no socket")
        assert checks(down)["concierge listeners"]["level"] == "warn"
        assert "concierge listeners" not in doctor(fx)         # no hook and not a real system: nothing is asked
    finally:
        fx.close()


def test_doctor_reports_a_bad_tours_table_while_the_rest_of_motel_keeps_working():
    fx = Fixture(BASE_TOML + CABINS + '[tours]\nenabled = true\nmax_guests_per_tour = 0\n')
    try:
        checks = doctor(fx)
        assert checks["[tours] is valid"]["level"] == "fail" and "max_guests_per_tour" in checks["[tours] is valid"]["detail"]
        code, out, _ = fx.desk("ls", "--json")                                  # rooms still answer
        assert code == 0
    finally:
        fx.close()
