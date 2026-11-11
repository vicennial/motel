"""The entry point, namespace validation and registry instantiation. Needs Omnigent; skipped without it."""

import importlib
import importlib.metadata
import tomllib
from pathlib import Path

import pytest

pytest.importorskip("omnigent.onboarding.sandboxes.registry")

from omnigent.onboarding.sandboxes import registry  # noqa: E402
from omnigent.onboarding.sandboxes.base import ExecModelHostLauncher  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """Make the entry point from pyproject.toml discoverable, as if motel were pip-installed."""
    points = PYPROJECT["project"]["entry-points"]["omnigent.sandbox_providers"]
    dist = tmp_path / "motel-0.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text("Metadata-Version: 2.1\nName: motel\nVersion: 0\n")
    (dist / "entry_points.txt").write_text("[omnigent.sandbox_providers]\n" + "".join(f"{k} = {v}\n" for k, v in points.items()))
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    registry.reset_plugin_state_for_tests()
    yield
    registry.reset_plugin_state_for_tests()


def test_pyproject_declares_the_entry_point_under_the_community_namespace():
    points = PYPROJECT["project"]["entry-points"]["omnigent.sandbox_providers"]
    assert points == {"motel": "omnigent.community.sandbox.motel.plugin:get_contribution"}


def test_entry_point_loads_and_passes_namespace_validation(installed):
    state = registry.plugin_state()
    assert state.load_errors == {}
    meta = state.get("motel")
    assert meta is not None and meta.name == "motel"
    assert meta.launcher_class.startswith(registry.COMMUNITY_MODULE_PREFIX)
    assert meta.config_model.__module__.startswith(registry.COMMUNITY_MODULE_PREFIX)
    assert "motel" in registry.available_providers()


def test_registry_instantiates_the_launcher_with_and_without_config(installed):
    plain = registry.instantiate("motel")
    assert isinstance(plain, ExecModelHostLauncher) and plain.provider == "motel"
    assert plain.config.ttl == "2h"
    configured = registry.instantiate("motel", config={"ttl": "6h", "server_type": "cpx52", "env": ["ANTHROPIC_API_KEY"]})
    assert (configured.config.ttl, configured.config.server_type, configured.config.env) == ("6h", "cpx52", ["ANTHROPIC_API_KEY"])


def test_get_launcher_is_what_the_cli_calls(installed):
    from omnigent.onboarding.sandboxes import get_launcher
    assert get_launcher("motel", server_url="http://127.0.0.1:6767").provider == "motel"


@pytest.mark.parametrize("config", [
    {"env": ["PATH"]}, {"env": ["LD_PRELOAD"]}, {"env": ["not valid"]}, {"ttl": "forever"}, {"nonsense": 1},
    {"tunnel": "carrier-pigeon"}, {"omnigent_version": "latest; rm -rf /"}, {"harness_packages": ["x y"]}, {"disk_gb": 0},
])
def test_bad_config_stops_at_the_registry(installed, config):
    with pytest.raises(registry.SandboxRegistryError, match="invalid 'sandbox.motel' config"):
        registry.instantiate("motel", config=config)


def test_capabilities_match_what_the_launcher_implements(installed):
    caps = registry.instantiate("motel").capabilities
    assert (caps.cli_bootstrap, caps.managed_launch, caps.programmatic_terminate, caps.file_copy) == (True,) * 4
    assert (caps.streaming_exec, caps.foreground_exec) == (True, True)
    assert caps.local_port_forward is False
    assert caps.resume_stopped is True  # the wake path: resume() re-opens the tunnel after a server restart


def test_a_launcher_outside_the_namespace_would_be_rejected():
    """Guards the guard: validation is what keeps our module path honest."""
    from omnigent.onboarding.sandboxes.registry import SandboxProviderContribution, SandboxProviderMetadata
    bad = SandboxProviderContribution("x", {"motel": SandboxProviderMetadata("motel", "motel.sdk:Motel")})
    assert "expected omnigent.community.sandbox." in registry._validate_community_contribution(bad, entry_point_name="x", existing={})


def test_the_wheel_does_not_ship_omnigents_own_namespace_files():
    """omnigent/__init__.py, omnigent/community/__init__.py and .../sandbox/__init__.py belong to Omnigent. A copy in the
    motel wheel would overwrite them in a shared site-packages (and delete them on uninstall)."""
    base = ROOT / "src" / "omnigent"
    assert not (base / "__init__.py").exists()
    assert not (base / "community" / "__init__.py").exists()
    assert not (base / "community" / "sandbox" / "__init__.py").exists()
    assert (base / "community" / "sandbox" / "motel" / "plugin.py").exists()
    assert "src/omnigent" in PYPROJECT["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]


def test_server_managed_config_accepts_a_motel_block(installed):
    managed = pytest.importorskip("omnigent.server.managed_hosts")
    deployment = managed.parse_sandbox_config({"provider": "motel", "server_url": "http://127.0.0.1:6767",
                                                "motel": {"ttl": "4h", "env": ["ANTHROPIC_API_KEY"]}})
    (config,) = deployment.configs
    from omnigent.community.sandbox.motel.config import token_ttl_s
    assert (config.provider, config.managed_launch_supported, config.token_ttl_s) == ("motel", True, token_ttl_s())
    launcher = config.launcher_factory()
    assert launcher.config.ttl == "4h" and launcher.config.env == ["ANTHROPIC_API_KEY"]
    with pytest.raises(ValueError, match="sandbox.motel"):
        managed.parse_sandbox_config({"provider": "motel", "server_url": "http://127.0.0.1:6767", "motel": {"typo": 1}})
