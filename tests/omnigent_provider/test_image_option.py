"""``sandbox.motel.image``: start cabins from a baked image, and say at once when ``bootstrap: none`` has no omnigent."""

import pytest

pytest.importorskip("omnigent.onboarding.sandboxes.base")  # the launcher needs Omnigent's base classes (and pydantic)

from pydantic import ValidationError  # noqa: E402

from omnigent.community.sandbox.motel import transport as tp  # noqa: E402
from omnigent.community.sandbox.motel.config import MotelSandboxConfig  # noqa: E402
from omnigent.community.sandbox.motel.launcher import MotelSandboxError  # noqa: E402
from omnigent_provider.fakes import CABIN_ID  # noqa: E402
from omnigent_provider.helpers import make, seed  # noqa: E402


# -- the config key ----------------------------------------------------------------------------------------------
def test_image_defaults_to_none_and_does_not_choose_the_bootstrap():
    assert MotelSandboxConfig().image is None
    both = MotelSandboxConfig(image="omnigent")
    assert both.image == "omnigent" and both.bootstrap == "pip"          # nothing is guessed from the image
    assert MotelSandboxConfig(image="omnigent", bootstrap="none").bootstrap == "none"


@pytest.mark.parametrize("good", ["omnigent", "a", "omni-2", "x" * 40])
def test_a_baked_image_name_is_accepted(good):
    assert MotelSandboxConfig(image=good).image == good


@pytest.mark.parametrize("bad", ["Omnigent", "has space", "a;b", "../x", "-x", "x-", "ubuntu-24.04", "x" * 41, "12345", ""])
def test_anything_else_is_refused_when_the_config_loads(bad):
    with pytest.raises(ValidationError, match="image"):
        MotelSandboxConfig(image=bad)


# -- the checkin ---------------------------------------------------------------------------------------------------
def test_provision_passes_the_image_to_checkin(rig, tmp_path, capsys):
    seed(rig)
    assert make(rig, tmp_path, image="omnigent", bootstrap="none").provision("managed-a1b2c3d4") == CABIN_ID
    checkin = rig.argv()[0]
    assert checkin[:2] == ["cabin", "checkin"] and checkin[checkin.index("--image") + 1] == "omnigent"
    assert "image omnigent" in capsys.readouterr().out


def test_without_an_image_checkin_has_no_image_flag(rig, tmp_path):
    seed(rig)
    make(rig, tmp_path).provision("managed-a1b2c3d4")
    assert "--image" not in rig.argv()[0]


def test_a_desk_that_refuses_the_image_is_a_clean_error_and_no_cabin_is_left(rig, tmp_path):
    from omnigent_provider.fakes import fail
    rig.rule("cabin", "checkin", **fail(2, "the installed front desk is older than this motel and does not know --image",
                                        "ask the host admin to run: sudo motel install --upgrade"))
    with pytest.raises(MotelSandboxError, match="does not know --image"):
        make(rig, tmp_path, image="omnigent", bootstrap="none").provision("x1")
    assert [a[:2] for a in rig.argv()] == [["cabin", "checkin"]]


# -- bootstrap: none -------------------------------------------------------------------------------------------------
def test_bootstrap_none_with_omnigent_on_the_cabin_provisions_and_checks_nothing_else(rig, tmp_path):
    seed(rig)
    make(rig, tmp_path, image="omnigent", bootstrap="none").provision("managed-a1b2c3d4")
    wait, probe = rig.scripts()
    assert wait == tp.wrap("true\n") and "command -v omnigent" in probe and "exit 3" in probe
    assert [a[:2] for a in rig.argv()] == [["cabin", "checkin"], ["cabin", "ssh"], ["cabin", "ssh"]]


def test_bootstrap_none_without_omnigent_fails_early_names_the_image_and_checks_the_cabin_out(rig, tmp_path):
    seed(rig)
    rig.script_rule("command -v omnigent", exit=3)
    with pytest.raises(MotelSandboxError) as caught:
        make(rig, tmp_path, image="omnigent", bootstrap="none").provision("managed-a1b2c3d4")
    message = caught.value.message
    assert "omnigent is not on the PATH of cabin" in message and "image 'omnigent' was baked without omnigent" in message
    assert "motel omnigent bake-script" in message and "bootstrap: pip" in message
    assert rig.argv()[-1][:2] == ["cabin", "checkout"]
    assert not [s for s in rig.scripts() if "setsid nohup" in s or "pip install" in s]


def test_bootstrap_none_without_omnigent_and_without_an_image_points_at_both_fixes(rig, tmp_path):
    seed(rig)
    rig.script_rule("command -v omnigent", exit=3)
    with pytest.raises(MotelSandboxError, match=r"sandbox\.motel\.image.*bootstrap: pip"):
        make(rig, tmp_path, bootstrap="none").provision("managed-a1b2c3d4")
    assert rig.argv()[-1][:2] == ["cabin", "checkout"]


def test_a_probe_that_cannot_run_is_not_reported_as_a_missing_omnigent(rig, tmp_path):
    seed(rig)
    rig.script_rule("command -v omnigent", exit=255, stderr="ssh: connection reset")
    with pytest.raises(MotelSandboxError, match=r"could not check whether omnigent is on cabin .*exit 255"):
        make(rig, tmp_path, bootstrap="none").provision("managed-a1b2c3d4")
    assert rig.argv()[-1][:2] == ["cabin", "checkout"]


def test_the_pip_bootstrap_adds_no_probe_at_provision(rig, tmp_path):
    seed(rig)
    make(rig, tmp_path, image="omnigent").provision("managed-a1b2c3d4")
    assert rig.scripts() == [tp.wrap("true\n")]
