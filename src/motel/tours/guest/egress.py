"""Egress profile and user setup for tour guests, rendered as text for the cabin's cloud-init.

A tour guest runs an agent that executes untrusted work, so on top of the cabin's own filter it gets a second,
separate nftables table, ``inet cabin_tour``, whose output chain has policy DROP. Only these leave the machine:

* replies on established and related connections (so the controller's inbound ssh keeps working);
* loopback (the concierge is reached through the controller's ssh reverse forward, which is loopback on the guest);
* DNS (UDP and TCP port 53) to the configured resolvers, and only from root;
* DHCP renewal (UDP 68 to 67) to the broadcast address and the DHCP server (the default gateway) only;
* TCP to the tour's allowlisted hosts, resolved at boot and refreshed for a bounded time.

The cabin's own table (``inet cabin``) is untouched: it still drops the provider metadata service and still owns the
input chain, so inbound ssh stays limited to the controller's exact address. Because the tour table is separate, the
cabin's integrity check (which compares ``inet cabin`` with a snapshot) is unaffected. A packet must pass both tables.

Name resolution happens in ``motel-tour-egress`` (installed to ``/usr/local/sbin``, runs as root): it resolves every
allowlisted host to IPv4 addresses, drops any that are not globally routable (so a poisoned or rebinding answer cannot
open a private or metadata address), ADDS them to the ``allow4`` set and writes the same host-to-address pairs into a
managed block of ``/etc/hosts``. It runs at boot, then from a timer every ``refresh_s`` seconds until
``refresh_window_s`` has passed since its first run, and then disables the timer. Addresses are never removed during
the window; the set is capped. After a reboot it puts the remembered addresses back into the (then empty) set.

DNS is not available to the guest user. Port 53 is open only to root. On a tour guest ``systemd-resolved`` is stopped and
masked and ``/etc/resolv.conf`` is a plain file with the configured resolvers (for root's own lookups; apt does not work
in the tour state: its ``_apt`` user is not root and the package mirrors are not allowlisted, so packages are installed
before the filter loads, by the bootstrap); the
refresher resolves with its own small DNS client (UDP, TCP when truncated, random id and source port, replies checked
against the question) and never through ``/etc/hosts`` or NSS. The guest resolves the allowlisted names through
``/etc/hosts`` and nothing else, so it has no DNS channel. Limits worth knowing: a tool that does its own DNS (a static
binary with a built-in resolver, DNS over HTTPS) cannot resolve anything and fails; a name that is not allowlisted fails
after the libc timeout; the allowlist is by IP address, so every site that shares an address with an allowlisted host (a
CDN) is reachable too; an allowlisted host can itself carry data out; ports default to 443 only; IPv6 is disabled on
cabins; hosts are exact names, no wildcards.

Boot order: the rules the firewall unit loads (``TOUR_NFT``) are written only by the unit's own ``ExecStartPre=``
(``TOUR_NFT_RENDER``, root): it fills the template's one token, checks the result with ``nft -c`` and moves it in place.
Nothing else loads that file, and ssh ``Requires=`` the unit, so no start order can run the unit before its rules exist.

Also created for tour guests: the unprivileged user ``guest`` (locked password, no sudo, no other group; an existing
``guest`` with more than that stops the boot) and ``/var/lib/motel-guest/in``, root-owned, group ``guest``, mode 0750,
where the controller puts the card, the git bundle and the overlay. Written for the runner:
``/usr/local/lib/motel-tour/`` (root-owned; the controller copies ``runner.py`` and ``adapters.py`` there, because
together they exceed the user-data size limit).

Reverse forward for the concierge (``concierge_port``): an sshd drop-in allows remote forwarding for the cabin
account to ``127.0.0.1:<port>`` only, and the cabin key gains ``port-forwarding`` and ``permitlisten`` options.
This changes what the cabin's own readiness probe sees (forwarding is no longer fully disabled, the key line is
longer); see ``/etc/motel-cabin/tour.json``, which records the profile so the probe can accept exactly this and nothing
broader.

User data has a size limit (32 KiB at the provider). The host names take part of it, so ``userdata_extras`` accepts at
most ``MAX_EGRESS_HOSTS`` hosts and ``MAX_EGRESS_BYTES`` of :func:`egress_cost`; with the budget full, the rendered user
data stays at or below ``USER_DATA_TARGET`` (31,744 bytes, 1 KiB under the limit) for the typical cabin configuration:
two resolvers, two source addresses, the default packages. The 1 KiB left over is the room for longer package and resolver
lists. The engine's spec validation imports the same numbers.
"""

from __future__ import annotations

import base64
import gzip
import ipaddress
import json
import re
import textwrap

from motel.cabins.errors import Fail
from motel.cabins.userdata import GuestExtras

GUEST_USER = "guest"
INPUT_DIR = "/var/lib/motel-guest/in"
RUNNER_DIR = "/usr/local/lib/motel-tour"
TOUR_CONF = "/etc/motel-cabin/tour.json"
TOUR_NFT = "/etc/motel-cabin/tour.nft"                  # the finished rules, written by TOUR_NFT_RENDER on every start
TOUR_NFT_TEMPLATE = "/etc/motel-cabin/tour.nft.in"      # the rules with their two boot-time tokens
# Present once the bootstrap reaches its tour-firewall stage. Until then the filter unit is skipped: ssh pulls it in
# (Requires=), and loaded during the package stage it would cut apt and the system resolver, which are not root.
TOUR_ARMED = "/etc/motel-cabin/tour-armed"
TOUR_NFT_RENDER = "/usr/local/sbin/motel-tour-nft"
DHCP_STATE = "/var/lib/motel-guest/dhcp-server"
TOUR_READY = "/var/lib/motel-guest/tour-ready.json"
MAX_HOSTS = MAX_EGRESS_HOSTS = 64            # in all; an own-login guest's api.anthropic.com counts as one of them
EGRESS_HOST_OVERHEAD = 4                     # bytes a host costs in the user data besides its name (quotes, comma, slack)
MAX_EGRESS_BYTES = 1700                      # budget for sum(len(host) + EGRESS_HOST_OVERHEAD); see the module docstring
USER_DATA_TARGET = 31_744                    # the rendered user data stays at or below this: the provider limit minus 1 KiB
DHCP_DEST_TOKEN = "@DHCP_DEST@"              # replaced at boot: the broadcast address, plus the default gateway if known
DEFAULT_PORTS = (443,)
DEFAULT_REFRESH_S = 300
DEFAULT_WINDOW_S = 6 * 3600
MAX_SET_ELEMENTS = 512
# Hosts an own-login guest needs and nothing else. Source: Claude Code docs, "Enterprise network configuration",
# section "Network access requirements": api.anthropic.com carries the Claude API requests. claude.ai,
# platform.claude.com and sso.claude.com are for interactive sign-in and OAuth refresh; a token from
# `claude setup-token` is used as is for the whole session ("Generate a long-lived token" in the authentication docs),
# and the CLI is run with the traffic-cutting variables in adapters.py, so none of those are needed.
OWN_LOGIN_HOSTS = ("api.anthropic.com",)

HOST_RE = re.compile(r"^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]$")


def normalize_hosts(hosts) -> tuple:
    """Lowercase exact DNS names or globally routable IPv4 literals; sorted, unique. Anything else is refused."""
    out = set()
    for host in hosts or ():
        if not isinstance(host, str):
            raise Fail("egress hosts must be strings")
        name = host.strip().lower().rstrip(".")
        try:
            addr = ipaddress.IPv4Address(name)
        except ValueError:
            if not HOST_RE.fullmatch(name) or name.endswith((".local", ".internal", ".localhost")):
                raise Fail(f"invalid egress host {host!r}: use an exact DNS name or IPv4 address (no wildcards)") from None
            out.add(name)
            continue
        if not addr.is_global:
            raise Fail(f"egress host {host!r} is not a public address")
        out.add(str(addr))
    if len(out) > MAX_HOSTS:
        raise Fail(f"at most {MAX_HOSTS} egress hosts")
    return tuple(sorted(out))


def egress_cost(hosts) -> int:
    """Bytes the host names take in the user data (used against ``MAX_EGRESS_BYTES``)."""
    return sum(len(host) + EGRESS_HOST_OVERHEAD for host in hosts)


def normalize_resolvers(resolvers) -> tuple:
    out = []
    for item in resolvers or ():
        try:
            addr = ipaddress.IPv4Address(item)
        except (ValueError, TypeError):
            raise Fail("invalid DNS resolver") from None
        if addr.is_loopback or addr.is_link_local or addr.is_multicast or addr.is_unspecified:
            raise Fail("a DNS resolver cannot be a loopback, link-local or multicast address")
        out.append(str(addr))
    if not out:
        raise Fail("at least one DNS resolver is required")
    return tuple(sorted(set(out)))


def render_nft(resolvers, ports=DEFAULT_PORTS) -> str:
    """The tour table. The ``allow4`` set starts empty; ``motel-tour-egress`` fills it. One token is filled in at boot
    (:data:`DHCP_DEST_TOKEN`) because the DHCP server depends on the network."""
    resolvers = normalize_resolvers(resolvers)
    ports = tuple(sorted({int(p) for p in ports}))
    if not ports or not all(1 <= p <= 65535 for p in ports):
        raise Fail("invalid egress ports")
    dns = ", ".join(resolvers)
    port_set = ", ".join(str(p) for p in ports)
    return textwrap.dedent(f"""\
        table inet cabin_tour {{
          set allow4 {{
            type ipv4_addr
            size {MAX_SET_ELEMENTS}
          }}
          chain output {{
            type filter hook output priority 0; policy drop;
            ct state established,related accept
            ip daddr 169.254.169.254 drop
            udp dport 53 meta skuid != 0 drop
            tcp dport 53 meta skuid != 0 drop
            oif "lo" accept
            ip daddr {{ {dns} }} udp dport 53 accept
            ip daddr {{ {dns} }} tcp dport 53 accept
            ip daddr {{ {DHCP_DEST_TOKEN} }} udp sport 68 udp dport 67 accept
            ip daddr @allow4 tcp dport {{ {port_set} }} accept
          }}
        }}
        """)


# Runs as root on the guest (python3 is in the Ubuntu images). Python 3.10 floor; no f-strings (the text is filled in
# with .replace, not formatted).
REFRESHER = r"""#!/usr/bin/python3 -I
# Resolve the tour's allowlisted hosts into the nft set allow4 and the /etc/hosts block (motel.tours.guest.egress).
import ipaddress
import json
import os
import random
import socket
import struct
import subprocess
import sys
import time

CONF = "@CONF@"
STATE = "/var/lib/motel-guest/tour-egress.json"
HOSTS = "/etc/hosts"
BEGIN = "# BEGIN motel-tour"
END = "# END motel-tour"
NFT = "/usr/sbin/nft"


def load(path, default):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def set_is_empty():
    proc = subprocess.run([NFT, "list", "set", "inet", "cabin_tour", "allow4"], capture_output=True, check=False)
    return proc.returncode != 0 or b"elements" not in (proc.stdout or b"")


def add_to_set(addresses):
    script = "add element inet cabin_tour allow4 { " + ", ".join(addresses) + " }\n"
    return subprocess.run([NFT, "-f", "-"], input=script.encode(), check=False).returncode == 0


# A small DNS client for root: A queries straight to the configured resolvers (never /etc/hosts or NSS), a random
# transaction id and source port, UDP first and TCP when the answer is truncated, and every answer checked against
# the question before a single address is believed.
DNS_TIMEOUT = 3.0
BUDGET_S = 120          # one run stops asking after this long, whatever is left is tried again by the next run
DNS_MAX_CNAME = 8


def encode_name(name):
    out = b""
    for label in name.rstrip(".").split("."):
        raw = label.encode("ascii")
        if not 0 < len(raw) < 64:
            raise ValueError("bad label")
        out += bytes([len(raw)]) + raw
    return out + b"\0"


def read_name(data, pos):
    '''(tuple of lowercase labels, position after it); follows compression pointers, bounded.'''
    labels, jumped, end, hops = [], False, pos, 0
    while True:
        if pos >= len(data):
            raise ValueError("short name")
        size = data[pos]
        if size & 0xC0 == 0xC0:
            if pos + 1 >= len(data):
                raise ValueError("short pointer")
            if not jumped:
                end = pos + 2
            pos = ((size & 0x3F) << 8) | data[pos + 1]
            jumped = True
            hops += 1
            if hops > 32:
                raise ValueError("pointer loop")
            continue
        if size & 0xC0:
            raise ValueError("bad label type")
        pos += 1
        if size == 0:
            return tuple(labels), (end if jumped else pos)
        if pos + size > len(data):
            raise ValueError("short label")
        labels.append(data[pos:pos + size].decode("ascii", "replace").lower())
        pos += size


def build_query(name, txid):
    return struct.pack(">HHHHHH", txid, 0x0100, 1, 0, 0, 0) + encode_name(name) + struct.pack(">HH", 1, 1)


def parse_answer(data, name, txid):
    '''The IPv4 addresses a reply gives for ``name`` (following its CNAME chain), or None when the reply is not an
    answer to exactly this question (wrong id, not a response, another question). Raises OSError for a server error.'''
    if len(data) < 12:
        return None
    rid, flags, qd, an, _ns, _ar = struct.unpack(">HHHHHH", data[:12])
    if rid != txid or not flags & 0x8000 or qd != 1:
        return None
    try:
        qname, pos = read_name(data, 12)
        if pos + 4 > len(data) or struct.unpack(">HH", data[pos:pos + 4]) != (1, 1) or qname != tuple(name.rstrip(".").lower().split(".")):
            return None
        pos += 4
        records = []
        for _ in range(an):
            owner, pos = read_name(data, pos)
            if pos + 10 > len(data):
                return None
            rtype, rclass, _ttl, rdlen = struct.unpack(">HHIH", data[pos:pos + 10])
            pos += 10
            if pos + rdlen > len(data):
                return None
            if rclass == 1 and rtype == 1 and rdlen == 4:
                records.append((owner, "A", socket.inet_ntoa(data[pos:pos + 4])))
            elif rclass == 1 and rtype == 5:
                records.append((owner, "CNAME", read_name(data, pos)[0]))
            pos += rdlen
    except ValueError:
        return None
    rcode = flags & 0x000F
    if rcode != 0:
        raise OSError("dns error %d" % rcode)
    wanted, found = set([qname]), set()
    for _ in range(DNS_MAX_CNAME + 1):
        grown = False
        for owner, kind, value in records:
            if owner in wanted:
                if kind == "CNAME" and value not in wanted:
                    wanted.add(value)
                    grown = True
                elif kind == "A" and value not in found:
                    found.add(value)
                    grown = True
        if not grown:
            break
    return sorted(found), bool(flags & 0x0200)


def query_a(name, server, port=53, timeout=DNS_TIMEOUT):
    '''IPv4 addresses of ``name`` from one resolver. OSError on timeout, refusal or a server error.'''
    txid = random.SystemRandom().randrange(65536)
    query = build_query(name, txid)
    deadline = time.monotonic() + timeout
    result = None
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((server, port))                 # a connected socket drops datagrams from anyone else
        sock.send(query)
        while result is None:
            left = deadline - time.monotonic()
            if left <= 0:
                raise OSError("dns timeout")
            sock.settimeout(left)
            try:
                reply = sock.recv(4096)
            except socket.timeout:
                raise OSError("dns timeout") from None
            result = parse_answer(reply, name, txid)     # a mismatch is ignored: keep waiting for the real answer
    finally:
        sock.close()
    if not result[1]:
        return result[0]
    deadline = max(deadline, time.monotonic() + 1.0)     # truncated: ask again over TCP, within one deadline
    with socket.create_connection((server, port), timeout=max(deadline - time.monotonic(), 0.1)) as tcp:
        tcp.settimeout(max(deadline - time.monotonic(), 0.1))
        tcp.sendall(struct.pack(">H", len(query)) + query)
        head = read_exactly(tcp, 2, deadline)
        reply = read_exactly(tcp, struct.unpack(">H", head)[0], deadline)
    result = parse_answer(reply, name, txid)
    if result is None:
        raise OSError("dns reply does not match the question")
    return result[0]


def read_exactly(sock, count, deadline):
    # the deadline is absolute: a server that sends a byte at a time cannot keep the run waiting past it
    data = b""
    while len(data) < count:
        left = deadline - time.monotonic()
        if left <= 0:
            raise OSError("dns timeout")
        sock.settimeout(left)
        try:
            chunk = sock.recv(count - len(data))
        except socket.timeout:
            raise OSError("dns timeout") from None
        if not chunk:
            raise OSError("dns connection closed")
        data += chunk
    return data


def lookup(host, resolvers):
    '''Addresses for ``host`` from the first resolver that answers; OSError when none does.'''
    last = OSError("no resolver")
    for server in resolvers:
        try:
            return query_a(host, server)
        except (OSError, ValueError) as exc:
            last = exc if isinstance(exc, OSError) else OSError(str(exc))
    raise last


def write_hosts(by_host):
    lines = [a + " " + h for h in sorted(by_host) for a in sorted(by_host[h])]
    block = BEGIN + "\n" + "".join(line + "\n" for line in lines) + END + "\n"
    try:
        with open(HOSTS, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        text = ""
    if not lines and BEGIN not in text:
        return
    if BEGIN in text and END in text.split(BEGIN, 1)[1]:
        head, rest = text.split(BEGIN, 1)
        tail = rest.split(END, 1)[1]
        new = head + block + (tail[1:] if tail.startswith("\n") else tail)
    else:
        new = text + ("" if not text or text.endswith("\n") else "\n") + block
    if new == text:
        return
    tmp = HOSTS + ".motel-tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(new)
    os.chmod(tmp, 0o644)
    os.replace(tmp, HOSTS)


def is_literal(host):
    try:
        ipaddress.IPv4Address(host)
        return True
    except ValueError:
        return False


def main():
    conf = load(CONF, {})
    hosts = conf.get("egress_hosts") or []
    resolvers = conf.get("resolvers") or []
    window = int(conf.get("refresh_window_s") or @WINDOW@)
    now = time.time()
    state = load(STATE, {})
    first = state.get("first_run") or now
    expired = now - first > window
    known = set(state.get("addresses") or [])
    by_host = dict((h, set(a)) for h, a in (state.get("by_host") or {}).items())
    wanted, resolved, failed = set(), {}, []
    started = time.monotonic()
    if not expired:
        for host in hosts:
            if time.monotonic() - started > BUDGET_S:
                failed.append(host)
                continue
            try:
                answers = [host] if is_literal(host) else lookup(host, resolvers)
            except OSError:
                failed.append(host)
                continue
            found = set(a for a in answers if ipaddress.ip_address(a).is_global)
            wanted |= found
            if not is_literal(host):
                resolved[host] = found
    new = wanted - known
    todo = set(new)
    if known and set_is_empty():
        todo |= known                       # after a reboot the set is empty but the state remembers the addresses
    if todo and len(known | set(new)) <= @CAP@ and add_to_set(sorted(todo)):
        known |= set(new)
    for host, found in resolved.items():
        by_host.setdefault(host, set()).update(found & known)
    write_hosts(dict((h, sorted(a)) for h, a in by_host.items() if a))
    doc = {"first_run": first, "last_run": now, "addresses": sorted(known), "failed_hosts": failed,
           "by_host": dict((h, sorted(a)) for h, a in by_host.items())}
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, sort_keys=True)
    os.replace(tmp, STATE)
    if expired:
        subprocess.run(["/usr/bin/systemctl", "disable", "--now", "motel-tour-egress.timer"], check=False)
        return 0
    return 0 if (known or not hosts) else 1


if __name__ == "__main__":
    sys.exit(main())
""".replace("@CONF@", TOUR_CONF).replace("@WINDOW@", str(DEFAULT_WINDOW_S)).replace("@CAP@", str(MAX_SET_ELEMENTS))
REFRESHER = "\n".join(line for line in REFRESHER.splitlines()
                      if line.strip() and not (line.lstrip().startswith("#") and not line.startswith("#!"))) + "\n"


# Boot snippets (bash, inserted into the cabin bootstrap, which runs with ``set -euo pipefail`` as root). A failing
# command stops the bootstrap, so the cabin never reports ready: the checks below fail closed.
GUEST_CHECK = f"""\
if [ "$(id -u {GUEST_USER})" = 0 ] || [ "$(id -gn {GUEST_USER})" != {GUEST_USER} ] || [ "$(id -Gn {GUEST_USER})" != {GUEST_USER} ]; then
  echo "tour: the {GUEST_USER} account is root or has a group besides its own" >&2
  exit 1
fi
sudo_out=$(sudo -n -l -U {GUEST_USER} 2>&1 || true)
case "$sudo_out" in
  *"is not allowed to run sudo"*) ;;
  *) echo "tour: the {GUEST_USER} account has sudo rights (or sudo cannot say): $(printf '%s' "$sudo_out" | tr -c '[:print:]' ' ' | cut -c1-160)" >&2
     exit 1 ;;
esac
"""

BOOT_GUEST_USER = f"""\
stage tour-user
id {GUEST_USER} >/dev/null 2>&1 || useradd --create-home --shell /bin/bash --user-group --comment 'motel tour guest' {GUEST_USER}
passwd -l {GUEST_USER} >/dev/null 2>&1 || true
""" + GUEST_CHECK + f"""\
guest_home=$(getent passwd {GUEST_USER} | cut -d: -f6)
chmod 0750 "$guest_home"
install -d -o root -g {GUEST_USER} -m 0750 {INPUT_DIR}
install -d -o root -g root -m 0755 {RUNNER_DIR}
"""

# Runs as ExecStartPre= of motel-tour-firewall.service, as root, on every start (first boot and every reboot): fills the
# token of the template, checks the result with `nft -c`, and only then replaces TOUR_NFT. If anything fails the unit
# fails, and ssh, which requires the unit, does not start. Nothing else ever loads TOUR_NFT. At early boot the default
# route does not exist yet, so the DHCP server (the gateway) is remembered from the last time it was known.
RENDER_SCRIPT = f"""\
#!/bin/bash
set -euo pipefail
gateway=$(ip -4 route show default 2>/dev/null | awk '$1 == "default" {{print $3; exit}}' || true)
if [[ "$gateway" =~ ^[0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+$ ]]; then
  printf '%s\\n' "$gateway" > {DHCP_STATE} || true
else
  gateway=$(cat {DHCP_STATE} 2>/dev/null || true)
fi
dhcp_dest=255.255.255.255
if [[ "$gateway" =~ ^[0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+$ ]]; then dhcp_dest="255.255.255.255, $gateway"; fi
rules=$(cat {TOUR_NFT_TEMPLATE})
rules=${{rules//{DHCP_DEST_TOKEN}/$dhcp_dest}}
case "$rules" in
  *{DHCP_DEST_TOKEN}*) echo "tour: a token of the rules is still unfilled" >&2; exit 1 ;;
esac
tmp={TOUR_NFT}.new
printf '%s\\n' "$rules" > "$tmp"
nft -c -f "$tmp"
chmod 0644 "$tmp"
mv -f "$tmp" {TOUR_NFT}
"""

def boot_resolver(resolvers) -> str:
    """Replace systemd-resolved on a tour guest by a plain /etc/resolv.conf that lists the configured resolvers: root's
    own lookups work, and the guest user, who may not reach port 53, has no resolver service to talk to. (apt does not
    work in the tour state: ``_apt`` is not root and the mirrors are not allowlisted. The bootstrap installs packages
    before this runs.)"""
    lines = "".join(f"nameserver {addr}\\n" for addr in normalize_resolvers(resolvers)[:3])   # glibc reads three
    return f"""\
stage tour-resolver
systemctl stop systemd-resolved.service || true
systemctl mask systemd-resolved.service
rm -f /etc/resolv.conf
printf '{lines}options timeout:2 attempts:2\\n' > /etc/resolv.conf
chmod 0644 /etc/resolv.conf
"""


BOOT_FIREWALL = f"""\
stage tour-firewall
install -o root -g root -m 0644 /dev/null {TOUR_ARMED}
systemctl daemon-reload
systemctl enable --now motel-tour-firewall.service
stage tour-egress
tour_ips=0
for _ in $(seq 1 20); do
  if /usr/local/sbin/motel-tour-egress; then break; fi
  sleep 3
done
tour_ips=$(python3 -I -c 'import json; print(len(json.load(open("/var/lib/motel-guest/tour-egress.json"))["addresses"]))' 2>/dev/null || echo 0)
systemctl enable motel-tour-egress.service
systemctl enable --now motel-tour-egress.timer
nft list table inet cabin_tour >/dev/null && tour_nft=loaded || tour_nft=missing
printf '{{"profile": "tour-guest", "tour_nft": "%s", "allowed_ips": %s, "guest_user": true}}\\n' "$tour_nft" "$tour_ips" > /var/lib/motel-guest/tour-ready.json
"""


def _block(text, spaces):
    pad = " " * spaces
    return "\n".join((pad + line) if line.strip() else line for line in text.rstrip("\n").splitlines())


def _file(path, mode, text):
    return f"  - path: {path}\n    permissions: '{mode}'\n    content: |\n{_block(text, 6)}\n"


def _file_gz(path, mode, text):
    """A file stored gzip+base64 (cloud-init ``encoding: gz+b64``): smaller user data. mtime 0 keeps it reproducible."""
    packed = base64.b64encode(gzip.compress(text.encode(), 9, mtime=0)).decode()
    return f"  - path: {path}\n    permissions: '{mode}'\n    encoding: gz+b64\n    content: {packed}\n"


def userdata_extras(egress_hosts, resolvers, *, concierge_port=None, own_login=False, ports=DEFAULT_PORTS,
                    refresh_s=DEFAULT_REFRESH_S, refresh_window_s=DEFAULT_WINDOW_S) -> GuestExtras:
    """The tour-guest profile for ``motel.cabins.userdata.render_user_data(..., extras=...)``.

    egress_hosts       exact DNS names (or public IPv4 literals) the guest may reach; may be empty.
    resolvers          the cabin's configured DNS resolvers (``cabins.dns_resolvers``): the only DNS allowed.
    concierge_port     TCP port of the controller's ssh reverse forward on the guest's loopback; opens that one
                       remote listen for the cabin account. None: no forward. Ignored when ``own_login`` is true.
    own_login          the guest uses the owner's own Claude login: the allowlist gains ``api.anthropic.com`` and
                       nothing else, and no reverse forward is configured.
    ports              TCP ports allowed to the allowlisted hosts (default 443).
    refresh_s          seconds between name re-resolutions; refresh_window_s: how long, from the first run, they go on.

    Returns a :class:`motel.cabins.userdata.GuestExtras`. Only valid for ``access = "ssh-allowlist"``.
    """
    hosts = list(normalize_hosts(egress_hosts))
    if own_login:
        hosts = sorted(set(hosts) | set(OWN_LOGIN_HOSTS))
        concierge_port = None
    if len(hosts) > MAX_EGRESS_HOSTS:
        raise Fail(f"at most {MAX_EGRESS_HOSTS} egress hosts in all"
                   + (" (an own-login guest also needs " + ", ".join(OWN_LOGIN_HOSTS) + ")" if own_login else ""))
    if egress_cost(hosts) > MAX_EGRESS_BYTES:
        raise Fail(f"the egress host names are too long in all ({egress_cost(hosts)} of {MAX_EGRESS_BYTES} bytes "
                   "of user data)")
    resolvers = normalize_resolvers(resolvers)
    if not isinstance(refresh_s, int) or isinstance(refresh_s, bool) or not 30 <= refresh_s <= 3600:
        raise Fail("refresh_s must be 30 to 3600 seconds")
    if (not isinstance(refresh_window_s, int) or isinstance(refresh_window_s, bool)
            or not refresh_s <= refresh_window_s <= 24 * 3600):
        raise Fail("refresh_window_s must be between refresh_s and 24 hours")
    if concierge_port is not None and (isinstance(concierge_port, bool) or not isinstance(concierge_port, int)
                                       or not 1024 <= concierge_port <= 65535):
        raise Fail("concierge_port must be an unprivileged TCP port")
    nft = render_nft(resolvers, ports)
    conf = {"profile": "tour-guest", "egress_hosts": hosts, "resolvers": list(resolvers),
            "ports": sorted({int(p) for p in ports}), "concierge_port": concierge_port, "own_login": bool(own_login),
            "refresh_s": refresh_s, "refresh_window_s": refresh_window_s}
    files = [
        _file(TOUR_CONF, "0644", json.dumps(conf, sort_keys=True, separators=(",", ":"))),
        _file_gz(TOUR_NFT_TEMPLATE, "0644", nft),
        _file_gz(TOUR_NFT_RENDER, "0755", RENDER_SCRIPT),
        _file_gz("/usr/local/sbin/motel-tour-egress", "0755", REFRESHER),
        _file_gz("/etc/systemd/system/motel-tour-firewall.service", "0644", textwrap.dedent(f"""\
            [Unit]
            Description=Restore the tour guest egress filter before guest access
            DefaultDependencies=no
            Before=network-pre.target ssh.service ssh.socket
            Wants=network-pre.target
            After=cabin-guest-firewall.service
            ConditionPathExists={TOUR_ARMED}
            [Service]
            Type=oneshot
            ExecStartPre={TOUR_NFT_RENDER}
            ExecStart=/usr/sbin/nft -f {TOUR_NFT}
            RemainAfterExit=yes
            [Install]
            WantedBy=multi-user.target
            """)),
        _file_gz("/etc/systemd/system/motel-tour-egress.service", "0644", textwrap.dedent("""\
            [Unit]
            Description=Resolve the tour egress allowlist into nftables
            After=network-online.target motel-tour-firewall.service
            Wants=network-online.target
            Requires=motel-tour-firewall.service
            [Service]
            Type=oneshot
            ExecStart=/usr/local/sbin/motel-tour-egress
            [Install]
            WantedBy=multi-user.target
            """)),
        _file_gz("/etc/systemd/system/motel-tour-egress.timer", "0644", textwrap.dedent(f"""\
            [Unit]
            Description=Refresh the tour egress allowlist for a bounded time
            [Timer]
            OnBootSec=30s
            OnUnitActiveSec={refresh_s}s
            AccuracySec=5s
            [Install]
            WantedBy=timers.target
            """)),
    ]
    # sshd must not start without the tour filter (fail closed), as it already must not start without the cabin one.
    drop_in = "[Unit]\nRequires=motel-tour-firewall.service\nAfter=motel-tour-firewall.service\n"
    files.append(_file("/etc/systemd/system/ssh.service.d/20-tour-firewall.conf", "0644", drop_in))
    files.append(_file("/etc/systemd/system/ssh.socket.d/20-tour-firewall.conf", "0644", drop_in))
    key_options = ""
    if concierge_port is not None:
        files.append(_file_gz("/etc/ssh/sshd_config.d/04-cabin-tour.conf", "0644", textwrap.dedent(f"""\
            # Tour guests: the controller's ssh reverse forward for the concierge, loopback and this one port only.
            # Listed before 10-cabin.conf on purpose: sshd keeps the first value it reads.
            Match User cabin
                AllowTcpForwarding remote
                PermitListen 127.0.0.1:{concierge_port}
            """)))
        key_options = f',port-forwarding,permitlisten="127.0.0.1:{concierge_port}"'
    boot = BOOT_GUEST_USER + boot_resolver(resolvers) + BOOT_FIREWALL
    return GuestExtras(files="".join(files), boot=boot, key_options=key_options)
