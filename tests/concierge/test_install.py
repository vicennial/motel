"""The systemd units and the installer layout."""
import re

import pytest

from motel.concierge import install


def directives(text):
    return [line for line in text.splitlines() if line and not line.startswith(("#", "["))]


def test_service_unit_is_hardened():
    unit = install.unit_text(7781)
    lines = directives(unit)
    for wanted in ("User=motel-concierge", "Group=motel-concierge", "NoNewPrivileges=yes", "ProtectSystem=strict", "ProtectHome=yes",
                   "PrivateTmp=yes", "CapabilityBoundingSet=", "AmbientCapabilities=", "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
                   "ReadWritePaths=/var/lib/motel-concierge", "PrivateDevices=yes", "ProtectKernelTunables=yes", "MemoryDenyWriteExecute=yes",
                   "RestrictNamespaces=yes", "LockPersonality=yes", "UMask=0077", "SystemCallFilter=@system-service",
                   "ExecStart=/usr/local/sbin/motel-concierge", "Requires=motel-concierge.socket"):
        assert wanted in lines, wanted
    assert "CapabilityBoundingSet=" in lines and not any(line.startswith("CapabilityBoundingSet=CAP") for line in lines)
    assert not any(line.startswith(("RuntimeDirectory=", "User=root")) for line in lines)  # see the install module docstring
    assert "PYTHONDONTWRITEBYTECODE" not in unit  # a no-op: the root-owned lib is not writable by this user anyway
    assert "AF_NETLINK" not in unit and "AF_PACKET" not in unit
    assert re.search(r"^\[Install\]\nWantedBy=multi-user.target", unit, re.M)


@pytest.mark.parametrize("port", [0, 80, 70000, "7781", True])
def test_unit_text_rejects_bad_ports(port):
    with pytest.raises(ValueError):
        install.unit_text(port)


def test_the_unit_has_no_port_because_the_listeners_are_per_attempt():
    """The port argument is only kept so older callers still work; the range comes from motel.toml."""
    assert install.unit_text(17781) == install.unit_text() == install.unit_text(None)
    assert "--port" not in install.unit_text(17781)


def test_socket_unit_is_root_only():
    lines = directives(install.socket_unit_text())
    for wanted in ("ListenStream=/run/motel/concierge.sock", "SocketUser=root", "SocketGroup=root", "SocketMode=0600", "DirectoryMode=0700",
                   "Accept=no"):
        assert wanted in lines, wanted


def test_layout_describes_user_dirs_files_and_modes():
    lay = install.layout()
    assert lay.user.name == "motel-concierge" and lay.user.shell == "/usr/sbin/nologin"
    assert lay.user.useradd_args()[:3] == ["useradd", "--system", "--user-group"]
    dirs = {d.path: d for d in lay.dirs}
    state = dirs["/var/lib/motel-concierge"]
    assert (state.owner, state.group, state.mode) == ("motel-concierge", "motel-concierge", 0o700)
    keys = dirs["/etc/motel/llm-keys"]
    assert (keys.owner, keys.group, keys.mode) == ("root", "motel-concierge", 0o750)
    assert not any(d.path.startswith("/etc/motel/credentials") for d in lay.dirs)  # root-only; the concierge never needs it
    assert not any(d.mode & 0o007 for d in lay.dirs)  # nothing is world-accessible
    files = {f.path: f for f in lay.files}
    assert files["/etc/systemd/system/motel-concierge.service"].mode == 0o644
    assert files["/etc/systemd/system/motel-concierge.socket"].kind == "socket"
    assert files["/usr/local/sbin/motel-concierge"].mode == 0o755 and files["/usr/local/sbin/motel-concierge"].owner == "root"
    assert lay.socket_path == "/run/motel/concierge.sock" and lay.enable == ("motel-concierge.socket", "motel-concierge.service")


def test_entry_script_compiles_and_uses_the_system_interpreter():
    text = install.entry_text()
    assert text.startswith("#!/usr/bin/python3 -I\n")
    compile(text, "motel-concierge", "exec")
    assert "from motel.concierge.service import main" in text


# -- review L5: the units are rewritten on upgrade, not only on first install ----------------------------------------------------------

def test_a_first_install_creates_both_units():
    changes = install.unit_changes(lambda path: None, systemd_version=255)
    assert [(c.unit, c.action) for c in changes] == [("motel-concierge.service", "create"), ("motel-concierge.socket", "create")]
    assert changes[0].text == install.unit_text(systemd_version=255) and changes[0].mode == 0o644
    assert changes[0].path == "/etc/systemd/system/motel-concierge.service"


def test_an_unchanged_install_writes_nothing_and_needs_no_reload():
    installed = {"/etc/systemd/system/motel-concierge.service": install.unit_text(systemd_version=255),
                 "/etc/systemd/system/motel-concierge.socket": install.socket_unit_text()}
    assert install.unit_changes(installed.get, systemd_version=255) == ()
    assert install.after_unit_changes(()) == ()


def test_an_upgrade_rewrites_a_unit_that_an_earlier_version_wrote():
    """The units earlier versions wrote must all be rewritten, then reloaded and the service restarted: one from before the fd
    store, one that had the misspelled ``FileDescriptorStore=`` key (systemd ignored it), one without the marker or the Preserve
    line, one with a store sized too small."""
    current = install.unit_text(systemd_version=255)
    no_store = "\n".join(line for line in current.splitlines() if not line.startswith(("FileDescriptorStore", "NotifyAccess", "Environment"))) + "\n"
    misspelled = current.replace("FileDescriptorStoreMax=512", "FileDescriptorStore=512")
    no_marker = "\n".join(line for line in current.splitlines() if not line.startswith("Environment=MOTEL_FDSTORE")) + "\n"
    no_preserve = install.unit_text(systemd_version=250)
    small = current.replace("FileDescriptorStoreMax=512", "FileDescriptorStoreMax=64").replace("MOTEL_FDSTORE_MAX=512", "MOTEL_FDSTORE_MAX=64")
    for old in (no_store, misspelled, no_marker, no_preserve, small):
        installed = {"/etc/systemd/system/motel-concierge.service": old, "/etc/systemd/system/motel-concierge.socket": install.socket_unit_text()}
        changes = install.unit_changes(installed.get, systemd_version=255)
        assert [(c.unit, c.action) for c in changes] == [("motel-concierge.service", "update")] and changes[0].text == current
        assert install.after_unit_changes(changes) == (("systemctl", "daemon-reload"), ("systemctl", "restart", "motel-concierge.service"))


def test_a_changed_socket_unit_is_rewritten_and_reloaded_without_restarting_the_service():
    installed = {"/etc/systemd/system/motel-concierge.service": install.unit_text(systemd_version=255),
                 "/etc/systemd/system/motel-concierge.socket": "[Socket]\nListenStream=/somewhere/else\n"}
    changes = install.unit_changes(installed.get, systemd_version=255)
    assert [(c.unit, c.action) for c in changes] == [("motel-concierge.socket", "update")]
    assert install.after_unit_changes(changes) == (("systemctl", "daemon-reload"),)


def test_first_install_reloads_but_does_not_restart():
    assert install.after_unit_changes(install.unit_changes(lambda path: None)) == (("systemctl", "daemon-reload"),)


def test_the_verify_step_flags_a_unit_without_a_large_enough_store_or_marker():
    for version in (None, 252, 259):
        assert install.unit_problems(install.unit_text(systemd_version=version)) == []
    assert len(install.unit_problems("[Service]\nExecStart=/x\n")) == 2
    assert install.unit_problems("[Service]\nFileDescriptorStore=512\nEnvironment=MOTEL_FDSTORE_MAX=512\n") == [
        "motel-concierge.service has no FileDescriptorStoreMax=; rewrite it (sudo motel install)"]  # the misspelled key does not count
    small = install.unit_text().replace("FileDescriptorStoreMax=512", "FileDescriptorStoreMax=10")
    assert install.unit_problems(small) == ["FileDescriptorStoreMax=10 is below 512; rewrite it (sudo motel install)"]
    assert install.unit_problems(install.unit_text().replace("MOTEL_FDSTORE_MAX=512", "MOTEL_FDSTORE_MAX=10")) == [
        "Environment=MOTEL_FDSTORE_MAX=10 is below 512; rewrite it (sudo motel install)"]


# -- H1: every directive the units write must be one systemd knows ------------------------------------------------------------------

KNOWN_DIRECTIVES = {
    "Unit": {"Description", "Requires", "After", "Wants", "ConditionPathExists"},
    "Service": {"Type", "User", "Group", "ExecStart", "Restart", "RestartSec", "FileDescriptorStoreMax", "FileDescriptorStorePreserve",
                "NotifyAccess", "Environment", "TimeoutStopSec", "StateDirectory", "StateDirectoryMode", "ReadWritePaths", "UMask",
                "NoNewPrivileges", "CapabilityBoundingSet", "AmbientCapabilities", "ProtectSystem", "ProtectHome", "PrivateTmp",
                "PrivateDevices", "ProtectKernelTunables", "ProtectKernelModules", "ProtectKernelLogs", "ProtectControlGroups",
                "ProtectClock", "ProtectHostname", "ProtectProc", "ProcSubset", "RestrictAddressFamilies", "RestrictNamespaces",
                "RestrictRealtime", "RestrictSUIDSGID", "LockPersonality", "MemoryDenyWriteExecute", "RemoveIPC",
                "SystemCallArchitectures", "SystemCallFilter"},
    "Install": {"WantedBy"},
    "Socket": {"ListenStream", "SocketUser", "SocketGroup", "SocketMode", "DirectoryMode", "Accept", "RemoveOnStop"},
}  # a fixed list of real systemd directive names, so a CI host without systemd still catches a made-up one


def unit_variants():
    return [install.unit_text(systemd_version=v) for v in (None, 249, 252, 253, 254, 255, 259)] + [install.socket_unit_text()]


def test_every_directive_in_the_units_is_a_real_systemd_directive():
    for text in unit_variants():
        section = None
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("["):
                section = line.strip("[]")
                assert section in KNOWN_DIRECTIVES, f"unknown section {line}"
                continue
            key = line.partition("=")[0]
            assert "=" in line and key in KNOWN_DIRECTIVES[section], f"[{section}] {key}= is not a systemd directive"


def test_the_allowlist_would_have_caught_the_misspelled_key():
    assert "FileDescriptorStore" not in KNOWN_DIRECTIVES["Service"] and "FileDescriptorStoreMax" in KNOWN_DIRECTIVES["Service"]


def test_systemd_analyze_accepts_every_unit_variant(tmp_path):
    """Verifies only (``systemd-analyze verify`` loads the files; it starts nothing). Skipped where the tool is absent."""
    import shutil
    import subprocess
    analyze = shutil.which("systemd-analyze")
    if not analyze:
        pytest.skip("systemd-analyze is not installed")
    for number, service in enumerate(unit_variants()[:-1]):
        directory = tmp_path / f"v{number}"
        directory.mkdir()
        (directory / "motel-concierge.service").write_text(service)
        (directory / "motel-concierge.socket").write_text(install.socket_unit_text())
        out = subprocess.run([analyze, "verify", "--man=no", str(directory / "motel-concierge.service"), str(directory / "motel-concierge.socket")],
                             capture_output=True, text=True, timeout=60, check=False)
        output = out.stdout + out.stderr
        assert "Unknown key" not in output and "Unknown section" not in output, output
        assert "Unknown lvalue" not in output and "Invalid" not in output, output
