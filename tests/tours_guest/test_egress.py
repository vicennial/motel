"""The tour-guest egress profile, rendered as text, and its place in the cabin user data."""
import json
import pathlib
import re
import subprocess
import time

import pytest
from cabin_fakes import HOST_IPV4, HOST_IPV6, pubkey, read_lib

from motel.cabins import userdata
from motel.cabins.errors import Fail
from motel.tours import guest
from motel.tours.guest import egress

REC = {"lease_id": "tour-0123456789ab", "name": "motel-tour-0123456789ab", "expires_at": "2026-11-08T14:00:00Z"}
RESOLVERS = ["185.12.64.1", "185.12.64.2"]


def render(extras=None, mode="ssh-allowlist"):
    private = "-----BEGIN OPENSSH PRIVATE KEY-----\n" + "H" * 70 + "\n-----END OPENSSH PRIVATE KEY-----\n"
    kw = {"rec": REC, "mode": mode, "host_private": private, "host_public": pubkey("H"), "client_public": pubkey("U"),
          "controller_public": pubkey("C"), "guest_agent": read_lib("guest_agent.py", "rb"), "packages": ["git", "rsync"],
          "sources": [HOST_IPV4 + "/32", HOST_IPV6 + "/128"], "idle_seconds": 1800, "dns": RESOLVERS, "extras": extras}
    if mode == "tailscale":
        kw.update(tag="tag:motel-cabin", authkey="tskey-auth-kABC123CNTRL-0123456789abcdef", sources=[])
    return userdata.render_user_data(read_lib("cloud-init.yaml"), **kw)


def file_block(text, path):
    """The content of one cloud-init write_files entry, dedented (gz+b64 entries are decoded)."""
    packed = re.search(r"- path: " + re.escape(path) + r"\n    permissions: '\d+'\n    encoding: gz\+b64\n    content: (\S+)\n", text)
    if packed:
        import base64
        import gzip
        return gzip.decompress(base64.b64decode(packed.group(1))).decode()
    match = re.search(r"- path: " + re.escape(path) + r"\n(?:    [^\n]*\n)*?    content: \|\n((?:      [^\n]*\n|\n)+)", text)
    assert match, path
    return "\n".join(line[6:] for line in match.group(1).splitlines()) + "\n"


def decoded_files(text):
    """path -> content for every write_files entry of the rendered user data (gz+b64 entries decoded); needs yaml."""
    import base64
    import gzip
    yaml = pytest.importorskip("yaml")
    out = {}
    for item in yaml.safe_load(text)["write_files"]:
        body = item["content"]
        out[item["path"]] = gzip.decompress(base64.b64decode(body)).decode() if item.get("encoding") == "gz+b64" else body
    return out


# ---------------------------------------------------------------------------------------------- the rules

def test_output_chain_drops_by_default_and_allows_only_the_listed_things():
    nft = egress.render_nft(RESOLVERS)
    assert "hook output priority 0; policy drop;" in nft and "policy accept" not in nft
    accepts = [line.strip() for line in nft.splitlines() if line.strip().endswith("accept")]
    assert accepts == ["ct state established,related accept", 'oif "lo" accept',
                       "ip daddr { 185.12.64.1, 185.12.64.2 } udp dport 53 accept",
                       "ip daddr { 185.12.64.1, 185.12.64.2 } tcp dport 53 accept",
                       "ip daddr { @DHCP_DEST@ } udp sport 68 udp dport 67 accept",
                       "ip daddr @allow4 tcp dport { 443 } accept"]
    assert "ip daddr 169.254.169.254 drop" in nft
    assert nft.index("169.254.169.254 drop") < nft.index("@allow4")      # metadata is dropped before anything allows it


def test_established_traffic_is_allowed_so_inbound_ssh_replies_flow_and_the_input_chain_is_not_touched():
    nft = egress.render_nft(RESOLVERS)
    assert "ct state established,related accept" in nft
    assert "hook input" not in nft and "hook forward" not in nft          # inbound policy stays the cabin table's
    assert "table inet cabin_tour" in nft and "table inet cabin " not in nft


def test_dns_goes_only_to_the_configured_resolvers():
    nft = egress.render_nft(["198.51.100.7"])
    assert "ip daddr { 198.51.100.7 } udp dport 53 accept" in nft and "185.12.64" not in nft
    assert re.findall(r"dport 53", nft) == ["dport 53"] * 4          # two drops for other users, two accepts
    for bad in ([], ["127.0.0.1"], ["169.254.169.254"], ["not-an-ip"], ["2001:db8::1"]):
        with pytest.raises(Fail):
            egress.render_nft(bad)


def test_ports_default_to_443_and_are_validated():
    assert "tcp dport { 443 }" in egress.render_nft(RESOLVERS)
    assert "tcp dport { 80, 443 }" in egress.render_nft(RESOLVERS, ports=(443, 80))
    for bad in ((), (0,), (70000,)):
        with pytest.raises(Fail):
            egress.render_nft(RESOLVERS, ports=bad)


# ---------------------------------------------------------------------------------------------- the hosts

def test_hosts_are_exact_lowercase_names_or_public_addresses():
    assert egress.normalize_hosts(["API.Example.com", "git.example.org.", "1.1.1.1", "api.example.com"]) == (
        "1.1.1.1", "api.example.com", "git.example.org")
    assert egress.normalize_hosts([]) == () and egress.normalize_hosts(None) == ()
    for bad in ("*.example.com", "localhost", "example", "a_b.example.com", "http://example.com", "example.com/x",
                "10.0.0.5", "169.254.169.254", "127.0.0.1", "foo.internal", "-bad.example.com", "", 5):
        with pytest.raises(Fail):
            egress.normalize_hosts([bad])
    with pytest.raises(Fail):
        egress.normalize_hosts([f"h{i}.example.com" for i in range(egress.MAX_HOSTS + 1)])


def test_extras_signature_and_content():
    extras = guest.userdata_extras(["Example.com"], RESOLVERS)
    assert isinstance(extras, userdata.GuestExtras) and extras.key_options == ""
    conf = json.loads(file_block(extras.files.replace("  - path", "- path"), "/etc/motel-cabin/tour.json"))
    assert conf["egress_hosts"] == ["example.com"] and conf["concierge_port"] is None and conf["own_login"] is False
    assert conf["resolvers"] == RESOLVERS and conf["refresh_s"] == 300 and conf["refresh_window_s"] == 21600


# ---------------------------------------------------------------------------------------------- user data

def test_extras_add_the_tour_table_user_and_input_dir_and_leave_the_cabin_table_alone():
    plain = render()
    text = render(guest.userdata_extras(["git.example.org"], RESOLVERS, concierge_port=17781))
    # the cabin's own filter file is byte-identical
    assert file_block(text, "/etc/motel-cabin/guest.nft") == file_block(plain, "/etc/motel-cabin/guest.nft")
    assert "ip daddr 169.254.169.254 drop" in file_block(text, "/etc/motel-cabin/guest.nft")
    tour = file_block(text, "/etc/motel-cabin/tour.nft.in")
    assert tour == egress.render_nft(RESOLVERS)
    assert "ExecStart=/usr/sbin/nft -f /etc/motel-cabin/tour.nft\n" in file_block(
        text, "/etc/systemd/system/motel-tour-firewall.service")
    # inbound ssh from the controller is still only from the exact addresses, in the cabin table
    assert f"ip saddr {{ {HOST_IPV4} }} tcp dport 22 accept" in file_block(text, "/etc/motel-cabin/guest.nft")
    boot = text[text.index("stage tour-user"):]
    assert "useradd --create-home --shell /bin/bash --user-group" in boot and "passwd -l guest" in boot
    assert "install -d -o root -g guest -m 0750 /var/lib/motel-guest/in" in boot
    assert "install -d -o root -g root -m 0755 /usr/local/lib/motel-tour" in boot
    assert "systemctl enable --now motel-tour-firewall.service" in boot
    # order: packages first (apt needs the network), then the tour filter, then ready.json is written as done
    assert text.index("apt-get install") < text.index("stage tour-user") < text.index('"bootstrap": "done"')
    assert text.index("systemctl enable --now cabin-guest-firewall.service") < text.index("stage tour-user")
    assert "sudo" not in file_block(text, "/etc/motel-cabin/tour.json")             # the guest user gets no sudo


def test_ssh_does_not_start_without_the_tour_filter():
    text = render(guest.userdata_extras([], RESOLVERS))
    for unit in ("ssh.service.d", "ssh.socket.d"):
        block = file_block(text, f"/etc/systemd/system/{unit}/20-tour-firewall.conf")
        assert "Requires=motel-tour-firewall.service" in block and "After=motel-tour-firewall.service" in block


def test_nothing_can_start_the_firewall_unit_before_its_rules_are_complete():
    """R-H1: ssh requires the firewall unit, and the unit's own ExecStartPre renders the rules it then loads."""
    text = render(guest.userdata_extras(["git.example.org"], RESOLVERS, concierge_port=17781))
    files = decoded_files(text)
    unit = files["/etc/systemd/system/motel-tour-firewall.service"]
    assert re.findall(r"^Exec\w+=.*$", unit, re.M) == [f"ExecStartPre={egress.TOUR_NFT_RENDER}",
                                                       f"ExecStart=/usr/sbin/nft -f {egress.TOUR_NFT}"]
    assert unit.index("ExecStartPre") < unit.index("ExecStart=")
    # the rules file the unit loads is not shipped: only the template with tokens is, so only the script can make it
    assert egress.TOUR_NFT not in files and "@DHCP_DEST@" in files[egress.TOUR_NFT_TEMPLATE]
    script = files[egress.TOUR_NFT_RENDER]
    assert script == egress.RENDER_SCRIPT and "nft -c -f" in script
    assert script.index("nft -c -f") < script.index(f"mv -f \"$tmp\" {egress.TOUR_NFT}")        # checked, then put in place
    # every way to start or restart ssh goes through the unit; nothing in the bootstrap loads the rules itself
    for name in ("ssh.service.d", "ssh.socket.d"):
        drop_in = files[f"/etc/systemd/system/{name}/20-tour-firewall.conf"]
        assert "Requires=motel-tour-firewall.service" in drop_in and "After=motel-tour-firewall.service" in drop_in
    loaders = [line for line in text.splitlines() if re.search(r"\bnft\b.*\s-f\s", line) and egress.TOUR_NFT in line
               and not line.lstrip().startswith(("ExecStart=", "ExecStartPre="))]
    assert loaders == []
    # the bootstrap restarts ssh before the tour boot part runs, and that part no longer fills tokens
    assert text.index("systemctl restart ssh") < text.index("stage tour-user")
    assert "@DHCP_DEST@" not in text.split("stage tour-user", 1)[1]


def test_without_extras_the_user_data_has_no_tour_parts():
    text = render()
    assert "cabin_tour" not in text and "motel-tour" not in text and "stage tour" not in text
    assert 'restrict,pty,command="/usr/local/bin/motel-guest gate" ' + pubkey("U") in text
    assert "@@" not in text


def test_concierge_port_opens_one_loopback_listen_for_the_cabin_account_only():
    text = render(guest.userdata_extras([], RESOLVERS, concierge_port=17781))
    drop_in = file_block(text, "/etc/ssh/sshd_config.d/04-cabin-tour.conf")
    assert "Match User cabin" in drop_in and "AllowTcpForwarding remote" in drop_in
    assert "PermitListen 127.0.0.1:17781" in drop_in
    assert 'restrict,pty,port-forwarding,permitlisten="127.0.0.1:17781",command="/usr/local/bin/motel-guest gate" ' in text
    # sorted before 10-cabin.conf, whose global "AllowTcpForwarding no" must not win for the cabin account
    assert "04-cabin-tour.conf" < "10-cabin.conf"
    assert "AllowTcpForwarding no" in file_block(text, "/etc/ssh/sshd_config.d/10-cabin.conf")
    none = render(guest.userdata_extras([], RESOLVERS))
    assert "04-cabin-tour.conf" not in none and "permitlisten" not in none
    for bad in (0, 80, 70000, True, "1"):
        with pytest.raises(Fail):
            guest.userdata_extras([], RESOLVERS, concierge_port=bad)


def test_own_login_allows_api_anthropic_com_and_no_concierge_forward():
    extras = guest.userdata_extras(["git.example.org"], RESOLVERS, concierge_port=17781, own_login=True)
    assert extras.key_options == ""
    conf = json.loads(file_block(extras.files.replace("  - path", "- path"), "/etc/motel-cabin/tour.json"))
    assert conf["egress_hosts"] == ["api.anthropic.com", "git.example.org"] and conf["own_login"] is True
    assert conf["concierge_port"] is None
    text = render(extras)
    assert "04-cabin-tour.conf" not in text and "permitlisten" not in text
    assert egress.OWN_LOGIN_HOSTS == ("api.anthropic.com",)
    # nothing the sign-in flow needs is allowed by default
    for host in ("claude.ai", "platform.claude.com", "statsig", "sentry", "datadog"):
        assert host not in json.dumps(conf)
    alone = json.loads(file_block(guest.userdata_extras([], RESOLVERS, own_login=True).files.replace("  - path", "- path"),
                                  "/etc/motel-cabin/tour.json"))
    assert alone["egress_hosts"] == ["api.anthropic.com"]


def test_tailscale_cabins_cannot_be_tour_guests():
    with pytest.raises(Fail):
        render(guest.userdata_extras([], RESOLVERS), mode="tailscale")


def test_user_data_stays_under_the_provider_limit_and_parses_as_yaml():
    text = render(guest.userdata_extras([f"h{i}.example.com" for i in range(64)], RESOLVERS, concierge_port=17781))
    assert len(text.encode()) < userdata.USER_DATA_LIMIT
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load(text)
    paths = {item["path"] for item in doc["write_files"]}
    assert {"/etc/motel-cabin/tour.nft.in", "/usr/local/sbin/motel-tour-nft", "/etc/motel-cabin/tour.json", "/usr/local/sbin/motel-tour-egress",
            "/etc/systemd/system/motel-tour-firewall.service", "/etc/ssh/sshd_config.d/04-cabin-tour.conf"} <= paths
    by_path = {item["path"]: item for item in doc["write_files"]}
    assert decoded_files(text)["/etc/motel-cabin/tour.nft.in"] == egress.render_nft(RESOLVERS)
    assert "/etc/motel-cabin/tour.nft" not in by_path
    assert by_path["/etc/ssh/cabin-keys/cabin"]["content"].startswith('restrict,pty,port-forwarding,permitlisten="127.0.0.1:17781",command=')
    assert text.count("#cloud-config") == 1


def test_invalid_extras_are_refused_by_the_renderer():
    for bad in (userdata.GuestExtras(files="  - path: x\n@@", boot=""), userdata.GuestExtras(files="", boot="", key_options="; rm -rf"),
                "not extras"):
        with pytest.raises(Fail):
            render(bad)


# ---------------------------------------------------------------------------------------------- runner shipping

def test_runner_files_and_command():
    files = guest.runner_files()
    assert sorted(files) == ["adapters.py", "runner.py"] and all(len(b) > 1000 for b in files.values())
    argv = guest.runner_argv()
    assert argv[:6] == ["sudo", "-n", "-u", "guest", "-H", "--"] and argv[-1] == "/usr/local/lib/motel-tour/runner.py"
    for blob in files.values():
        compile(blob, "guest-file", "exec")


# ---------------------------------------------------------------------------------------------- the refresher script

class Refresher:
    """The refresher script run in-process against temporary files, with nft, systemctl and DNS faked."""

    def __init__(self, tmp_path, monkeypatch, hosts=("a.example.com", "b.example.com"), hosts_text="127.0.0.1 localhost\n"):
        self.conf, self.state = tmp_path / "tour.json", tmp_path / "state.json"
        self.hosts = tmp_path / "hosts"
        self.hosts.write_text(hosts_text)
        self.conf.write_text(json.dumps({"egress_hosts": list(hosts), "refresh_window_s": 3600, "resolvers": RESOLVERS}))
        self.ns = {"__name__": "refresher"}
        exec(compile(egress.REFRESHER, "motel-tour-egress", "exec"), self.ns)
        self.ns["CONF"], self.ns["STATE"], self.ns["HOSTS"] = str(self.conf), str(self.state), str(self.hosts)
        self.answers = {}
        self.calls = []                 # (argv, stdin text)
        self.set_elements = set()       # what the kernel set holds
        self.nft_ok = True
        self.ns["lookup"] = self.lookup
        monkeypatch.setattr("subprocess.run", self.run_command)

    def lookup(self, host, resolvers):
        self.asked = getattr(self, "asked", []) + [(host, list(resolvers))]
        if host not in self.answers:
            raise OSError("no such host")
        return list(self.answers[host])

    def run_command(self, argv, **kw):
        self.calls.append((argv, kw.get("input", b"").decode()))
        if argv[:2] == ["/usr/sbin/nft", "list"]:
            out = b"table inet cabin_tour {\n set allow4 {\n  elements = { " + ", ".join(sorted(self.set_elements)).encode() + b" }\n }\n}\n" \
                if self.set_elements else b"table inet cabin_tour {\n set allow4 {\n }\n}\n"
            return subprocess.CompletedProcess(argv, 0, stdout=out)
        if argv[:2] == ["/usr/sbin/nft", "-f"]:
            if not self.nft_ok:
                return subprocess.CompletedProcess(argv, 1)
            body = kw["input"].decode().split("{", 1)[1].rsplit("}", 1)[0]
            self.set_elements |= {item.strip() for item in body.split(",") if item.strip()}
        return subprocess.CompletedProcess(argv, 0, stdout=b"")

    def adds(self):
        return [text for argv, text in self.calls if argv[:2] == ["/usr/sbin/nft", "-f"]]

    def main(self):
        return self.ns["main"]()

    def doc(self):
        return json.loads(self.state.read_text())


@pytest.fixture
def refresher(tmp_path, monkeypatch):
    return Refresher(tmp_path, monkeypatch)


def test_refresher_adds_only_global_addresses_and_only_new_ones(refresher):
    refresher.answers = {"a.example.com": ["1.1.1.1", "10.0.0.5", "169.254.169.254", "127.0.0.1"],
                         "b.example.com": ["198.51.100.9", "8.8.8.8"]}      # 198.51.100.0/24 is documentation space
    assert refresher.main() == 0
    assert refresher.adds() == ["add element inet cabin_tour allow4 { 1.1.1.1, 8.8.8.8 }\n"]
    for forbidden in ("10.0.0.5", "169.254", "127.0.0.1", "198.51.100.9"):
        assert forbidden not in refresher.adds()[0]
    # a second run with the same answers adds nothing; a new address is added alone
    refresher.calls.clear()
    assert refresher.main() == 0 and refresher.adds() == []
    refresher.answers["a.example.com"].append("9.9.9.9")
    assert refresher.main() == 0 and refresher.adds() == ["add element inet cabin_tour allow4 { 9.9.9.9 }\n"]
    doc = refresher.doc()
    assert doc["addresses"] == ["1.1.1.1", "8.8.8.8", "9.9.9.9"] and doc["failed_hosts"] == []


def test_refresher_writes_the_resolved_names_into_a_managed_hosts_block(refresher):
    refresher.answers = {"a.example.com": ["1.1.1.1", "9.9.9.9"], "b.example.com": ["8.8.8.8"]}
    assert refresher.main() == 0
    text = refresher.hosts.read_text()
    assert text == ("127.0.0.1 localhost\n# BEGIN motel-tour\n1.1.1.1 a.example.com\n9.9.9.9 a.example.com\n"
                    "8.8.8.8 b.example.com\n# END motel-tour\n")
    # idempotent, and a new answer rewrites the block in place instead of appending a second one
    assert refresher.main() == 0 and refresher.hosts.read_text() == text
    refresher.answers["b.example.com"] = ["93.184.216.34"]
    assert refresher.main() == 0
    again = refresher.hosts.read_text()
    assert again.count("# BEGIN motel-tour") == 1 and again.startswith("127.0.0.1 localhost\n")
    assert "93.184.216.34 b.example.com" in again and "8.8.8.8 b.example.com" in again        # addresses are never removed
    assert not (refresher.hosts.parent / "hosts.motel-tmp").exists()


def test_refresher_keeps_other_hosts_lines_around_its_block(tmp_path, monkeypatch):
    before = "127.0.0.1 localhost\n::1 ip6-localhost\n# BEGIN motel-tour\n9.9.9.9 stale.example.com\n# END motel-tour\n10.1.1.1 db\n"
    r = Refresher(tmp_path, monkeypatch, hosts=["a.example.com"], hosts_text=before)
    r.answers = {"a.example.com": ["1.1.1.1"]}
    assert r.main() == 0
    assert r.hosts.read_text() == ("127.0.0.1 localhost\n::1 ip6-localhost\n# BEGIN motel-tour\n1.1.1.1 a.example.com\n"
                                   "# END motel-tour\n10.1.1.1 db\n")


def test_refresher_does_not_touch_hosts_for_literal_addresses_or_when_nothing_resolved(tmp_path, monkeypatch):
    r = Refresher(tmp_path, monkeypatch, hosts=["8.8.8.8"])
    r.answers = {"8.8.8.8": ["8.8.8.8"]}
    assert r.main() == 0 and r.adds() == ["add element inet cabin_tour allow4 { 8.8.8.8 }\n"]
    assert r.hosts.read_text() == "127.0.0.1 localhost\n"
    (tmp_path / "second").mkdir()
    none = Refresher(tmp_path / "second", monkeypatch, hosts=["a.example.com"])
    assert none.main() == 1 and none.hosts.read_text() == "127.0.0.1 localhost\n"
    assert none.doc()["failed_hosts"] == ["a.example.com"]


def test_failed_nft_add_is_not_remembered_and_is_retried(refresher):
    refresher.answers = {"a.example.com": ["1.1.1.1"]}
    refresher.nft_ok = False
    assert refresher.main() == 1
    assert refresher.doc()["addresses"] == [] and "1.1.1.1" not in refresher.hosts.read_text()
    refresher.nft_ok = True
    assert refresher.main() == 0 and refresher.set_elements == {"1.1.1.1"}
    assert "1.1.1.1 a.example.com" in refresher.hosts.read_text()


def test_after_a_reboot_the_empty_set_is_refilled_from_the_state_file(refresher):
    refresher.answers = {"a.example.com": ["1.1.1.1"], "b.example.com": ["8.8.8.8"]}
    assert refresher.main() == 0 and refresher.set_elements == {"1.1.1.1", "8.8.8.8"}
    refresher.set_elements.clear()                      # the firewall unit loaded the table again: the set is empty
    refresher.calls.clear()
    refresher.answers = {}                              # and DNS is not up yet
    assert refresher.main() == 0                        # nothing resolved, but the state remembers
    assert refresher.adds() == ["add element inet cabin_tour allow4 { 1.1.1.1, 8.8.8.8 }\n"]
    assert refresher.set_elements == {"1.1.1.1", "8.8.8.8"}
    refresher.calls.clear()
    assert refresher.main() == 0 and refresher.adds() == []     # a set that is not empty is left alone


def test_refresher_stops_and_disables_its_timer_after_the_window_but_still_restores_the_set(refresher):
    refresher.state.write_text(json.dumps({"first_run": 1.0, "addresses": ["1.1.1.1"],
                                           "by_host": {"a.example.com": ["1.1.1.1"]}}))
    refresher.ns["lookup"] = lambda *a: pytest.fail("must not resolve after the window")
    assert refresher.main() == 0
    assert refresher.adds() == ["add element inet cabin_tour allow4 { 1.1.1.1 }\n"]       # a reboot after the window
    assert ["/usr/bin/systemctl", "disable", "--now", "motel-tour-egress.timer"] in [argv for argv, _ in refresher.calls]
    assert "1.1.1.1 a.example.com" in refresher.hosts.read_text()


def test_the_set_cap_is_respected(refresher):
    refresher.state.write_text(json.dumps({"first_run": time.time(), "addresses": [f"1.0.{i // 250}.{i % 250 + 1}" for i in range(egress.MAX_SET_ELEMENTS)]}))
    refresher.answers = {"a.example.com": ["1.1.1.1"]}
    refresher.main()
    assert refresher.adds() == [] and "1.1.1.1" not in refresher.hosts.read_text()


# ---------------------------------------------------------------------------------------------- DNS, DHCP (M4, L6)

def test_port_53_is_closed_to_every_user_except_root_before_loopback_is_accepted():
    nft = egress.render_nft(RESOLVERS)
    lines = [line.strip() for line in nft.splitlines()]
    drops = [line for line in lines if "dport 53" in line and line.endswith("drop")]
    assert drops == ["udp dport 53 meta skuid != 0 drop", "tcp dport 53 meta skuid != 0 drop"]
    first_accept_of_loopback = lines.index('oif "lo" accept')
    assert all(lines.index(drop) < first_accept_of_loopback for drop in drops)      # also covers any stub on loopback
    # nothing else opens 53: the only accepts for it name the configured resolvers
    assert [line for line in lines if "dport 53" in line and line.endswith("accept")] == [
        "ip daddr { 185.12.64.1, 185.12.64.2 } udp dport 53 accept", "ip daddr { 185.12.64.1, 185.12.64.2 } tcp dport 53 accept"]


def test_dhcp_is_limited_to_broadcast_and_the_dhcp_server():
    nft = egress.render_nft(RESOLVERS)
    assert "ip daddr { @DHCP_DEST@ } udp sport 68 udp dport 67 accept" in nft
    assert "\n    udp sport 68 udp dport 67 accept" not in nft            # the old, destination-free rule is gone


def run_render(tmp_path, *, gateway, template=None, saved_gateway=None, nft_ok=True):
    """Run the unit's ExecStartPre script with fake `ip` and `nft` and temporary files; return (result, rules, paths)."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "ip").write_text("#!/bin/sh\nif [ -n \"$GATEWAY\" ]; then echo \"default via $GATEWAY dev eth0 proto dhcp\"; fi\n")
    (bin_dir / "nft").write_text('#!/bin/sh\n[ "$NFT_OK" = 1 ] || { echo "nft: syntax error" >&2; exit 1; }\n')
    for name in ("ip", "nft"):
        (bin_dir / name).chmod(0o755)
    paths = {name: tmp_path / name for name in ("tour.nft.in", "tour.nft", "dhcp-server")}
    paths["tour.nft.in"].write_text(template or egress.render_nft(RESOLVERS))
    if saved_gateway:
        paths["dhcp-server"].write_text(saved_gateway + "\n")
    script = (egress.RENDER_SCRIPT.replace(egress.TOUR_NFT_TEMPLATE, str(paths["tour.nft.in"]))
              .replace(egress.TOUR_NFT, str(paths["tour.nft"])).replace(egress.DHCP_STATE, str(paths["dhcp-server"])))
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False,
                          env={"PATH": f"{bin_dir}:/usr/bin:/bin", "GATEWAY": gateway, "NFT_OK": "1" if nft_ok else "0"})
    return proc, (paths["tour.nft"].read_text() if paths["tour.nft"].exists() else None), paths


def run_fixup(tmp_path, *, gateway, rules=None):
    proc, text, _ = run_render(tmp_path, gateway=gateway, template=rules)
    assert proc.returncode == 0, proc.stderr
    return text


def test_boot_fills_in_the_dhcp_server_and_port_53_stays_root_only(tmp_path):
    text = run_fixup(tmp_path, gateway="172.31.1.1")
    assert "@DHCP_DEST@" not in text and "@" not in text.replace("@allow4", "")
    assert "udp dport 53 meta skuid != 0 drop" in text and "tcp dport 53 meta skuid != 0 drop" in text
    assert "ip daddr { 255.255.255.255, 172.31.1.1 } udp sport 68 udp dport 67 accept" in text


@pytest.mark.parametrize("gateway,dest", [
    ("", "255.255.255.255"),                                      # no default route
    ("172.31.1.1", "255.255.255.255, 172.31.1.1"),
    ("not-an-address; drop", "255.255.255.255")])                  # garbage is never pasted into the rules
def test_boot_fixup_cases(tmp_path, gateway, dest):
    text = run_fixup(tmp_path, gateway=gateway)
    assert f"ip daddr {{ {dest} }} udp sport 68" in text and "@DHCP_DEST@" not in text


def test_at_early_boot_the_remembered_dhcp_server_is_used_and_a_known_one_is_remembered(tmp_path):
    _, text, paths = run_render(tmp_path, gateway="", saved_gateway="172.31.1.1")        # no route yet
    assert "ip daddr { 255.255.255.255, 172.31.1.1 } udp sport 68" in text
    run_render(tmp_path, gateway="172.31.1.254")
    assert paths["dhcp-server"].read_text() == "172.31.1.254\n"


def test_a_failing_check_leaves_no_rules_file_and_fails_the_script(tmp_path):
    proc, text, paths = run_render(tmp_path, gateway="", nft_ok=False)
    assert proc.returncode != 0 and text is None and not paths["tour.nft"].exists()


def test_the_filled_in_rules_load_when_nft_is_available(tmp_path):
    import shutil
    if shutil.which("nft") is None:
        pytest.skip("nft is not installed here; the syntax is checked on the guest by `nft -c -f` at boot")
    text = run_fixup(tmp_path, gateway="172.31.1.1")
    path = tmp_path / "filled.nft"
    path.write_text(text)
    proc = subprocess.run(["unshare", "--user", "--map-root-user", "--net", "nft", "-c", "-f", str(path)],
                          capture_output=True, text=True, check=False)
    if "Operation not permitted" in proc.stderr or "Permission denied" in proc.stderr:
        pytest.skip("no user namespace here")
    assert proc.returncode == 0, proc.stderr


# ---------------------------------------------------------------------------------------------- the guest account (L5)

def run_guest_check(tmp_path, *, uid="1001", primary="guest", groups="guest", sudo_out="User guest is not allowed to run sudo on host."):
    bin_dir = tmp_path / "fakebin2"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "id").write_text('#!/bin/sh\ncase "$1" in -u) echo "$UID_OUT";; -gn) echo "$PRIMARY";; -Gn) echo "$GROUPS_OUT";; esac\n')
    (bin_dir / "sudo").write_text('#!/bin/sh\necho "$SUDO_OUT"\n')
    for name in ("id", "sudo"):
        (bin_dir / name).chmod(0o755)
    return subprocess.run(["bash", "-c", "set -euo pipefail\n" + egress.GUEST_CHECK], capture_output=True, text=True, check=False,
                          env={"PATH": f"{bin_dir}:/usr/bin:/bin", "UID_OUT": uid, "PRIMARY": primary, "GROUPS_OUT": groups,
                               "SUDO_OUT": sudo_out})


def test_an_unprivileged_guest_account_passes_the_check(tmp_path):
    assert run_guest_check(tmp_path).returncode == 0


@pytest.mark.parametrize("kw", [{"groups": "guest sudo"}, {"groups": "guest docker"}, {"primary": "users", "groups": "users"},
                                {"uid": "0"}, {"sudo_out": "User guest may run the following commands on host:\n    (ALL) NOPASSWD: ALL"},
                                {"sudo_out": "sudo: command not found"}, {"sudo_out": ""}])
def test_a_guest_account_with_more_than_its_own_group_or_any_sudo_stops_the_boot(tmp_path, kw):
    proc = run_guest_check(tmp_path, **kw)
    assert proc.returncode == 1 and proc.stderr.startswith("tour:")


def test_a_refusal_names_what_sudo_said(tmp_path):
    proc = run_guest_check(tmp_path, sudo_out="sudo: Account or password is expired, reset your password and try again")
    assert proc.returncode == 1 and "Account or password is expired" in proc.stderr


def test_the_bootstrap_clears_a_forced_root_password_change_before_any_sudo():
    text = (pathlib.Path(userdata.__file__).parent / "cloud-init.yaml").read_text()
    assert text.index("passwd -l root") < text.index('chage -d "$(date +%F)" root') < text.index("@@BOOT_TOUR@@")


def test_the_tour_filter_stays_off_until_its_own_stage_arms_it():
    """ssh pulls the filter unit in (Requires=). Loaded during the package stage it would cut apt and the system resolver
    (neither is root), so the unit is skipped until the tour-firewall stage writes the marker, and armed on every boot after."""
    extras = guest.userdata_extras([], RESOLVERS)
    unit = file_block(extras.files, "/etc/systemd/system/motel-tour-firewall.service")
    assert f"ConditionPathExists={egress.TOUR_ARMED}" in unit
    assert f"- path: {egress.TOUR_ARMED}" not in extras.files                  # never written before the stage
    boot = extras.boot
    assert boot.index("stage tour-firewall") < boot.index(egress.TOUR_ARMED) < boot.index("systemctl enable --now motel-tour-firewall")


def test_the_check_sits_between_creating_the_account_and_using_it():
    boot = guest.userdata_extras([], RESOLVERS).boot
    assert boot.index("useradd") < boot.index("sudo -n -l -U guest") < boot.index("install -d -o root -g guest")


# ---------------------------------------------------------------------------------------------- budgets (L2, L3)

def many_hosts(budget):
    """Distinct valid names that together cost as close to ``budget`` as possible."""
    hosts, spent, i = [], 0, 0
    while len(hosts) < egress.MAX_EGRESS_HOSTS:
        name = f"service-{i:02d}." + "a" * 40 + ".example.com"
        if spent + len(name) + egress.EGRESS_HOST_OVERHEAD > budget:
            break
        hosts.append(name)
        spent += len(name) + egress.EGRESS_HOST_OVERHEAD
        i += 1
    return hosts


@pytest.mark.parametrize("kw", [{"concierge_port": 17781}, {"own_login": True}, {}])
def test_a_full_host_budget_keeps_the_user_data_at_or_below_the_target(kw):
    hosts = many_hosts(egress.MAX_EGRESS_BYTES - (egress.egress_cost(egress.OWN_LOGIN_HOSTS) if kw.get("own_login") else 0))
    assert egress.egress_cost(hosts) <= egress.MAX_EGRESS_BYTES
    assert len(hosts) > 20
    text = render(guest.userdata_extras(hosts, RESOLVERS, **kw))
    assert len(text.encode()) <= egress.USER_DATA_TARGET, len(text.encode())


def test_the_longest_names_fit_only_as_many_as_the_budget_allows():
    name = ".".join(["a" * 60] * 3) + "." + "b" * 59 + ".com"
    assert len(name) <= 253
    most = egress.MAX_EGRESS_BYTES // (len(name) + egress.EGRESS_HOST_OVERHEAD)
    hosts = [f"{i:02d}{name[2:]}" for i in range(most)]
    text = render(guest.userdata_extras(hosts, RESOLVERS, concierge_port=17781))
    assert len(text.encode()) <= egress.USER_DATA_TARGET
    with pytest.raises(Fail, match="too long in all"):
        guest.userdata_extras(hosts + [f"{most:02d}{name[2:]}"], RESOLVERS, concierge_port=17781)


def test_the_engine_can_check_a_list_before_it_calls_userdata_extras():
    from motel.tours import guest as package
    assert package.MAX_EGRESS_HOSTS == 64 and package.MAX_EGRESS_BYTES == egress.MAX_EGRESS_BYTES
    assert package.egress_cost(["a.example.com"]) == len("a.example.com") + package.EGRESS_HOST_OVERHEAD


def test_an_own_login_guest_may_have_63_hosts_besides_api_anthropic_com():
    hosts = [f"h{i:02d}.example.com" for i in range(63)]
    extras = guest.userdata_extras(hosts, RESOLVERS, own_login=True)
    conf = json.loads(file_block(extras.files.replace("  - path", "- path"), "/etc/motel-cabin/tour.json"))
    assert len(conf["egress_hosts"]) == 64 and "api.anthropic.com" in conf["egress_hosts"]
    with pytest.raises(Fail, match="at most 64"):
        guest.userdata_extras(hosts + ["h63.example.com"], RESOLVERS, own_login=True)
    # listing api.anthropic.com yourself does not count twice
    assert guest.userdata_extras(hosts + ["api.anthropic.com"], RESOLVERS, own_login=True)
    # without own-login the limit is 64 requested hosts
    assert guest.userdata_extras(hosts + ["h63.example.com"], RESOLVERS)


# ---------------------------------------------------------------------------------------------- shipped refresher

def test_the_refresher_ships_gzipped_and_decodes_to_the_script():
    import base64
    import gzip
    text = render(guest.userdata_extras(["git.example.org"], RESOLVERS))
    match = re.search(r"- path: /usr/local/sbin/motel-tour-egress\n    permissions: '0755'\n    encoding: gz\+b64\n    content: (\S+)\n", text)
    assert match
    assert gzip.decompress(base64.b64decode(match.group(1))).decode() == egress.REFRESHER
    assert text == render(guest.userdata_extras(["git.example.org"], RESOLVERS))        # reproducible: no timestamp inside
    yaml = pytest.importorskip("yaml")
    by_path = {item["path"]: item for item in yaml.safe_load(text)["write_files"]}
    assert by_path["/usr/local/sbin/motel-tour-egress"]["encoding"] == "gz+b64"


def test_the_egress_unit_runs_again_at_boot_and_the_boot_enables_it():
    text = render(guest.userdata_extras([], RESOLVERS))
    assert "[Install]\nWantedBy=multi-user.target" in file_block(text, "/etc/systemd/system/motel-tour-egress.service")
    assert "systemctl enable motel-tour-egress.service" in text


# ---------------------------------------------------------------------------------------------- DNS client (R-M1, R-M2)

import socket  # noqa: E402
import struct  # noqa: E402
import threading  # noqa: E402


def dns_name(name):
    return b"".join(bytes([len(x)]) + x.encode() for x in name.rstrip(".").split(".")) + b"\0"


def dns_reply(query, answers=(), *, rcode=0, tc=False, txid=None, question=None, qr=True, cname_to=None):
    """A reply to ``query`` (bytes) with A records for ``answers``; knobs make it wrong in one way."""
    rid = struct.unpack(">H", query[:2])[0] if txid is None else txid
    flags = (0x8000 if qr else 0) | 0x0100 | 0x0080 | (0x0200 if tc else 0) | rcode
    qsec = query[12:] if question is None else dns_name(question) + struct.pack(">HH", 1, 1)
    body = b""
    owner = b"\xc0\x0c"
    if cname_to:
        target = dns_name(cname_to)
        body += owner + struct.pack(">HHIH", 5, 1, 60, len(target)) + target
        owner = target
    for ip in answers:
        body += owner + struct.pack(">HHIH", 1, 1, 60, 4) + socket.inet_aton(ip)
    count = len(answers) + (1 if cname_to else 0)
    return struct.pack(">HHHHHH", rid, flags, 1, count, 0, 0) + qsec + body


class FakeDns:
    """UDP and TCP DNS on the loopback, one port. ``script(query, via)`` returns a list of replies to send, in order."""

    def __init__(self, script, tcp_conn=None):
        self.script, self.seen, self.tcp_conn = script, [], tcp_conn
        for _ in range(50):
            self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.udp.bind(("127.0.0.1", 0))
            self.port = self.udp.getsockname()[1]
            self.tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                self.tcp.bind(("127.0.0.1", self.port))
                break
            except OSError:
                self.udp.close()
                self.tcp.close()
        self.tcp.listen(4)
        self.stop = False
        self.threads = [threading.Thread(target=self.serve_udp, daemon=True), threading.Thread(target=self.serve_tcp, daemon=True)]
        for thread in self.threads:
            thread.start()

    def serve_udp(self):
        self.udp.settimeout(0.2)
        while not self.stop:
            try:
                data, peer = self.udp.recvfrom(4096)
            except OSError:
                continue
            self.seen.append(("udp", data))
            for reply in self.script(data, "udp"):
                self.udp.sendto(reply, peer)

    def serve_tcp(self):
        self.tcp.settimeout(0.2)
        while not self.stop:
            try:
                conn, _ = self.tcp.accept()
            except OSError:
                continue
            with conn:
                conn.settimeout(2)
                if self.tcp_conn is not None:
                    try:
                        self.tcp_conn(conn)
                    except OSError:
                        pass
                    continue
                try:
                    size = struct.unpack(">H", conn.recv(2))[0]
                    data = b""
                    while len(data) < size:
                        data += conn.recv(size - len(data))
                except (OSError, struct.error):
                    continue
                self.seen.append(("tcp", data))
                for reply in self.script(data, "tcp"):
                    conn.sendall(struct.pack(">H", len(reply)) + reply)

    def close(self):
        self.stop = True
        for thread in self.threads:
            thread.join(2)
        self.udp.close()
        self.tcp.close()


@pytest.fixture
def dns():
    made = []

    def start(script, tcp_conn=None):
        server = FakeDns(script, tcp_conn)
        made.append(server)
        return server
    yield start
    for server in made:
        server.close()


@pytest.fixture(scope="module")
def client():
    ns = {"__name__": "refresher"}
    exec(compile(egress.REFRESHER, "motel-tour-egress", "exec"), ns)
    return ns


def test_the_client_asks_for_an_a_record_with_a_random_id_and_returns_the_addresses(client, dns):
    server = dns(lambda q, via: [dns_reply(q, ["93.184.216.34", "1.1.1.1"])])
    assert client["query_a"]("Git.Example.org.", "127.0.0.1", server.port, 2) == ["1.1.1.1", "93.184.216.34"]
    via, query = server.seen[0]
    ident, flags, qd, an = struct.unpack(">HHHH", query[:8])
    assert via == "udp" and flags == 0x0100 and qd == 1 and an == 0
    assert query[12:].lower() == dns_name("git.example.org") + struct.pack(">HH", 1, 1)              # type A, class IN
    ids = set()
    for _ in range(12):
        client["query_a"]("git.example.org", "127.0.0.1", server.port, 2)
        ids.add(struct.unpack(">H", server.seen[-1][1][:2])[0])
    assert len(ids) > 3                                                                      # not a fixed or counting id


def test_the_client_uses_a_fresh_source_port_each_time(client, dns, monkeypatch):
    ports = []
    real = socket.socket

    class Spy(real):
        def connect(self, addr):
            super().connect(addr)
            if self.type == socket.SOCK_DGRAM:
                ports.append(self.getsockname()[1])
    server = dns(lambda q, via: [dns_reply(q, ["1.1.1.1"])])
    monkeypatch.setattr(socket, "socket", Spy)
    for _ in range(6):
        client["query_a"]("a.example.com", "127.0.0.1", server.port, 2)
    assert len(set(ports)) > 1


def test_a_reply_with_the_wrong_id_is_ignored_and_the_real_one_is_used(client, dns):
    server = dns(lambda q, via: [dns_reply(q, ["203.0.113.66"], txid=(struct.unpack(">H", q[:2])[0] + 1) % 65536),
                                 dns_reply(q, ["1.1.1.1"])])
    assert client["query_a"]("a.example.com", "127.0.0.1", server.port, 2) == ["1.1.1.1"]


@pytest.mark.parametrize("bad", [{"question": "evil.example.net"}, {"qr": False}])
def test_a_reply_to_another_question_or_that_is_not_a_response_is_ignored(client, dns, bad):
    server = dns(lambda q, via: [dns_reply(q, ["203.0.113.66"], **bad), dns_reply(q, ["1.1.1.1"])])
    assert client["query_a"]("a.example.com", "127.0.0.1", server.port, 2) == ["1.1.1.1"]


def test_only_wrong_replies_end_in_a_timeout(client, dns):
    server = dns(lambda q, via: [dns_reply(q, ["203.0.113.66"], txid=(struct.unpack(">H", q[:2])[0] + 1) % 65536)])
    started = time.monotonic()
    with pytest.raises(OSError, match="timeout"):
        client["query_a"]("a.example.com", "127.0.0.1", server.port, 0.6)
    assert time.monotonic() - started < 3


def test_a_silent_server_times_out(client, dns):
    server = dns(lambda q, via: [])
    with pytest.raises(OSError, match="timeout"):
        client["query_a"]("a.example.com", "127.0.0.1", server.port, 0.4)


def test_a_truncated_answer_is_asked_again_over_tcp(client, dns):
    def script(query, via):
        return [dns_reply(query, ["1.1.1.1"], tc=True)] if via == "udp" else [dns_reply(query, ["1.1.1.1", "8.8.8.8", "9.9.9.9"])]
    server = dns(script)
    assert client["query_a"]("a.example.com", "127.0.0.1", server.port, 2) == ["1.1.1.1", "8.8.8.8", "9.9.9.9"]
    assert [via for via, _ in server.seen] == ["udp", "tcp"]
    assert server.seen[0][1][:2] == server.seen[1][1][:2]                       # the same question, the same id


def test_a_tcp_answer_to_another_question_is_refused(client, dns):
    def script(query, via):
        return [dns_reply(query, [], tc=True)] if via == "udp" else [dns_reply(query, ["203.0.113.66"], question="evil.example.net")]
    server = dns(script)
    with pytest.raises(OSError, match="does not match"):
        client["query_a"]("a.example.com", "127.0.0.1", server.port, 2)


def test_a_cname_chain_is_followed_and_unrelated_records_are_not_believed(client, dns):
    server = dns(lambda q, via: [dns_reply(q, ["93.184.216.34"], cname_to="edge.cdn.example.net")])
    assert client["query_a"]("www.example.org", "127.0.0.1", server.port, 2) == ["93.184.216.34"]
    # a record for a name the question does not lead to is dropped
    evil = struct.pack(">HHIH", 1, 1, 60, 4) + socket.inet_aton("203.0.113.66")

    def script(query, via):
        good = dns_reply(query, ["1.1.1.1"])
        stray = dns_name("evil.example.net") + evil
        head = struct.unpack(">HHHHHH", good[:12])
        return [struct.pack(">HHHHHH", head[0], head[1], 1, 2, 0, 0) + good[12:] + stray]
    server2 = dns(script)
    assert client["query_a"]("a.example.com", "127.0.0.1", server2.port, 2) == ["1.1.1.1"]


@pytest.mark.parametrize("rcode", [2, 3, 5])
def test_a_server_error_or_nxdomain_is_a_failure(client, dns, rcode):
    server = dns(lambda q, via: [dns_reply(q, [], rcode=rcode)])
    with pytest.raises(OSError, match="dns error"):
        client["query_a"]("a.example.com", "127.0.0.1", server.port, 2)


@pytest.mark.parametrize("junk", [b"", b"\x00" * 5, b"\xff" * 40])
def test_garbage_replies_do_not_crash_the_client(client, dns, junk):
    server = dns(lambda q, via: [junk])
    with pytest.raises(OSError, match="timeout"):
        client["query_a"]("a.example.com", "127.0.0.1", server.port, 0.4)


def test_a_compression_loop_in_a_reply_is_survived(client):
    query = client["build_query"]("a.example.com", 77)
    loop = struct.pack(">HHHHHH", 77, 0x8180, 1, 1, 0, 0) + query[12:] + b"\xc0\x1c" + struct.pack(">HHIH", 1, 1, 60, 4) + b"\1\1\1\1"
    assert client["parse_answer"](loop[:12] + b"\xc0\x0c" + loop[14:], "a.example.com", 77) is None    # pointer to itself


def test_lookup_tries_the_next_resolver_and_gives_up_when_there_is_none(client, dns, monkeypatch):
    silent = dns(lambda q, via: [])
    live = dns(lambda q, via: [dns_reply(q, ["1.1.1.1"])])
    real = client["query_a"]
    monkeypatch.setitem(client, "query_a", lambda name, server, port=53, timeout=3.0: real(
        name, "127.0.0.1", silent.port if server == "a" else live.port, 0.3))
    assert client["lookup"]("x.example.com", ["a", "b"]) == ["1.1.1.1"]
    with pytest.raises(OSError):
        client["lookup"]("x.example.com", [])
    with pytest.raises(OSError):
        client["lookup"]("x.example.com", ["a"])


def test_a_run_stops_asking_after_its_time_budget_and_leaves_the_rest_for_the_next_run(refresher):
    refresher.answers = {"a.example.com": ["1.1.1.1"], "b.example.com": ["8.8.8.8"]}
    refresher.ns["BUDGET_S"] = -1
    assert refresher.main() == 1
    assert refresher.doc()["failed_hosts"] == ["a.example.com", "b.example.com"] and not getattr(refresher, "asked", [])


def test_the_refresher_goes_to_the_configured_resolvers_and_not_to_the_system_resolver(refresher):
    refresher.answers = {"a.example.com": ["1.1.1.1"], "b.example.com": ["8.8.8.8"]}
    refresher.main()
    assert refresher.asked and all(resolvers == RESOLVERS for _, resolvers in refresher.asked)
    src = egress.REFRESHER
    assert "getaddrinfo" not in src and "gethostbyname" not in src            # nothing goes through NSS or /etc/hosts


# ---------------------------------------------------------------------------------------------- systemd-resolved is replaced

def test_the_boot_masks_systemd_resolved_and_writes_a_plain_resolv_conf(tmp_path):
    script = egress.boot_resolver(["185.12.64.2", "185.12.64.1"])
    assert "systemctl mask systemd-resolved.service" in script and "systemctl stop systemd-resolved.service" in script
    target = tmp_path / "resolv.conf"
    target.symlink_to("/run/systemd/resolve/stub-resolv.conf")                 # what Ubuntu ships
    bin_dir = tmp_path / "fakebin3"
    bin_dir.mkdir()
    (bin_dir / "systemctl").write_text("#!/bin/sh\necho \"$@\" >> \"$LOG\"\n")
    (bin_dir / "systemctl").chmod(0o755)
    log = tmp_path / "systemctl.log"
    run = subprocess.run(["bash", "-c", "set -euo pipefail\nstage() { :; }\n" + script.replace("/etc/resolv.conf", str(target))],
                         capture_output=True, text=True, check=False, env={"PATH": f"{bin_dir}:/usr/bin:/bin", "LOG": str(log)})
    assert run.returncode == 0, run.stderr
    assert not target.is_symlink() and target.read_text() == (
        "nameserver 185.12.64.1\nnameserver 185.12.64.2\noptions timeout:2 attempts:2\n")
    assert (target.stat().st_mode & 0o777) == 0o644
    assert log.read_text().splitlines() == ["stop systemd-resolved.service", "mask systemd-resolved.service"]


def test_at_most_three_resolvers_go_into_resolv_conf():
    script = egress.boot_resolver(["185.12.64.1", "185.12.64.2", "198.51.100.3", "198.51.100.4"])
    assert script.count("nameserver") == 3 and "198.51.100.4" not in script


def test_the_resolver_replacement_runs_before_the_firewall_and_the_first_lookup():
    boot = guest.userdata_extras(["git.example.org"], RESOLVERS).boot
    assert boot.index("systemctl mask systemd-resolved.service") < boot.index("motel-tour-firewall.service") \
        < boot.index("/usr/local/sbin/motel-tour-egress")


def test_l3_a_server_that_drips_bytes_cannot_hold_the_tcp_retry_past_the_deadline(client, dns):
    def drip(conn):
        conn.recv(4096)
        conn.sendall(struct.pack(">H", 400))                     # a long answer is coming ...
        for _ in range(200):                                     # ... one byte every 0.15 s: 30 s in all
            conn.sendall(b"\x00")
            time.sleep(0.15)

    server = dns(lambda q, via: [dns_reply(q, [], tc=True)], drip)
    started = time.monotonic()
    with pytest.raises(OSError, match="timeout"):
        client["query_a"]("a.example.com", "127.0.0.1", server.port, 0.5)
    assert time.monotonic() - started < 4


def test_l5_names_are_compared_as_labels_not_as_dotted_strings(client):
    txid = 4242
    good = struct.pack(">HHHHHH", txid, 0x8180, 1, 1, 0, 0)

    def one_label(text):                                       # a single label that contains literal dots
        raw = text.encode()
        return bytes([len(raw)]) + raw + b"\0"
    ip = struct.pack(">HHIH", 1, 1, 60, 4) + socket.inet_aton("203.0.113.9")
    question = struct.pack(">HH", 1, 1)
    # the question is ONE label "a.example.com", not the three labels the query asked for
    forged = good + one_label("a.example.com") + question + b"\xc0\x0c" + ip
    assert client["parse_answer"](forged, "a.example.com", txid) is None
    # the honest packet still works
    honest = good + dns_name("a.example.com") + question + b"\xc0\x0c" + ip
    assert client["parse_answer"](honest, "a.example.com", txid) == (["203.0.113.9"], False)
    # a CNAME target that is one dotted label does not extend the chain to the three-label name
    target = one_label("edge.example.net")
    cname = b"\xc0\x0c" + struct.pack(">HHIH", 5, 1, 60, len(target)) + target
    stray = dns_name("edge.example.net") + ip
    chained = struct.pack(">HHHHHH", txid, 0x8180, 1, 2, 0, 0) + dns_name("a.example.com") + question + cname + stray
    assert client["parse_answer"](chained, "a.example.com", txid) == ([], False)
