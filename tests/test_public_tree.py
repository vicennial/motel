"""Fails if the tree carries anything that should never be public.

The forbidden words are stored only as truncated SHA-256 digests, so this file does not leak them.
"""

import hashlib
import ipaddress
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOME_PATH = re.compile("/ho" + "me/|/Us" + "ers/|/ro" + "ot/[a-z]")
IPV4 = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\d.])")
ALLOWED_IP_PREFIXES = ("127.", "0.0.0.0", "169.254.169.254", "192.0.2.", "198.51.100.", "203.0.113.")
# Documented public infrastructure addresses (not anyone's host): Hetzner's recursive resolvers and private-network
# gateway, and the first address of the CGNAT range used in examples.
# Well-known public resolvers and example.com are also fine. Private, CGNAT, link-local and reserved addresses
# are never anyone's public host, so only globally routable addresses are checked.
ALLOWED_IPS = {"185.12.64.1", "185.12.64.2", "172.31.1.1", "100.64.0.1", "1.1.1.1", "8.8.8.8", "9.9.9.9", "93.184.216.34"}
CIDR = re.compile(r"/\d{1,2}\b")
FORBIDDEN_DIGESTS = {"8f64ae6a4c7aa205", "095ed417a95724d7", "8cfde6efdfc4ed5a", "0e1060689530435d",
                     "57d59e28722dfd55", "ab14d3faa25e917e", "4a65ededfc2238c9", "9ebd99776a8124c6", "89cf64a60286a118",
                     "c760b77c6170602e", "ce4a72852b849616"}
SKIP_SUFFIXES = {".png", ".jpg", ".gif", ".svg", ".pyc", ".whl", ".gz"}


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def tree_files():
    try:
        out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
        names = [n for n in out.splitlines() if n]
    except (OSError, subprocess.CalledProcessError):
        names = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts]
    for name in names:
        path = ROOT / name
        if path.is_file() and path.suffix not in SKIP_SUFFIXES:
            yield name, path.read_text(encoding="utf-8", errors="replace")


class PublicTreeTest(unittest.TestCase):
    def test_the_scan_sees_the_source(self):
        names = [n for n, _ in tree_files()]
        self.assertIn("src/motel/rooms.py", names)
        self.assertGreater(len(names), 30)

    def test_no_home_directory_paths(self):
        hits = [(n, m.group()) for n, text in tree_files() for m in HOME_PATH.finditer(text)]
        self.assertEqual(hits, [])

    def test_no_stray_ip_addresses(self):
        hits = []
        for name, text in tree_files():
            for match in IPV4.finditer(text):
                ip = match.group()
                octets = [int(g) for g in match.groups()]
                if max(octets) > 255 or ip in ALLOWED_IPS or any(ip.startswith(p) for p in ALLOWED_IP_PREFIXES):
                    continue
                addr = ipaddress.ip_address(ip)
                if not addr.is_global or addr.is_multicast:
                    continue
                if CIDR.match(text, match.end()):
                    continue  # a network range such as 10.0.0.0/8, not a host
                hits.append((name, ip))
        self.assertEqual(hits, [])

    def test_no_private_names_or_ids(self):
        hits = []
        for name, text in tree_files():
            words = re.findall(r"[a-z0-9]+", text.lower())
            seen = set(words) | {f"{a} {b}" for a, b in zip(words, words[1:], strict=False)}
            for item in seen:
                if digest(item) in FORBIDDEN_DIGESTS:
                    hits.append((name, digest(item)))
        self.assertEqual(hits, [], "a forbidden word or id is in the tree (digests shown)")

    def test_no_attribution_trailer_with_a_session_link(self):
        for name, text in tree_files():
            if name != "tests/test_public_tree.py":
                self.assertNotIn("Claude-Ses" + "sion", text, name)

    def test_no_secrets_shaped_strings(self):
        pattern = re.compile(r"(?i)(api[_-]?key|secret|token)\s*[:=]\s*[\"'][A-Za-z0-9]{24,}[\"']")
        hits = [n for n, text in tree_files() if pattern.search(text)]
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
