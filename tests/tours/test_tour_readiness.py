"""A tour cabin may have exactly one remote forward (loopback, the concierge port) and nothing more.

Both directions: the tour profile passes, and any extra forwarding permission, another port, a wider key option or a
forward nobody asked for still fails the cabin's readiness check."""
import json
import subprocess

import pytest
from rig import file_block

from motel.cabins import guest_agent
from motel.cabins.controller import forward_port_of
from motel.cabins.guestchan import validate_ready
from motel.tours import deps

PORT = 7781
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexample cabin"
STRICT = ["allowtcpforwarding no", "allowagentforwarding no", "allowstreamlocalforwarding no", "x11forwarding no",
          "permittunnel no", "gatewayports no"]
BASE = ["allowusers cabin", "allowusers cabinctl", "authorizedkeysfile /etc/ssh/cabin-keys/%u", "authorizedkeyscommand none",
        "passwordauthentication no", "kbdinteractiveauthentication no", "permitrootlogin no", "forcecommand none",
        "authenticationmethods publickey", "usedns no"]
TOUR = ["allowagentforwarding no", "allowstreamlocalforwarding no", "x11forwarding no", "permittunnel no", "gatewayports no",
        "allowtcpforwarding remote", f"permitlisten 127.0.0.1:{PORT}"]
TABLE = {"nftables": [{"metainfo": {"version": "1"}}, {"table": {"family": "inet", "name": "cabin", "handle": 1}}]}


def key_line(port=PORT, extra=""):
    options = "restrict,pty" + (f',port-forwarding,permitlisten="127.0.0.1:{port}"' if port else "") + extra
    return f'{options},command="/usr/local/bin/motel-guest gate" {KEY}'


def agent(tmp_path, monkeypatch, *, sshd, key, tour=None):
    config = tmp_path / "etc"
    (config / "motel-cabin").mkdir(parents=True)
    (config / "ssh" / "cabin-keys").mkdir(parents=True)
    (config / "sudoers.d").mkdir()
    (config / "motel-cabin" / "expected-nft.json").write_text(json.dumps(TABLE))
    (config / "ssh" / "cabin-keys" / "cabin").write_text(key + "\n")
    (config / "ssh" / "cabin-keys" / "cabinctl").write_text("x\n")
    if tour is not None:
        (config / "motel-cabin" / "tour.json").write_text(json.dumps(tour))
    monkeypatch.setattr(guest_agent.Guest, "trusted", staticmethod(lambda paths: True))

    def run(argv, **kw):
        out = json.dumps(TABLE) if argv[0].endswith("nft") else "\n".join(sshd) if argv[0].endswith("sshd") else ""
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")
    guest = guest_agent.Guest(root=str(tmp_path / "root"))
    return guest.inspect_security(run=run, config=str(config), connection="198.51.100.7 50000 192.0.2.10 22")


TOUR_JSON = {"profile": "tour-guest", "concierge_port": PORT, "own_login": False}


def test_a_plain_cabin_is_unchanged(tmp_path, monkeypatch):
    got = agent(tmp_path, monkeypatch, sshd=STRICT + BASE, key=key_line(None))
    assert got == {"nft": "loaded", "forwarding_disabled": True, "gate": "enforced", "forward_port": None}


def test_a_plain_cabin_with_a_tour_style_forward_fails(tmp_path, monkeypatch):
    """No tour profile was written, so the same relaxed sshd settings are a violation."""
    got = agent(tmp_path, monkeypatch, sshd=TOUR + BASE, key=key_line())
    assert got["forwarding_disabled"] is False and got["forward_port"] is None and got["gate"] != "enforced"


def test_the_tour_profile_passes_with_exactly_that_forward(tmp_path, monkeypatch):
    got = agent(tmp_path, monkeypatch, sshd=TOUR + BASE, key=key_line(), tour=TOUR_JSON)
    assert got["forward_port"] == PORT and got["gate"] == "enforced" and got["nft"] == "loaded"
    assert got["forwarding_disabled"] is False                  # honest: forwarding is not fully disabled


@pytest.mark.parametrize("label,sshd,key", [
    ("tcp forwarding for everything", [x for x in TOUR if not x.startswith("allowtcpforwarding")] + ["allowtcpforwarding yes"],
     key_line()),
    ("local forwarding too", [x for x in TOUR if not x.startswith("allowtcpforwarding")] + ["allowtcpforwarding local"], key_line()),
    ("a second listen entry", [x for x in TOUR if not x.startswith("permitlisten")] + [f"permitlisten 127.0.0.1:{PORT} 0.0.0.0:80"],
     key_line()),
    ("another port", [x for x in TOUR if not x.startswith("permitlisten")] + ["permitlisten 127.0.0.1:9999"], key_line()),
    ("a wildcard listen", [x for x in TOUR if not x.startswith("permitlisten")] + ["permitlisten *:7781"], key_line()),
    ("no listen restriction", [x for x in TOUR if not x.startswith("permitlisten")] + ["permitlisten any"], key_line()),
    ("agent forwarding", [x for x in TOUR if not x.startswith("allowagentforwarding")] + ["allowagentforwarding yes"], key_line()),
    ("x11", [x for x in TOUR if not x.startswith("x11")] + ["x11forwarding yes"], key_line()),
    ("tunnels", [x for x in TOUR if not x.startswith("permittunnel")] + ["permittunnel yes"], key_line()),
    ("gateway ports", [x for x in TOUR if not x.startswith("gatewayports")] + ["gatewayports yes"], key_line()),
    ("a wider key option", TOUR, key_line(extra=",agent-forwarding")),
    ("the key allows another port", TOUR, key_line(port=9999)),
    ("the key has no forward option", TOUR, key_line(port=None)),
    ("a permitopen on the key", TOUR, key_line(extra=',permitopen="10.0.0.1:22"')),
])
def test_anything_wider_than_the_profile_fails(tmp_path, monkeypatch, label, sshd, key):
    got = agent(tmp_path, monkeypatch, sshd=sshd + BASE, key=key, tour=TOUR_JSON)
    assert got["forward_port"] is None, label


@pytest.mark.parametrize("tour", [{"profile": "tour-guest", "concierge_port": 80}, {"profile": "tour-guest", "concierge_port": True},
                                  {"profile": "tour-guest", "concierge_port": None}, {"profile": "other", "concierge_port": PORT},
                                  {"profile": "tour-guest", "concierge_port": PORT, "own_login": True}, "garbage", []])
def test_a_bad_or_own_login_profile_allows_no_forward(tmp_path, monkeypatch, tour):
    got = agent(tmp_path, monkeypatch, sshd=TOUR + BASE, key=key_line(), tour=tour)
    assert got["forward_port"] is None and got["gate"] != "enforced"


def test_an_own_login_guest_keeps_the_strict_rules(tmp_path, monkeypatch):
    own = dict(TOUR_JSON, own_login=True, concierge_port=None)
    got = agent(tmp_path, monkeypatch, sshd=STRICT + BASE, key=key_line(None), tour=own)
    assert got["forwarding_disabled"] is True and got["gate"] == "enforced" and got["forward_port"] is None


# -- the controller side ---------------------------------------------------------------------------------------------

def test_the_ready_document_carries_the_forward_port():
    doc = {"bootstrap": "done", "tailnet_ip": None, "tcp_listeners": [], "nft": "loaded", "gate": "enforced",
           "forwarding_disabled": False, "forward_port": PORT}
    assert validate_ready(dict(doc))["forward_port"] == PORT
    assert validate_ready({k: v for k, v in doc.items() if k != "forward_port"})["forward_port"] is None
    for bad in (80, True, "7781", 70000, 1.5):
        with pytest.raises(Exception):  # noqa: B017
            validate_ready(dict(doc, forward_port=bad))


def test_the_expected_port_comes_from_the_extras_the_cabin_was_built_with():
    assert forward_port_of(None) is None
    assert forward_port_of(deps.userdata_extras(hosts=["github.com"], dns_resolvers=["185.12.64.1"], concierge_port=PORT)) == PORT
    assert forward_port_of(deps.userdata_extras(hosts=[], dns_resolvers=["185.12.64.1"], concierge_port=None)) is None
    assert forward_port_of(deps.userdata_extras(hosts=[], dns_resolvers=["185.12.64.1"], concierge_port=PORT, own_login=True)) is None


def test_the_cloud_init_profile_and_the_agent_agree():
    """The drop-in the guest part writes and what the agent accepts are the same settings."""
    extras = deps.userdata_extras(hosts=[], dns_resolvers=["185.12.64.1"], concierge_port=PORT)
    dropin = file_block(extras.files, "/etc/ssh/sshd_config.d/04-cabin-tour.conf")
    assert f"PermitListen 127.0.0.1:{PORT}" in dropin and "AllowTcpForwarding remote" in dropin
    assert extras.key_options == f',port-forwarding,permitlisten="127.0.0.1:{PORT}"'
    assert f'"concierge_port":{PORT}' in extras.files and '"profile":"tour-guest"' in extras.files
    assert "Match User cabin" in dropin
