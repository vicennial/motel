import base64
import gzip
import re

import pytest
from cabin_fakes import HOST_IPV4, HOST_IPV6, fake_keygen, pubkey, read_lib

from motel.cabins import userdata
from motel.cabins.errors import Fail

REC = {"lease_id": "build-0123456789ab", "name": "motel-build-0123456789ab", "expires_at": "2026-11-08T14:00:00Z"}


def render(mode="ssh-allowlist", **over):
    private, public = fake_keygen("/tmp", "host_x_unused", "c") if False else (
        "-----BEGIN OPENSSH PRIVATE KEY-----\n" + "H" * 70 + "\n-----END OPENSSH PRIVATE KEY-----\n", pubkey("H"))
    kw = {"rec": REC, "mode": mode, "host_private": private, "host_public": public, "client_public": pubkey("U"),
              "controller_public": pubkey("C"), "guest_agent": read_lib("guest_agent.py", "rb"), "packages": ["git", "rsync"],
              "sources": [HOST_IPV4 + "/32", HOST_IPV6 + "/128"], "idle_seconds": 1800}
    if mode == "tailscale":
        kw.update(tag="tag:motel-cabin", authkey="tskey-auth-kABC123CNTRL-0123456789abcdef", sources=[])
    kw.update(over)
    return userdata.render_user_data(read_lib("cloud-init.yaml"), **kw)


def test_expiry_dead_mans_switch_is_rendered_and_started_first():
    text = render()
    assert "cabin-guest-expiry.timer" in text and "cabin-guest-expiry.service" in text
    assert "ExecStart=/usr/local/bin/motel-guest expire" in text
    assert text.index("enable --now cabin-guest-expiry.timer") < text.index("apt-get update")


def test_lease_file_carries_expiry_grace_and_idle_floor():
    import json
    text = render(idle_seconds=2400)
    blob = re.search(r"/etc/motel-cabin/lease.json\n\s+permissions: '0644'\n\s+encoding: b64\n\s+content: (\S+)", text)
    doc = json.loads(base64.b64decode(blob.group(1)))
    assert doc["expires"] == REC["expires_at"] and doc["grace_s"] == 600 and doc["idle_s"] == 2400


def test_guest_agent_is_embedded_intact():
    text = render()
    blob = re.search(r"encoding: gz\+b64\n\s+content: (\S+)", text).group(1)
    assert gzip.decompress(base64.b64decode(blob)) == read_lib("guest_agent.py", "rb")


def test_allowlist_guest_filter_allows_only_exact_host_addresses():
    nft = userdata.guest_nft("ssh-allowlist", [HOST_IPV4 + "/32", HOST_IPV6 + "/128"])
    assert f"ip saddr {{ {HOST_IPV4} }} tcp dport 22 accept" in nft
    assert f"ip6 saddr {{ {HOST_IPV6} }} tcp dport 22 accept" in nft
    assert "policy drop" in nft and "0.0.0.0" not in nft and "/0" not in nft
    text = render()
    assert "ListenAddress 0.0.0.0" in text and "tailscale up" not in text


@pytest.mark.parametrize("sources", [[], ["0.0.0.0/0"], ["203.0.113.0/24"], ["bad"]])
def test_guest_filter_refuses_broad_or_missing_sources(sources):
    with pytest.raises(Fail):
        userdata.guest_nft("ssh-allowlist", sources)


def test_tailscale_render_has_no_public_listener_and_wipes_the_key():
    text = render("tailscale")
    assert "ListenAddress 127.0.0.1" in text and "tailscale up --auth-key=file:" in text
    assert "shred -u /run/motel-cabin/authkey" in text
    assert "AddressFamily inet" in text


def test_hardening_is_present():
    text = render()
    for needle in ("PasswordAuthentication no", "AllowTcpForwarding no", "AllowAgentForwarding no",
                   "restrict,pty,command=\"/usr/local/bin/motel-guest gate\"",
                   "restrict,command=\"/usr/local/bin/motel-guest ctl\"", "disable_root: true",
                   "AllowUsers cabin cabinctl", "AuthorizedKeysFile /etc/ssh/cabin-keys/%u"):
        assert needle in text, needle
    assert "@@" not in text


def test_disk_fragment_only_when_a_volume_exists():
    assert "/mnt/cabin-disk" not in render()
    with_disk = render(volume_id=4242)
    assert "scsi-0HC_Volume_4242" in with_disk and "ln -sfn /mnt/cabin-disk" in with_disk and "getent passwd cabin" in with_disk
    assert "disk_state=none" in render()


@pytest.mark.parametrize("over", [
    {"client_public": "ssh-ed25519 AAAA\ncommand=evil"}, {"packages": ["git; rm -rf /"]},
    {"host_private": "not a key"}, {"volume_id": "1;rm"}, {"controller_public": "ssh-rsa AAAA"}])
def test_injection_is_refused(over):
    with pytest.raises(Fail):
        render(**over)


def test_tailscale_input_validation():
    with pytest.raises(Fail):
        render("tailscale", authkey="not-a-key")
    with pytest.raises(Fail):
        render("tailscale", tag="tag:BAD TAG")
    with pytest.raises(Fail):
        render("something-else")


def test_user_data_size_limit(monkeypatch):
    monkeypatch.setattr(userdata, "USER_DATA_LIMIT", 100)
    with pytest.raises(Fail, match="limit"):
        render()
