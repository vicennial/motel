"""Bake a tour image: a snapshot of a fresh cabin after an operator's setup script.

Flow (all on one disposable cabin, which is always deleted afterwards, even when something fails)::

    refuse a duplicate name -> check in a fresh cabin -> run the setup script as root -> finalize in one session:
    clean (cloud-init state, ssh host keys, machine-id, shell history, logs, root and home caches, and the cabin
    bootstrap's own leftovers) -> scan for credential files and REFUSE if any is found -> schedule power-off
    -> wait until the server is off -> snapshot with managed-by=motel, role=tour-image labels -> wait until the image
    is available -> check the cabin out (deletes the server).

A bake is refused on a tailscale-mode host: such a cabin holds a tailnet node key, and tour guests never use tailscale.

Finalizing is one ssh session on purpose: the cleanup removes the cabin's admission gate, so no second session could
start. The scan runs after the cleanup, so what is checked is what would be snapshotted. The setup script is the
operator's own and runs as root on the cabin; the cabin has the usual HTTP/HTTPS egress, so the script can install
software. It must not leave credentials behind: a login for an agent CLI, a cloud key or a private key makes the bake
fail, and the server is deleted without a snapshot. API keys never belong in an image; the concierge holds them.

The scan is best effort, not a proof. It looks for known credential paths, for credentials in the text of files up to
512 KiB under /root, /home, /etc (so /etc/environment and the shell rc files), /opt, /srv, /mnt, /var/www and
/usr/local (private-key blocks, ``sk-ant-``, ``sk-`` keys, ``ghp_``/``github_pat_``/``xox?-``/``AKIA`` tokens), and for
credentials inside a URL in ``.git/config``. A secret that is encoded, split, kept in a larger file, in a database or
outside those directories passes. Treat the setup script as the place that must not write credentials; the scan
catches mistakes.

Each image id is also written to ``tour-images.json`` in the cabins state directory (root-owned) when it is created;
``delete`` only removes an image that has such a record (and the provider labels).

Public entry points: :func:`bake`, :func:`images`, :func:`delete`. Everything provider-facing goes through
``controller.hz`` (``motel.cabins.provider.GuardedApi``), so image reads and writes are label-scoped and
ownership-guarded like every other write.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
from datetime import timedelta

from motel.cabins.errors import ApiError, Fail, Guarded, GuardRefused, Incomplete, PolicyRefused, Unavailable, Usage
from motel.cabins.guestchan import ssh_options
from motel.cabins.provider import IMAGE_NAME_RE
from motel.cabins.store import atomic_write
from motel.cabins.userdata import ssh_keygen
from motel.cabins.util import is_pos_int, iso

MAX_SCRIPT_BYTES = 256 * 1024
OUTPUT_CAP = 256 * 1024
DEFAULT_TTL = timedelta(hours=1)
SETUP_TIMEOUT_S = 1800
FINALIZE_TIMEOUT_S = 600
POWER_OFF_TIMEOUT_S = 300
IMAGE_TIMEOUT_S = 1800
POLL_S = 5
BAKE_TASK = "bake"
BAKE_OWNER = "motel-bake"
IMAGES_FILE = "tour-images.json"
SCAN_EXIT = 3                       # exit status of the finalize script when a credential file is found

# --- what the finalize script removes -------------------------------------------------------------------------------
# (id, what it clears, shell). Each runs as root on the cabin; the list is the documented contract of "clean".
CLEANUP_STEPS = (
    ("cloud-init", "cloud-init state (instance data, seed, logs) and the bootstrap's cloud-init.disabled marker",
     "cloud-init clean --logs --seed >/dev/null 2>&1 || true\n"
     "rm -rf /var/lib/cloud/instance /var/lib/cloud/instances /var/lib/cloud/data /var/lib/cloud/sem "
     "/var/lib/cloud/seed /run/cloud-init\n"
     "rm -f /etc/cloud/cloud-init.disabled"),
    ("ssh-host-keys", "ssh host keys (new ones come from the next cabin's user data)",
     "rm -f /etc/ssh/ssh_host_*"),
    ("machine-id", "the machine id",
     ": > /etc/machine-id\nrm -f /var/lib/dbus/machine-id\nln -s /etc/machine-id /var/lib/dbus/machine-id"),
    ("shell-history", "shell and tool history for root and every account",
     'for h in /root $(getent passwd | cut -d: -f6 | sort -u); do\n'
     '  [ -d "$h" ] || continue\n'
     '  rm -f "$h"/.bash_history "$h"/.zsh_history "$h"/.python_history "$h"/.lesshst "$h"/.viminfo '
     '"$h"/.psql_history "$h"/.node_repl_history "$h"/.sqlite_history "$h"/.wget-hsts\n'
     'done'),
    ("caches", "root and home caches, temporary directories and the apt cache",
     'for h in /root $(getent passwd | cut -d: -f6 | sort -u); do\n'
     '  [ -d "$h" ] || continue\n'
     '  rm -rf "$h"/.cache "$h"/.npm/_logs "$h"/.local/share/Trash\n'
     'done\n'
     'rm -rf /tmp/* /tmp/.[!.]* /var/tmp/* 2>/dev/null || true\n'
     'apt-get clean >/dev/null 2>&1 || true'),
    ("logs", "the journal and every log file under /var/log",
     "journalctl --rotate >/dev/null 2>&1 || true\n"
     "journalctl --vacuum-time=1s >/dev/null 2>&1 || true\n"
     "find /var/log -type f \\( -name '*.gz' -o -name '*.[0-9]' -o -name '*.old' \\) -delete 2>/dev/null || true\n"
     "find /var/log -type f -exec truncate -s 0 {} + 2>/dev/null || true"),
    ("cabin-bootstrap", "the previous cabin's bootstrap files and units, so a new boot starts from the stock image",
     "systemctl disable cabin-guest-firewall.service cabin-guest-sample.timer cabin-guest-expiry.timer "
     "motel-tour-firewall.service motel-tour-egress.timer >/dev/null 2>&1 || true\n"
     "rm -rf /etc/motel-cabin /var/lib/motel-guest /usr/local/bin/motel-guest /usr/local/sbin/cabin-bootstrap "
     "/usr/local/sbin/motel-tour-egress\n"
     "rm -f /etc/systemd/system/cabin-guest-* /etc/systemd/system/motel-tour-* "
     "/etc/systemd/system/ssh.service.d/*cabin-firewall.conf /etc/systemd/system/ssh.socket.d/*cabin-firewall.conf "
     "/etc/systemd/system/ssh.service.d/*tour-firewall.conf /etc/systemd/system/ssh.socket.d/*tour-firewall.conf "
     "/etc/systemd/system/tailscaled.service.d/10-cabin-firewall.conf\n"
     "rm -rf /etc/ssh/cabin-keys\n"
     "rm -f /etc/ssh/sshd_config.d/*cabin*.conf /etc/sudoers.d/cabinctl /etc/sysctl.d/60-cabin.conf "
     "/etc/systemd/resolved.conf.d/10-cabin.conf\n"
     "rm -f /var/log/cabin-bootstrap.log"),
)

# --- credential patterns --------------------------------------------------------------------------------------------
# Paths are matched anywhere under the scan roots. A match of any kind refuses the bake.
CREDENTIAL_PATHS = (
    "*/.claude/.credentials.json", "*/.claude/credentials.json", "*/.claude/*credentials*", "*/.config/claude/credentials*",
    "*/.codex/auth.json", "*/.config/codex/auth*", "*/.gemini/oauth_creds.json", "*/.gemini/google_accounts.json",
    "*/.config/gemini/*", "*/.aws", "*/.config/gcloud", "*/application_default_credentials.json",
    "*/.azure", "*/.config/gh/hosts.yml", "*/.docker/config.json", "*/.netrc", "*/.git-credentials", "*/.npmrc",
    "*/.pypirc", "*/.kube/config", "*/.gnupg/private-keys-v1.d", "*/.vault-token", "*/.config/hcloud/cli.toml",
    "*/.config/doctl", "*/.config/rclone/rclone.conf", "*/.cloudflared", "*/.config/anthropic", "*/.config/openai",
    "*/.local/share/opencode/auth.json", "*/.terraform.d/credentials.tfrc.json", "*/.config/op", "*/.ssh/id_rsa",
    "*/.ssh/id_dsa", "*/.ssh/id_ecdsa", "*/.ssh/id_ed25519", "*/ssh_host_*_key", "*/.tailscale*/*state*",
    "*/tailscale/*.state", "*/tailscale/tailscaled.state",
)
CREDENTIAL_NAMES = (".env", ".env.*", "*.p12", "*.pfx", "*.jks", "*.keystore", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")
ENV_TEMPLATE_SUFFIXES = ("*.example", "*.sample", "*.template", "*.dist")
# Files whose name suggests a key but whose content decides: CA bundles are fine, private keys are not.
KEY_CONTENT_NAMES = ("*.pem", "*.key")
PRIVATE_KEY_MARK = "PRIVATE KEY-----"
SNAKEOIL = "/etc/ssl/private/ssl-cert-snakeoil.key"          # the ssl-cert package's public, throwaway key
# What a secret looks like inside a text file (extended regular expressions; the scan prints paths, never matches).
CONTENT_PATTERNS = (
    PRIVATE_KEY_MARK,
    "sk-ant-",
    "(^|[^A-Za-z0-9])sk-[A-Za-z0-9_-]{12,}",
    "ghp_[A-Za-z0-9]{20,}",
    "github_pat_[A-Za-z0-9_]{20,}",
    "xox[abprs]-[A-Za-z0-9-]{10,}",
    "AKIA[0-9A-Z]{16}",
)
# A user name and password in a URL, such as https://user:token@host/repo.git (a bare user@host is not a secret).
URL_CREDENTIAL_PATTERN = "[a-zA-Z][a-zA-Z0-9+.-]*://[^/@[:space:]]*:[^/@[:space:]]+@"
CONTENT_MAX_KIB = 512
SCAN_ROOTS = ("/root", "/home", "/opt", "/srv", "/mnt", "/etc", "/usr/local", "/var/lib", "/var/www")
CONTENT_SKIP_ROOTS = ("/var/lib",)                          # databases and package state: too big, not where setup leaves keys
PRUNE = ("/proc", "/sys", "/dev", "/run", "/etc/ssl/certs", "/etc/ca-certificates", "/usr/share", "/var/lib/apt",
         "/var/lib/dpkg", "/var/lib/systemd", "/var/lib/cloud")
PRUNE_NAMES = ("node_modules", "site-packages", "dist-packages")
PRUNE_GIT = (".git",)


def credential_scan_script(roots=SCAN_ROOTS) -> str:
    """Shell that prints one path per credential-like file under ``roots`` (paths only, never contents) and exits 0
    when it ran: by path, by private-key marker in ``*.pem`` / ``*.key`` files, by secret patterns in the text of
    files up to ``CONTENT_MAX_KIB`` (not under ``CONTENT_SKIP_ROOTS``), and by credentials in a ``.git/config`` URL.
    Pure text, so it can be tested on a temporary tree."""
    q = shlex.quote
    prune = " -o ".join(["-path " + q(p) for p in PRUNE] + ["-name " + q(n) for n in PRUNE_NAMES + PRUNE_GIT])
    prune_keep_git = " -o ".join(["-path " + q(p) for p in PRUNE] + ["-name " + q(n) for n in PRUNE_NAMES])
    by_path = " -o ".join("-path " + q(p) for p in CREDENTIAL_PATHS)
    by_name = " -o ".join("-name " + q(n) for n in CREDENTIAL_NAMES)
    templates = " ".join("! -name " + q(n) for n in ENV_TEMPLATE_SUFFIXES)
    keys = " -o ".join("-name " + q(n) for n in KEY_CONTENT_NAMES)
    patterns = " ".join("-e " + q(p) for p in CONTENT_PATTERNS)
    size = f"-size -{CONTENT_MAX_KIB + 1}k"
    skip = "|".join("*" + r for r in CONTENT_SKIP_ROOTS)
    lines = [
        "scan_roots=''",
        "content_roots=''",
        "for r in " + " ".join(q(r) for r in roots) + "; do",
        "  [ -e \"$r\" ] || continue",
        "  scan_roots=\"$scan_roots $r\"",
        f"  case \"$r\" in {skip}) ;; *) content_roots=\"$content_roots $r\" ;; esac",
        "done",
        "if [ -n \"$scan_roots\" ]; then",
        f"  find $scan_roots -xdev \\( {prune} \\) -prune -o \\( {by_path} \\) -print 2>/dev/null",
        f"  find $scan_roots -xdev \\( {prune} \\) -prune -o -type f \\( {by_name} \\) {templates} -print 2>/dev/null",
        f"  find $scan_roots -xdev \\( {prune} \\) -prune -o -type f \\( {keys} \\) ! -path {q(SNAKEOIL)} "
        f"-exec grep -l -- {q(PRIVATE_KEY_MARK)} {{}} + 2>/dev/null",
        "fi",
        "if [ -n \"$content_roots\" ]; then",
        f"  find $content_roots -xdev \\( {prune} \\) -prune -o -type f {size} ! -path {q(SNAKEOIL)} "
        f"-exec grep -lIs -E {patterns} {{}} + 2>/dev/null",
        f"  find $content_roots -xdev \\( {prune_keep_git} \\) -prune -o -type f \\( -path '*/.git/config' -o -name .gitconfig \\) "
        f"{size} -exec grep -lIs -E {patterns} -e {q(URL_CREDENTIAL_PATTERN)} {{}} + 2>/dev/null",
        "fi",
        "true",                         # find -exec ... + exits 1 when grep matched nothing; that is the good case
    ]
    return "\n".join(lines) + "\n"


def finalize_script(roots=SCAN_ROOTS) -> str:
    """The whole finalize session: clean, scan, then (only when clean) schedule power-off."""
    parts = ["#!/bin/bash", "set -u", "export LC_ALL=C"]
    for step_id, what, shell in CLEANUP_STEPS:
        parts.append(f"# clean: {step_id}: {what}")
        parts.append(shell)
    parts.append("# scan: refuse to snapshot a credential")
    parts.append("hits=$({")
    parts.append(credential_scan_script(roots).rstrip("\n"))
    parts.append("} | sort -u)")
    parts.append('if [ -n "$hits" ]; then printf \'%s\\n\' "$hits" | sed \'s/^/CREDENTIAL /\'; exit ' + str(SCAN_EXIT) + '; fi')
    parts.append("echo MOTEL_BAKE_CLEAN")
    parts.append("systemd-run --no-block --on-active=3s /bin/systemctl poweroff >/dev/null 2>&1 "
                 "|| (sleep 3; /bin/systemctl poweroff) >/dev/null 2>&1 &")
    parts.append("exit 0")
    return "\n".join(parts) + "\n"


# The remote command: stage the script in a private temp file (so the script's own stdin is empty) and run it as root.
REMOTE_RUN = ('f=$(mktemp) && cat > "$f" && sudo -n /bin/bash "$f" </dev/null; rc=$?; sudo -n rm -f "$f"; exit $rc')


class BakeError(Fail):
    """A bake failed. ``cleanup`` is the cabin's cleanup state afterwards ('released' is what you want)."""

    cleanup = None
    paths = ()


class BakeRefused(BakeError, PolicyRefused):
    """Credential-like files were found; nothing was snapshotted. ``paths`` lists them (names only)."""


# --- running things on the cabin -----------------------------------------------------------------------------------

def run_capped(argv, stdin_bytes, timeout, cap=OUTPUT_CAP):
    """Run a command with stdin data, a time limit and a cap on captured output. Returns (rc, bytes)."""
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}, start_new_session=True)
    try:
        out, _ = proc.communicate(stdin_bytes, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, 9)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        proc.communicate()
        return 124, b"timed out"
    return proc.returncode, out[-cap:]


def _tail(data: bytes, limit=1500) -> str:
    text = data[-limit:].decode("utf-8", "replace")
    return re.sub(r"[^\x20-\x7e\n]", "?", text).strip()


def _read_script(path) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        raise Usage("the setup script cannot be read (it must be a regular file, not a link)",
                    "motel tour bake --help") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SCRIPT_BYTES:
            raise Usage(f"the setup script must be a regular file of at most {MAX_SCRIPT_BYTES // 1024} KiB")
        data = os.read(fd, MAX_SCRIPT_BYTES + 1)
    finally:
        os.close(fd)
    if not data.strip():
        raise Usage("the setup script is empty")
    return data


def _ssh_argv(controller, view, key_path):
    known = os.path.join(controller.store.keys, view["lease_id"], "known_hosts")
    ssh = view["ssh"]
    return (["/usr/bin/ssh", "-F", "/dev/null", "-i", key_path, *ssh_options(known, ssh["host_key_alias"]),
             "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=8",
             "-p", str(ssh["port"]), "-l", ssh["user"], "--", ssh["host"], REMOTE_RUN])


def _wait(controller, deadline_s, probe, what):
    """Poll ``probe()`` (truthy = done) using the controller's injectable clock and sleep."""
    deadline = controller.clock() + timedelta(seconds=deadline_s)
    while True:
        value = probe()
        if value:
            return value
        if controller.clock() >= deadline:
            raise Unavailable(f"timed out waiting for {what}")
        controller.sleep(POLL_S)


# --- the record of images this host made ---------------------------------------------------------------------------

def _records_path(controller) -> str:
    return os.path.join(controller.store.root, IMAGES_FILE)


def _load_doc(controller) -> dict:
    try:
        fd = os.open(_records_path(controller), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return {"images": {}, "intents": {}}
    except OSError:
        raise Guarded("the tour image record cannot be read (is it a link?); refusing to use it") from None
    with os.fdopen(fd, encoding="utf-8") as handle:
        try:
            doc = json.load(handle)
        except ValueError:
            raise Guarded("the tour image record is damaged; refusing to use it") from None
    images = doc.get("images") if isinstance(doc, dict) else None
    intents = doc.get("intents", {}) if isinstance(doc, dict) else None
    if not isinstance(images, dict) or not isinstance(intents, dict):
        raise Guarded("the tour image record is damaged; refusing to use it")
    return {"images": images, "intents": intents}


def _load_records(controller) -> dict:
    return _load_doc(controller)["images"]


def _write_doc(controller, doc: dict) -> None:
    atomic_write(_records_path(controller), json.dumps({"schema": "motel/tour-images/v1", **doc}, sort_keys=True, indent=1))


def _record_intent(controller, name, lease_id) -> None:
    """Written BEFORE the snapshot is requested: if the provider creates the image but the answer never reaches us,
    this note is what still lets the host delete that image (by its labels) afterwards."""
    with controller.store.global_lock():
        doc = _load_doc(controller)
        doc["intents"][lease_id] = {"name": name, "recorded_at": iso(controller.clock())}
        _write_doc(controller, doc)
        controller.store.ledger("tour_image_intent", name=name, lease=lease_id)


def _settle_intent(controller, lease_id, image_id=None, name=None) -> None:
    """The snapshot call has answered (``image_id``: the image is recorded in the same write) or it is cleaned up."""
    with controller.store.global_lock():
        doc = _load_doc(controller)
        doc["intents"].pop(lease_id, None)
        if image_id is not None:
            doc["images"][str(image_id)] = {"name": name, "lease": lease_id, "recorded_at": iso(controller.clock())}
            controller.store.ledger("tour_image_created", image=image_id, lease=lease_id)
        _write_doc(controller, doc)


def _forget_image(controller, image_id) -> None:
    with controller.store.global_lock():
        doc = _load_doc(controller)
        if doc["images"].pop(str(image_id), None) is not None:
            _write_doc(controller, doc)
            controller.store.ledger("tour_image_forgotten", image=image_id)


def _intent_matches(doc, labels) -> str | None:
    """The lease of a pending intent that this image's labels name (its name and its source lease), else None."""
    for lease_id, intent in doc["intents"].items():
        if labels.get("source-lease") == lease_id and labels.get("image-name") == (intent or {}).get("name"):
            return lease_id
    return None


# --- public API -----------------------------------------------------------------------------------------------------

def bake(controller, setup_script_path, name, *, server_type=None, ttl=DEFAULT_TTL, owner=BAKE_OWNER,
         setup_timeout_s=SETUP_TIMEOUT_S, run=None, keygen=ssh_keygen) -> dict:
    """Build the tour image ``name`` from a fresh cabin plus ``setup_script_path``. Returns a record::

        {"schema": "motel/tour-image/v1", "id": <image id>, "name": ..., "lease": ..., "cleanup": "released"}

    ``controller`` is a ``motel.cabins.controller.Controller`` running as root with the provider attached. ``run`` is
    ``run(argv, stdin_bytes, timeout) -> (rc, output)`` and exists for tests. Raises :class:`BakeError` (with
    ``.cleanup``) on failure and :class:`BakeRefused` when credential files were found.
    """
    if not isinstance(name, str) or not IMAGE_NAME_RE.fullmatch(name):
        raise Usage("an image name is 1-40 lowercase letters, digits and hyphens", "motel tour bake --help")
    if getattr(controller.cfg, "access", None) == "tailscale":
        raise PolicyRefused("tour images are not baked in tailscale mode (the cabin would hold a tailnet node key)",
                            "set cabins.access = \"ssh-allowlist\" to bake")
    if controller.hz is None:
        raise Fail("the cabin project token is not installed", "sudo motel-desk cabin doctor")
    script = _read_script(setup_script_path)
    run = run or run_capped
    for image in controller.hz.list_images():
        if (image.get("labels") or {}).get("image-name") == name:
            raise PolicyRefused(f"an image named {name} already exists (id {image.get('id')})",
                                "motel tour image rm " + str(image.get("id")))
    scratch = tempfile.mkdtemp(prefix="motel-bake-")
    os.chmod(scratch, 0o700)
    created = None
    lease_id = None
    failure = None
    intent_open = False
    try:
        private, public = keygen(scratch, "client_ed25519", "motel-bake")
        del private
        view = controller.checkin(server_type=server_type, ttl=ttl, task=BAKE_TASK, owner=owner, client_key=public)
        lease_id = view["lease_id"]
        key_path = os.path.join(scratch, "client_ed25519")
        argv = _ssh_argv(controller, view, key_path)
        rc, out = run(argv, script, setup_timeout_s)
        if rc != 0:
            raise BakeError(f"the setup script failed (exit {rc}): {_tail(out)}")
        rc, out = run(argv, finalize_script().encode(), FINALIZE_TIMEOUT_S)
        if rc == SCAN_EXIT:
            paths = sorted({line[len("CREDENTIAL "):].strip() for line in out.decode("utf-8", "replace").splitlines()
                            if line.startswith("CREDENTIAL ")})[:50]
            err = BakeRefused("credential files were found after cleanup; nothing was snapshotted: "
                              + ", ".join(paths), "remove them in the setup script (an image never holds a login)")
            err.paths = tuple(paths)
            raise err
        if rc != 0 or b"MOTEL_BAKE_CLEAN" not in out:
            raise BakeError(f"cleanup failed (exit {rc}): {_tail(out)}")
        rec = controller.store.load(lease_id)
        _wait(controller, POWER_OFF_TIMEOUT_S, lambda: controller.hz.server(rec, exposure=False).get("status") == "off",
              "the cabin to power off")
        _record_intent(controller, name, lease_id)
        intent_open = True
        created = controller.hz.create_image(rec, name=name)
        _settle_intent(controller, lease_id, created["id"], name)
        intent_open = False
        image = _wait(controller, IMAGE_TIMEOUT_S, lambda: _available(controller.hz.image(created["id"])),
                      "the snapshot to become available")
        record = {"schema": "motel/tour-image/v1", "id": image["id"], "name": name, "lease": lease_id,
                  "created": image.get("created"), "image_size_gb": image.get("image_size"),
                  "disk_size_gb": image.get("disk_size")}
        created = None                  # success: keep the image
        return record
    except BaseException as exc:
        failure = exc
        raise
    finally:
        cleanup = _check_out(controller, lease_id)
        shutil.rmtree(scratch, ignore_errors=True)
        if failure is not None and created is not None:
            try:
                controller.hz.delete_image(created["id"])       # never leave a half-made image behind
                _forget_image(controller, created["id"])
            except Exception:
                pass
        elif failure is not None and intent_open:
            # The snapshot call did not answer: the provider may still have made the image. Find it by its labels.
            try:
                for item in controller.hz.list_images():
                    labels = item.get("labels") or {}
                    if labels.get("source-lease") == lease_id and labels.get("image-name") == name:
                        controller.hz.delete_image(item["id"])
                        _settle_intent(controller, lease_id)
            except Exception:
                pass                                            # the intent stays: `image rm` can still remove it
        if failure is not None and isinstance(failure, BakeError):
            failure.cleanup = cleanup
        if cleanup not in (None, "released") and failure is None:
            raise Incomplete(f"the image was made but the cabin is not cleaned up yet ({cleanup})",
                             "motel cabin ls")


def _available(image):
    status = image.get("status")
    if status == "unavailable":
        raise Unavailable("the provider could not create the snapshot")
    return image if status == "available" else None


def _check_out(controller, lease_id):
    """Delete the cabin. Returns its cleanup state; never raises, so it cannot hide the original failure."""
    if lease_id is None:
        return None
    try:
        return controller.checkout(lease_id).get("state")
    except Exception as exc:                 # noqa: BLE001 - the caller reports the state, the reaper retries
        return f"checkout_failed:{type(exc).__name__}"


def images(controller) -> list:
    """This host's tour images: ``[{id, recorded, name, status, created, image_size_gb, disk_size_gb, description}]``.
    ``recorded`` says whether this host wrote a creation record for it (``delete`` needs one)."""
    out = []
    doc = _load_doc(controller)
    for item in controller.hz.list_images():
        labels = item.get("labels") or {}
        known = str(item["id"]) in doc["images"] or _intent_matches(doc, labels) is not None
        out.append({"id": item["id"], "recorded": known, "name": labels.get("image-name"),
                    "status": item.get("status"),
                    "created": item.get("created"), "image_size_gb": item.get("image_size"),
                    "disk_size_gb": item.get("disk_size"), "description": item.get("description")})
    return sorted(out, key=lambda row: (row["name"] or "", row["id"]))


def delete(controller, image_id) -> dict:
    """Delete one tour image by id. Refuses anything this host has no creation record for, and anything that does not
    carry this host's tour-image labels."""
    if not is_pos_int(image_id):
        raise Usage("an image id is a positive integer", "motel tour image ls")
    doc = _load_doc(controller)
    pending = None
    if str(image_id) not in doc["images"]:
        # No creation record: allowed only for an image that a still-pending intent names by label (the snapshot
        # call whose answer was lost). The labels are read from the provider and the guard re-checks them on delete.
        try:
            labels = controller.hz.image(image_id).get("labels") or {}
        except GuardRefused as exc:
            raise Guarded(str(exc)) from None
        except ApiError as exc:
            if exc.status != 404:
                raise
            labels = {}
        pending = _intent_matches(doc, labels)
        if pending is None:
            raise Guarded(f"image {image_id} has no creation record on this host; refusing to delete it")
    try:
        controller.hz.delete_image(image_id)
    except GuardRefused as exc:
        raise Guarded(str(exc)) from None
    except ApiError as exc:
        if exc.status != 404:
            raise
        _forget_image(controller, image_id)             # already gone at the provider: drop the stale record
        if pending is not None:
            _settle_intent(controller, pending)
        return {"schema": "motel/tour-image/v1", "id": image_id, "deleted": True, "already_gone": True}
    _forget_image(controller, image_id)
    if pending is not None:
        _settle_intent(controller, pending)
    return {"schema": "motel/tour-image/v1", "id": image_id, "deleted": True}
