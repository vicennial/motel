import pytest
from cabin_fakes import CTRL4, CTRL6, TAG, section

from motel.cabins import config as cfgmod
from motel.cabins.errors import ConfigError, Prerequisite


def parse(data, users=("alice",)):
    return cfgmod.parse_section(data, allow_users=users)


def test_defaults_are_safe():
    cfg = parse({"enabled": True, "types": ["cpx32"]})
    assert cfg.access == "ssh-allowlist"
    assert cfg.idle_timeout.total_seconds() == 1800
    assert cfg.max_concurrent == 4
    assert cfg.location == "auto"
    assert cfg.tailscale is None
    assert cfg.max_lifetime.days == 7


def test_unknown_key_and_bad_types_are_listed():
    with pytest.raises(ConfigError) as exc:
        parse({"enabled": "yes", "types": ["cpx32"], "surprise": 1})
    assert "unknown key cabins.surprise" in exc.value.message
    assert "cabins.enabled must be bool" in exc.value.message


def test_idle_cannot_be_shortened_below_30_minutes():
    with pytest.raises(ConfigError, match="idle_timeout"):
        parse({"enabled": True, "types": ["cpx32"], "idle_timeout": "10m"})
    assert parse({"enabled": True, "types": ["cpx32"], "idle_timeout": "45m"}).idle_timeout.total_seconds() == 2700


def test_enabled_needs_types():
    with pytest.raises(ConfigError, match="types"):
        parse({"enabled": True})


def test_limits_are_enforced():
    for bad in ({"max_concurrent": 0}, {"max_concurrent": 1000}, {"max_eur_per_hour": 0}, {"default_ttl": "48h"},
                {"max_lifetime": "30d"}, {"max_disk_gb": 5}, {"dns_resolvers": ["::1"]}, {"dns_resolvers": []},
                {"guest_packages": ["ok", "bad name; rm"]}, {"token_file": "relative/path"},
                {"token_file": "/etc/motel/../shadow"}, {"location": "FSN1"}):
        with pytest.raises(ConfigError):
            parse(dict({"enabled": True, "types": ["cpx32"]}, **bad))


def test_tailscale_mode_requires_its_table_and_exact_controller_addresses():
    with pytest.raises(ConfigError, match="needs a"):
        parse({"enabled": True, "types": ["cpx32"], "access": "tailscale"})
    data = section("tailscale")
    cfg = parse(data)
    assert cfg.tailscale.tag == TAG and cfg.tailscale.controller_addresses == (CTRL4, CTRL6)
    for bad in (["100.64.0.0/10"], ["8.8.8.8"], ["100.64.0.1/24"], []):
        broken = section("tailscale")
        broken["tailscale"]["controller_addresses"] = bad
        with pytest.raises(ConfigError, match="controller_addresses"):
            parse(broken)


def test_access_mode_must_be_known():
    with pytest.raises(ConfigError, match="access"):
        parse({"enabled": True, "types": ["cpx32"], "access": "open"})


def test_allow_users_come_from_the_host_table():
    assert cfgmod.parse_allow_users({"allow_users": ["alice", "@staff", "bad name"]}) == ("alice", "@staff")
    assert cfgmod.parse_allow_users({}) == ()


def test_load_config_reads_toml_and_reports_missing_section(tmp_path):
    path = tmp_path / "motel.toml"
    path.write_text('[host]\nallow_users = ["alice"]\n')
    with pytest.raises(Prerequisite, match="no \\[cabins\\] section"):
        cfgmod.load_config(str(path))
    path.write_text('[host]\nallow_users = ["alice"]\n[cabins]\nenabled = true\ntypes = ["cpx32"]\n')
    cfg = cfgmod.load_config(str(path))
    assert cfg.allow_users == ("alice",) and cfg.enabled
    path.write_text('[cabins]\nenabled = false\ntypes = ["cpx32"]\n')
    with pytest.raises(Prerequisite, match="disabled"):
        cfgmod.load_config(str(path))
    assert cfgmod.load_config(str(path), require_enabled=False).enabled is False


def test_paths_prefix():
    paths = cfgmod.CabinPaths("/tmp/x")
    assert paths.strict is False
    assert paths.config_file == "/tmp/x/etc/motel/motel.toml"
    assert cfgmod.CabinPaths().strict is True


def test_release_margin_defaults_to_six_minutes_and_is_bounded():
    assert parse({"enabled": True, "types": ["cpx32"]}).release_margin.total_seconds() == 360
    assert parse({"enabled": True, "types": ["cpx32"], "release_margin": "10m"}).release_margin.total_seconds() == 600
    assert parse({"enabled": True, "types": ["cpx32"], "release_margin": "4m"}).release_margin.total_seconds() == 240
    for bad in ("1m", "3m", "31m", "2h", "soon"):
        with pytest.raises(ConfigError, match="release_margin"):
            parse({"enabled": True, "types": ["cpx32"], "release_margin": bad})
