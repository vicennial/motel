"""Cabin lease state machine, root side. Standard library only.

A lease is one fresh Hetzner server (plus its own firewall and an optional extra volume) in the dedicated
cabin project. This module owns the lease states, the locked root-owned records, provider reconciliation and
cleanup. Every Hetzner write goes through ``provider.GuardedApi``; this module never calls a transport itself.

Lease states::

    intent -> creating_firewall -> creating_volume -> creating_server -> bootstrapping -> active
    any state -> cleanup_pending -> released     (cleanup items are tracked one by one)

Runtime collaborators are Python callables injected by the caller (the desk, or tests); there is no option or
environment variable that changes a path, endpoint or credential.
"""

from __future__ import annotations

import decimal
import ipaddress
import re
import secrets
import shutil
import time
import urllib.parse
from datetime import timedelta

from motel import billing
from motel.cabins.errors import (
    ApiError,
    Busy,
    CheckinAborted,
    Fail,
    Guarded,
    GuardRefused,
    GuestUnknown,
    NotFound,
    Pending,
    PolicyRefused,
    Prerequisite,
    Unavailable,
    Usage,
)
from motel.cabins.guestchan import (
    GuestChannel,
    classify_activity,
    validate_activity,
    validate_ready,
    validate_seal,
)
from motel.cabins.provider import (
    egress_rules,
    ingress_rules,
    lease_labels,
    list_all,
    selector,
)
from motel.cabins.tailnet import TAILNET_V4, verify_policy
from motel.cabins.userdata import render_user_data, ssh_keygen
from motel.cabins.util import (
    ED25519_PUB_RE,
    LEASE_SCHEMA,
    OWNER_RE,
    SCHEMA,
    TASK_RE,
    expiry_label,
    is_pos_int,
    iso,
    money,
    parse_iso,
    utcnow,
)
from motel.errors import INSTALL_HINT, install_hint

# Fixed limits. Config cannot loosen them.
MAX_TTL_PER_CALL = timedelta(hours=24)
CACHE_TTL_S = 30
MIN_EXTENSION = timedelta(minutes=5)       # an extension must move the expiry by at least this much
EXTEND_SPACING = timedelta(seconds=60)     # and the same cabin cannot be extended again within this long
EXPIRY_GRACE = timedelta(minutes=10)
BOOTSTRAP_DEADLINE = timedelta(minutes=20)
WARN_BEFORE = timedelta(hours=1)
AUTHKEY_SECONDS = int((BOOTSTRAP_DEADLINE + timedelta(minutes=5)).total_seconds())
POLL_S = 10
BOOT_PROBE_EVERY = timedelta(seconds=60)     # how often a failing guest channel is also probed with a plain TCP connect
# Checkout waits for the provider instead of giving up at once: a server delete is an asynchronous action, and the
# firewall and primary IP cannot go while it still holds them. Bounded; whatever is left stays cleanup_pending.
CLEANUP_BUDGET = timedelta(seconds=180)     # everything one cleanup pass may wait for
SERVER_WAIT = timedelta(seconds=120)        # of which the server delete action may take this long
RETRY_FIRST_S, RETRY_MAX_S, RATE_LIMIT_S = 2, 10, 10
MAX_WAIT_STEPS = 80                          # a stuck clock cannot spin forever
# The reaper runs unattended from a timer, so each lease gets a shorter budget and the whole pass is bounded; leases it
# does not reach are left for the next pass (billing ones first).
REAP_BUDGET = timedelta(seconds=60)
REAP_SERVER_WAIT = timedelta(seconds=45)
REAP_PASS_BUDGET = timedelta(minutes=10)
TRANSIENT_CODES = frozenset({"resource_in_use", "locked", "conflict", "rate_limit_exceeded", "timeout"})
CLEANUP_ITEMS = ("server", "primary_ip", "firewall", "volume", "auth_key", "tailnet_node")
LIVE_STATES = ("intent", "creating_firewall", "creating_volume", "creating_server", "bootstrapping", "active",
               "cleanup_pending")
DISK_GUEST_PATH = "/mnt/cabin-disk"
GUEST_USER = "cabin"


FORWARD_RE = re.compile(r'permitlisten="127\.0\.0\.1:([0-9]{4,5})"')


def forward_port_of(extras):
    """The loopback port a tour guest may receive a reverse forward on, from the cabin key options of its extras."""
    match = FORWARD_RE.search(getattr(extras, "key_options", "") or "") if extras is not None else None
    return int(match.group(1)) if match else None


def safe_error(exc) -> str:
    if isinstance(exc, ApiError):
        return f"provider_unavailable:{exc.status}:{exc.code}"
    if isinstance(exc, GuardRefused):
        return f"guard_refused:{exc}"
    if isinstance(exc, Fail):
        return exc.message
    return type(exc).__name__


def transient(exc) -> bool:
    """A provider refusal that time alone fixes: the resource is still in use, locked or rate limited."""
    if isinstance(exc, Pending):
        return True
    return isinstance(exc, ApiError) and (exc.code in TRANSIENT_CODES or exc.status in (409, 423, 429))


def primary_ipv4_record(ipv4) -> dict:
    """What the lease remembers of the address the provider created with the server. The guarded delete refuses
    unless both the ID and a valid IPv4 address are recorded, so anything unvalidated is stored as None."""
    ipv4 = ipv4 if isinstance(ipv4, dict) else {}
    ip = ipv4.get("ip")
    try:
        valid = isinstance(ip, str) and ipaddress.ip_address(ip).version == 4 and str(ipaddress.ip_address(ip)) == ip
    except ValueError:
        valid = False
    return {"id": ipv4.get("id") if is_pos_int(ipv4.get("id")) else None, "ip": ip if valid else None}


def rate_limited(exc) -> bool:
    return isinstance(exc, ApiError) and (exc.status == 429 or exc.code == "rate_limit_exceeded")


def _provider_time(value):
    """A provider timestamp as an aware datetime, or None when it is missing or malformed."""
    try:
        parsed = parse_iso(value) if isinstance(value, str) else None
    except ValueError:
        return None
    return parsed if parsed is not None and parsed.tzinfo is not None else None


def _hourly_total(offer) -> decimal.Decimal:
    return decimal.Decimal(offer["total_hourly_net_eur"])


class Controller:
    def __init__(self, cfg, store, *, receipt=None, hetzner=None, tailnet=None, guest=None, credential_hashes=None,
                 caller_uid=0, clock=utcnow, sleep=time.sleep, keygen=ssh_keygen, template=None, guest_agent=None,
                 fetch_metadata=None, billing_rule=None, tcp_probe=None):
        self.cfg, self.store = cfg, store
        # Cabins are Hetzner servers; ``billing_rule`` lets a test state another granularity.
        self.billing = billing_rule or billing.provider_billing("hetzner").servers
        self.rc = receipt or {}
        self.hz, self.ts = hetzner, tailnet
        self.guest = guest or GuestChannel(store.keys)
        self.hashes = credential_hashes or {}
        self.uid, self.clock, self.sleep, self.keygen = caller_uid, clock, sleep, keygen
        self.template, self.guest_agent = template, guest_agent
        self.fetch_metadata = fetch_metadata
        self.tcp_probe = tcp_probe        # (ip, port) -> short text: what a TCP connect to the guest's ssh port sees (see guestchan.probe_tcp)
        self.host = self.rc.get("host") or {}
        self.host_id = self.host.get("server_id")
        self.idle_after = cfg.idle_timeout

    # -- host identity and prerequisites
    @property
    def mode(self):
        return self.cfg.access

    def sources(self) -> list:
        """The exact addresses that may reach a cabin's SSH port in ssh-allowlist mode."""
        out = []
        for key, suffix in (("public_ipv4", "/32"), ("public_ipv6", "/128")):
            value = self.host.get(key)
            if value:
                out.append(str(ipaddress.ip_address(value)) + suffix)
        return out

    def excluded_ipv4(self):
        return [self.host["public_ipv4"]] if self.host.get("public_ipv4") else []

    def location(self):
        return self.host.get("location") if self.cfg.location == "auto" else self.cfg.location

    def missing_prerequisites(self) -> list:
        missing = []
        hz = self.rc.get("hetzner") or {}
        if not self.cfg.enabled:
            missing.append("cabins are disabled (set enabled = true in [cabins])")
        if not self.cfg.types:
            missing.append("cabins.types is empty")
        if not is_pos_int(self.host_id) or not self.host.get("verified_at"):
            missing.append("this host is not identified (run: sudo motel-desk cabin setup hetzner)")
        elif not self.host.get("public_ipv4"):
            missing.append("the host's public IPv4 is not recorded in the setup receipt")
        if self.hz is None or not self.hashes.get("hetzner"):
            missing.append("the cabin project token is not installed (run: sudo motel-desk cabin setup hetzner)")
        elif hz.get("token_sha256") != self.hashes["hetzner"] or not hz.get("verified_at"):
            missing.append("no verified setup receipt for the installed cabin token "
                           "(run: sudo motel-desk cabin setup hetzner)")
        elif self.host_id not in (hz.get("protected_not_found") or []):
            missing.append("setup receipt does not prove the host is invisible to the cabin token")
        if not self.location():
            missing.append("the cabin location is unknown (set cabins.location)")
        why = getattr(self.guest, "unavailable_reason", lambda: None)()
        if why:
            missing.append(f"the guest channel cannot run ({why}; with [cabins] enabled, run the installer again: "
                           f"{install_hint('--cabins --upgrade')}; layout in docs/cabins.md)")
        if self.mode == "tailscale":
            tn = self.rc.get("tailscale") or {}
            if self.ts is None or not self.hashes.get("tailscale"):
                missing.append("the tailnet OAuth credential is not installed (run: sudo motel-desk cabin setup tailscale)")
            elif tn.get("credential_sha256") != self.hashes["tailscale"] or not tn.get("verified_at"):
                missing.append("no verified setup receipt for the tailnet credential")
        return missing

    def verify_host_live(self):
        """Re-verify against the metadata service that this is still the host (and address) that setup froze."""
        if self.fetch_metadata is None:
            raise Unavailable("cannot re-verify the host identity (metadata service not available)")
        try:
            instance = self.fetch_metadata("instance-id")
            public = self.fetch_metadata("public-ipv4")
        except (OSError, ValueError):
            raise Unavailable("cannot reach the metadata service to re-verify this host") from None
        if not instance.isdigit() or int(instance) != self.host_id:
            raise Guarded("this is not the host that setup recorded", "sudo motel-desk cabin setup hetzner")
        if public != self.host.get("public_ipv4"):
            raise Guarded("this host's public address changed since setup; cabins would be reachable from a "
                          "stale address", "sudo motel-desk cabin setup hetzner")

    def verify_tailnet(self):
        devices = verify_policy(self.ts, self.cfg.tailscale)
        return {"checked_at": iso(self.clock()), "devices": len(devices)}

    def require_ready(self):
        missing = self.missing_prerequisites()
        if missing:
            raise Prerequisite("checkin refused, prerequisites missing: " + "; ".join(missing))
        self.verify_host_live()
        try:
            self.hz.isolated_project()
        except GuardRefused as exc:
            raise Guarded(str(exc)) from None
        except ApiError as exc:
            raise Unavailable(f"provider unavailable ({safe_error(exc)})") from None
        return self.verify_tailnet() if self.mode == "tailscale" else {"checked_at": None}

    # -- offer
    def offer(self, server_type, ttl, disk_gb=None, image=None):
        allowed = self.cfg.types
        if server_type not in allowed:
            raise PolicyRefused(f"server type {server_type!r} is not allowed; choices: {', '.join(sorted(allowed))}",
                                "motel cabin checkin --type " + self.cfg.types[0])
        loc = self.location()
        try:
            types = self.hz.get("/server_types?" + urllib.parse.urlencode({"name": server_type})).get("server_types")
            pricing = self.hz.get("/pricing").get("pricing") or {}
            images = self._images(image)
        except ApiError as exc:
            raise Unavailable(f"cannot query price and availability ({safe_error(exc)})") from None
        choices = ", ".join(sorted(allowed))
        if not isinstance(types, list) or len(types) != 1 or types[0].get("name") != server_type:
            raise PolicyRefused(f"server type {server_type} is unavailable; choices: {choices}")
        st = types[0]
        if st.get("architecture") != "x86" or st.get("deprecated") or st.get("deprecation"):
            raise PolicyRefused(f"server type {server_type} is not a current x86 type; choices: {choices}")
        if isinstance(st.get("locations"), list):
            here = [x for x in st["locations"] if x.get("name") == loc]
            if not here or here[0].get("deprecation"):
                raise PolicyRefused(f"{server_type} is unavailable in {loc}; choices: {choices}")
        prices = [x for x in st.get("prices") or [] if x.get("location") == loc]
        ipv4 = [x for item in pricing.get("primary_ips") or [] if item.get("type") == "ipv4"
                for x in item.get("prices") or [] if x.get("location") == loc]
        try:
            server_net = decimal.Decimal(prices[0]["price_hourly"]["net"])
            ip_net = decimal.Decimal(ipv4[0]["price_hourly"]["net"])
        except (IndexError, KeyError, TypeError, decimal.InvalidOperation):
            raise PolicyRefused(f"no current {loc} price for {server_type} plus IPv4; refusing") from None
        if pricing.get("currency") not in (None, "EUR"):
            raise PolicyRefused("provider pricing is not in EUR")
        disk_net = decimal.Decimal(0)
        if disk_gb:
            try:
                per_gb_month = decimal.Decimal(pricing["volume"]["price_per_gb_month"]["net"])
            except (KeyError, TypeError, decimal.InvalidOperation):
                raise PolicyRefused("no current volume price; refusing an extra disk") from None
            disk_net = per_gb_month * disk_gb / 730
        total = server_net + ip_net + disk_net
        if total > decimal.Decimal(str(self.cfg.max_eur_per_hour)):
            raise PolicyRefused(
                f"{server_type} now costs EUR {money(total)}/h net, above the cap of EUR "
                f"{self.cfg.max_eur_per_hour}/h (cabins.max_eur_per_hour)")
        if not isinstance(images, list) or len(images) != 1:
            raise PolicyRefused("the configured image is not uniquely available")
        img = images[0]
        if (img.get("architecture") != "x86" or img.get("status") != "available" or img.get("deprecated")
                or img.get("type") != ("system" if self._system_image(image) else "snapshot")
                or not is_pos_int(img.get("id"))):
            raise PolicyRefused("the configured image is not current")
        hours = decimal.Decimal(int(ttl.total_seconds())) / 3600
        return {"server_type": server_type, "location": loc, "image_id": img["id"], "image_name": img.get("name"),
                "server_hourly_net_eur": money(server_net), "ipv4_hourly_net_eur": money(ip_net),
                "disk_hourly_net_eur": money(disk_net), "total_hourly_net_eur": money(total),
                "ttl_cost_net_eur": money(total * hours), "currency": "EUR"}

    def _system_image(self, image) -> bool:
        return image is None or image == self.cfg.image

    def _images(self, image):
        """Candidate images: the configured system image, or one baked tour snapshot (id or name) of this host."""
        if self._system_image(image):
            query = {"type": "system", "architecture": "x86", "name": self.cfg.image}
            return self.hz.get("/images?" + urllib.parse.urlencode(query)).get("images")
        return [i for i in self.hz.list_images()
                if str(i.get("id")) == str(image) or (i.get("labels") or {}).get("image-name") == image]

    # -- checkin
    def checkin(self, server_type=None, ttl=None, task=None, owner=None, client_key=None, disk_gb=None, *,
                image=None, userdata_extras=None, on_boot=None, abort=None):
        """``image``: a baked tour snapshot (id or name) instead of the configured system image.
        ``userdata_extras``: opaque extra cloud-init material for a tour guest (see ``motel.tours``); handed to
        ``render_user_data`` unchanged.
        ``on_boot(info)``: called after every poll of the bootstrap wait with a small dict of what the controller last saw
        (provider phase, the guest channel's last error, the guest's own boot report); it must not raise.
        ``abort()``: asked before the server is created and before every poll; when true the checkin ends, the cabin is
        cleaned up like any failed checkin and :class:`CheckinAborted` is raised (a cancelled tour does not have to sit out the bootstrap deadline)."""
        server_type = server_type or self.cfg.types[0]
        ttl = ttl or self.cfg.default_ttl
        if not TASK_RE.fullmatch(task or ""):
            raise Usage("task must be 1-32 lowercase letters, digits or hyphens", "motel cabin checkin --task build")
        if not OWNER_RE.fullmatch(owner or ""):
            raise Usage("owner must be 1-40 letters, digits, dot, dash or underscore")
        if not ED25519_PUB_RE.fullmatch(client_key or ""):
            raise Usage("a cabin-specific ed25519 client public key is required (the motel CLI makes one)")
        if ttl > MAX_TTL_PER_CALL:
            raise PolicyRefused("a cabin TTL is at most 24h per call", "motel cabin checkin --for 24h")
        if ttl > self.cfg.max_lifetime:
            raise PolicyRefused(f"a cabin lives at most {self.cfg.max_lifetime} (cabins.max_lifetime)")
        if disk_gb is not None and not (is_pos_int(disk_gb) and 10 <= disk_gb <= self.cfg.max_disk_gb):
            raise PolicyRefused(f"--disk must be between 10 GB and {self.cfg.max_disk_gb} GB (cabins.max_disk_gb)")
        if self.template is None or self.guest_agent is None:
            raise Fail("guest bootstrap files are not installed", INSTALL_HINT)
        self.store.cache_drop()
        tailnet_check = self.require_ready()
        offer = self.offer(server_type, ttl, disk_gb, image)
        rec = self._intent(task, owner, ttl, offer, client_key, tailnet_check, disk_gb)
        with self.store.lease_lock(rec["lease_id"], blocking=True):
            rec = self.store.load(rec["lease_id"])
            if rec.get("state") != "intent":
                raise PolicyRefused("cabin intent changed before provisioning; checkin refused")
            try:
                return self._provision(rec, userdata_extras, on_boot, abort)
            except BaseException as exc:
                aborted = isinstance(exc, CheckinAborted)
                self.store.ledger("checkin_aborted" if aborted else "checkin_failed", lease=rec["lease_id"], state=rec["state"],
                                  error=safe_error(exc))
                cleanup = self._cleanup(rec, "checkin_aborted" if aborted else "checkin_failed")
                if isinstance(exc, CheckinAborted):
                    raise CheckinAborted(f"checkin aborted; cleanup: {cleanup['state']}"
                                         f"{'' if cleanup['state'] == 'released' else ' (see motel cabin status)'}",
                                         "motel cabin ls") from None
                if isinstance(exc, (Fail, GuardRefused, ApiError)):
                    raise Fail(f"checkin failed ({safe_error(exc)}); cleanup: {cleanup['state']}"
                               f"{'' if cleanup['state'] == 'released' else ' (see motel cabin status)'}",
                               "motel cabin ls") from None
                raise

    def _intent(self, task, owner, ttl, offer, client_key, tailnet_check, disk_gb):
        now = self.clock()
        with self.store.global_lock():
            # Reserve capacity with the intent, across every caller and session. In-flight, unreadable and
            # cleanup-pending leases consume capacity until cleanup proves release.
            pending = sum(rec.get("state") != "released" for rec in self.store.all())
            cap = self.cfg.max_concurrent
            if pending >= cap and self._evict_held(now, owner, task):
                # A sealed idle cabin held for its release window still counts against the cap (the provider's quota is
                # real). When it is all that stands between a checkin and a vacancy, the earliest-due one goes now.
                pending = sum(rec.get("state") != "released" for rec in self.store.all())
            if pending >= cap:
                raise PolicyRefused(
                    f"no vacancy: cabin cap {cap} reached ({pending} active or cleanup-pending cabins)",
                    "motel cabin ls")
            while True:
                lease_id = f"{task}-{secrets.token_hex(6)}"
                if not self.store.exists(lease_id):
                    break
            rec = {"schema": LEASE_SCHEMA, "lease_id": lease_id, "intent_nonce": secrets.token_hex(16),
                   "host_id": self.host_id, "name": "motel-" + lease_id,
                   "server_id": None, "firewall_id": None, "volume_id": None, "task": task, "owner": owner,
                   "caller_uid": self.uid, "state": "intent", "created_at": iso(now),
                   "expires_at": iso(now + ttl), "lifetime_ends_at": iso(now + self.cfg.max_lifetime),
                   "bootstrap_deadline_at": iso(now + BOOTSTRAP_DEADLINE), "offer": offer,
                   "access": {"mode": self.mode, "sources": self.sources() if self.mode == "ssh-allowlist" else [],
                              "address": None},
                   "disk": ({"size_gb": disk_gb, "guest_path": DISK_GUEST_PATH, "link": "~/disk"}
                            if disk_gb else None),
                   "client_key": client_key, "host_key": None, "controller_key": None,
                   "primary_ipv4": {"id": None, "ip": None},
                   "tailnet": {"key_id": None, "key_expires_at": None, "device_id": None, "ip": None},
                   "tailnet_policy_checked_at": tailnet_check.get("checked_at"),
                   "expiry_label": None, "extension_pending": None,
                   "cleanup": {k: "not_created" for k in CLEANUP_ITEMS}, "cleanup_reason": None,
                   "released_at": None, "activity": None, "warnings": [], "errors": []}
            self.store.save(rec)
            self.store.ledger("intent", lease=lease_id, owner=owner, task=task, server_type=offer["server_type"],
                              hourly_net_eur=offer["total_hourly_net_eur"], expires=rec["expires_at"],
                              access=self.mode, disk_gb=disk_gb)
        return rec

    def _evict_held(self, now, owner=None, task=None) -> bool:
        """Delete the sealed idle cabin with the earliest release time, at once, to make room. True when one was taken in
        hand (whether or not the provider has finished: a cleanup that is still pending keeps counting against the cap).
        Only sealed cabins qualify: the guest already admits no new work there. A cabin another pass is working on is
        skipped. ``owner`` and ``task`` are the checkin that needs the room; they go in the ledger entry. Called with the
        global lock held."""
        due = []
        for rec in self.store.all():
            if rec.get("state") == "active" and (rec.get("admission") or {}).get("sealed_at"):
                try:
                    due.append((self._release_by_dt(rec, now) or now, rec["lease_id"]))
                except (ValueError, TypeError):
                    due.append((now, rec["lease_id"]))
        for _by, lid in sorted(due):
            try:
                with self.store.lease_lock(lid):
                    rec = self.store.load(lid)
                    if rec.get("state") != "active" or not (rec.get("admission") or {}).get("sealed_at"):
                        continue
                    self.store.ledger("idle_evicted_for_checkin", lease=lid, evicted_owner=rec.get("owner"),
                                      evicted_task=rec.get("task"), for_owner=owner, for_task=task)
                    self._cleanup(rec, "idle_evicted", REAP_BUDGET, REAP_SERVER_WAIT)
                    self.store.cache_drop()
                    return True
            except Busy:
                continue
        return False

    def _create(self, rec, kind, call):
        """Create once; on an ambiguous response reconcile by unique intent before one retry."""
        single = kind[:-1]
        for attempt in (1, 2):
            try:
                resp = call()
                obj = resp.get(single) if isinstance(resp, dict) else None
                if isinstance(obj, dict) and is_pos_int(obj.get("id")):
                    return obj
                status = 0
            except ApiError as exc:
                if exc.status and 400 <= exc.status < 500 and exc.status not in (408, 409, 429):
                    raise
                status = exc.status
            found = self._find_intent(rec, kind)
            self.store.ledger("create_reconcile", lease=rec["lease_id"], kind=kind, status=status,
                              found=len(found), attempt=attempt)
            if len(found) == 1:
                return found[0]
            if len(found) > 1:
                raise Fail(f"{len(found)} {kind} carry this cabin intent; operator review required",
                           "sudo motel-desk cabin doctor")
        raise Unavailable(f"{single} creation stayed ambiguous after reconciliation")

    def _find_intent(self, rec, kind):
        labels = lease_labels(rec)
        name = rec["name"] + ("-disk" if kind == "volumes" else "")
        found = list_all(self.hz, kind, selector(**labels))
        return [o for o in found if o.get("name") == name
                and all((o.get("labels") or {}).get(k) == v for k, v in labels.items())]

    def _provision(self, rec, userdata_extras=None, on_boot=None, abort=None):
        rec["forward_port"] = forward_port_of(userdata_extras)
        # Keys are generated in a root-only scratch directory, then written once, exclusively and relative to a
        # verified directory descriptor, into the root-owned lease key directory. Nothing is chowned.
        scratch = self.store.scratch()
        try:
            ctl_private, ctl_public = self.keygen(scratch, "controller_ed25519", rec["name"] + "-controller")
            host_private, host_public = self.keygen(scratch, "host_ed25519", rec["name"])
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        with self.store.key_dir_fd(rec["lease_id"]) as dfd:
            self.store.write_key(dfd, "controller_ed25519", ctl_private)
            # The host key is pinned by alias, so the same file serves the desk and the caller.
            self.store.write_key(dfd, "known_hosts", f"{rec['name']} {host_public}\n")
        del ctl_private
        rec.update(host_key=host_public, controller_key=ctl_public)

        authkey = None
        if self.mode == "tailscale":
            # A lost mint response leaves a key we cannot name; it expires on its own.
            rec["tailnet"]["key_expires_at"] = iso(self.clock() + timedelta(seconds=AUTHKEY_SECONDS))
            rec["cleanup"]["auth_key"] = "pending: unconfirmed mint, expires unused"
            self.store.save(rec)
            try:
                key_id, authkey = self.ts.create_key(rec)
            except ApiError as exc:
                if exc.status and 400 <= exc.status < 500 and exc.status not in (408, 409, 429):
                    rec["cleanup"]["auth_key"] = "not_created"     # definitive refusal: no key exists
                    self.store.save(rec)
                raise
            rec["tailnet"]["key_id"] = key_id
            rec["cleanup"]["auth_key"] = "present"
        rec["state"] = "creating_firewall"
        self.store.save(rec)

        rules = egress_rules(self.cfg.dns_resolvers, self.excluded_ipv4()) + ingress_rules(rec["access"])
        fw = self._create(rec, "firewalls", lambda: self.hz.create_firewall(rec, rules))
        rec["firewall_id"] = fw["id"]
        rec["cleanup"]["firewall"] = "present"
        rec["state"] = "creating_volume" if rec["disk"] else "creating_server"
        self.store.save(rec)
        self.store.ledger("firewall_created", lease=rec["lease_id"], firewall_id=fw["id"])

        if rec["disk"]:
            vol = self._create(rec, "volumes", lambda: self.hz.create_volume(
                rec, size_gb=rec["disk"]["size_gb"], location=rec["offer"]["location"]))
            rec["volume_id"] = vol["id"]
            vol_created = _provider_time(vol.get("created"))
            rec["volume_created_at"] = iso(vol_created) if vol_created else None
            rec["volume_received_at"] = iso(self.clock())
            rec["cleanup"]["volume"] = "present"
            rec["state"] = "creating_server"
            self.store.save(rec)
            self.store.ledger("volume_created", lease=rec["lease_id"], volume_id=vol["id"],
                              size_gb=rec["disk"]["size_gb"])

        user_data = render_user_data(
            self.template, rec=rec, mode=self.mode, host_private=host_private, host_public=host_public,
            client_public=rec["client_key"], controller_public=ctl_public, guest_agent=self.guest_agent,
            packages=list(self.cfg.guest_packages), sources=rec["access"]["sources"],
            tag=self.cfg.tailscale.tag if self.mode == "tailscale" else None, authkey=authkey,
            volume_id=rec["volume_id"], idle_seconds=int(self.idle_after.total_seconds()),
            dns=list(self.cfg.dns_resolvers), **({"extras": userdata_extras} if userdata_extras is not None else {}))
        del host_private, authkey
        if abort is not None and abort():
            # a cancel that arrived while the firewall or volume was being made: nothing billable beyond them exists yet
            raise CheckinAborted("checkin aborted before the server was created")
        offer = rec["offer"]
        server = self._create(rec, "servers", lambda: self.hz.create_server(
            rec, server_type=offer["server_type"], location=offer["location"], image=offer["image_id"],
            user_data=user_data))
        user_data = None
        ipv4 = ((server.get("public_net") or {}).get("ipv4") or {})
        rec["server_id"] = server["id"]
        # The billing clock: the provider's own created time, and (for the earliest-wins anchor) the local time at which the
        # create response arrived. A missing or zone-less provider time is stored as None.
        created = _provider_time(server.get("created"))
        rec["server_created_at"] = iso(created) if created else None
        rec["server_received_at"] = iso(self.clock())
        rec["primary_ipv4"] = primary_ipv4_record(ipv4)
        rec["cleanup"]["server"] = "present"
        rec["cleanup"]["primary_ip"] = "present"
        rec["state"] = "bootstrapping"
        self.store.save(rec)
        self.store.ledger("server_created", lease=rec["lease_id"], server_id=server["id"])
        self._bootstrap(rec, on_boot, abort)
        rec["state"] = "active"
        self.store.save(rec)
        self.store.ledger("active", lease=rec["lease_id"], address=rec["access"]["address"])
        return self.view(rec)

    def _bootstrap(self, rec, on_boot=None, abort=None):
        deadline = parse_iso(rec["bootstrap_deadline_at"])
        last = "waiting for the server"
        info = {"polls": 0}
        probed = tcp22 = None
        while self.clock() < deadline:
            if abort is not None and abort():
                raise CheckinAborted("checkin aborted")
            info["polls"] += 1
            info["ready"] = info["channel"] = None
            try:
                if self._bootstrap_step(rec, info):
                    return
                last = self._bootstrap_phase(rec)
                if info.get("ready"):
                    last = self._boot_summary(info["ready"])
            except ApiError as exc:
                last = safe_error(exc)
            except Pending as exc:
                last = f"{exc} yet" if str(exc).endswith("applied") else str(exc)
            except GuestUnknown as exc:
                info["channel"] = {"rc": exc.rc, "error": exc.detail or str(exc)[:120], "at": iso(self.clock())}
                now = self.clock()
                if self.tcp_probe is not None and rec["access"].get("address") and (probed is None or now - probed >= BOOT_PROBE_EVERY):
                    probed = now
                    try:
                        tcp22 = str(self.tcp_probe(rec["access"]["address"], 22))[:80]
                    except Exception:  # noqa: BLE001  a diagnostic must never end the wait
                        tcp22 = "probe failed"
                if tcp22:
                    info["channel"]["tcp22"] = tcp22
                last = f"guest not ready ({exc}" + (f"; tcp/22 {info['channel']['tcp22']}" if info["channel"].get("tcp22") else "") + ")"
            info["summary"] = last[:300]
            info["phase"] = self._bootstrap_phase(rec)
            info["at"] = iso(self.clock())
            if on_boot is not None:
                try:
                    on_boot(dict(info))
                except Exception:  # noqa: BLE001  a diagnostic must never end the wait
                    pass
            self.sleep(POLL_S)
        raise Fail(f"bootstrap deadline passed: {last[:300]}", "motel cabin ls")

    @staticmethod
    def _boot_summary(ready) -> str:
        boot = ready.get("boot") or {}
        bits = [f"stage {boot['stage']}" if boot.get("stage") else None,
                f"cloud-init {boot['cloud_init']}" if boot.get("cloud_init") else None,
                f"last line: {boot['last']}" if boot.get("last") else None,
                f"errors: {' | '.join(boot['errors'])}" if boot.get("errors") and boot["errors"][-1] != boot.get("last") else None]
        return f"guest bootstrap {ready.get('bootstrap')}" + (f" ({'; '.join(b for b in bits if b)})" if any(bits) else "")

    def _bootstrap_phase(self, rec):
        if rec.get("expiry_label") is None:
            return "waiting for the server and firewall"
        if rec["access"]["address"] is None:
            return "waiting for tailnet enrollment"
        return "waiting for guest readiness"

    def _bootstrap_step(self, rec, info=None) -> bool:
        server = self.hz.server(rec)          # identity, firewall attachment, applied status
        self.hz.firewall(rec)
        if rec.get("volume_id"):
            self.hz.volume(rec)
        if server.get("status") != "running":
            return False
        if rec.get("expiry_label") is None:
            label = expiry_label(parse_iso(rec["expires_at"]))
            if (server.get("labels") or {}).get("expires") != label:
                self.hz.extend_server(rec, label)
            rec["expiry_label"] = label
            self.store.save(rec)
        if rec["access"]["address"] is None:
            if self.mode == "ssh-allowlist":
                ip = rec["primary_ipv4"]["ip"] or ((server.get("public_net") or {}).get("ipv4") or {}).get("ip")
                try:
                    if ipaddress.ip_address(ip).version != 4:
                        raise ValueError
                except (ValueError, TypeError):
                    raise Fail("the server has no public IPv4 address") from None
                rec["access"]["address"] = ip
            else:
                devices = self.ts.lease_devices(rec)
                if not devices:
                    return False
                if len(devices) > 1:
                    raise Fail("more than one tailnet node uses this cabin name")
                node = self.ts.check_device(rec, devices[0])
                addrs = [a for a in devices[0].get("addresses") or [] if self._tailnet_v4(a)]
                if len(addrs) != 1:
                    raise Fail("tailnet node has no single tailnet IPv4 address")
                rec["tailnet"].update(device_id=node, ip=addrs[0])
                rec["access"]["address"] = addrs[0]
                rec["cleanup"]["tailnet_node"] = "present"
                self._revoke_enrollment_key(rec)
            self.store.save(rec)
        ready = validate_ready(self.guest.call(rec, "ready"))
        if info is not None:
            info["ready"] = {"bootstrap": ready["bootstrap"], **({"boot": ready["boot"]} if ready.get("boot") else {})}
        problems = self.readiness_problems(rec, ready)
        if problems == ["bootstrap not finished"]:
            return False
        if problems:
            raise Fail("guest isolation check failed: " + "; ".join(problems))
        return True

    def _revoke_enrollment_key(self, rec):
        """The one-use key has done its job once the node enrolled: revoke it now so it cannot be replayed."""
        key_id = rec["tailnet"].get("key_id")
        if not key_id:
            return
        try:
            self.ts.revoke_key(key_id)
        except (ApiError, GuardRefused):
            rec["warnings"].append("enrollment key not revoked yet; checkout retries")
            return
        rec["cleanup"]["auth_key"] = "done"
        rec["tailnet"]["key_revoked_at"] = iso(self.clock())

    @staticmethod
    def _tailnet_v4(addr):
        try:
            return ipaddress.ip_address(addr) in TAILNET_V4
        except ValueError:
            return False

    def readiness_problems(self, rec, ready) -> list:
        if ready.get("bootstrap") != "done":
            return ["bootstrap not finished"]
        problems = []
        address = rec["access"]["address"]
        mode = rec["access"]["mode"]
        if mode == "tailscale" and ready.get("tailnet_ip") != address:
            problems.append("guest tailnet address differs")
        listeners = ready.get("tcp_listeners")
        if not isinstance(listeners, list):
            return problems + ["guest listener list missing"]
        ssh_ok = False
        for item in listeners:
            addr, port = item.get("addr"), item.get("port")
            try:
                parsed = ipaddress.ip_address(addr)
            except (ValueError, TypeError):
                problems.append("unreadable listener")
                continue
            if parsed.is_loopback:
                continue
            if mode == "tailscale":
                if addr == address or (parsed.version == 6 and parsed.exploded.startswith("fd7a:115c:a1e0")):
                    ssh_ok = ssh_ok or (addr == address and port == 22)
                    continue
            elif port == 22 and (parsed.is_unspecified or addr == address):
                # ssh-allowlist: sshd on the public address, restricted by the firewalls to the host.
                ssh_ok = True
                continue
            problems.append(f"unexpected TCP listener on port {port}")
        if not ssh_ok:
            problems.append("sshd is not listening on the expected address")
        if ready.get("nft") != "loaded":
            problems.append("guest packet filter is not loaded")
        expected_forward = rec.get("forward_port")      # a tour guest: exactly one loopback reverse forward, no other
        if expected_forward is None:
            if ready.get("forwarding_disabled") is not True:
                problems.append("guest SSH forwarding is not disabled")
            if ready.get("forward_port") is not None:
                problems.append("guest allows an SSH forward that was not requested")
        elif ready.get("forward_port") != expected_forward:
            problems.append("guest SSH forwarding is not limited to the requested loopback port")
        if ready.get("gate") != "enforced":
            problems.append("guest admission gate is not enforced")
        if rec.get("volume_id") and ready.get("disk") != "mounted":
            problems.append("the extra disk is not mounted")
        return problems

    # -- views
    def _billing_start(self, rec, now):
        """(billing clock anchor, source) for a cabin: the EARLIEST of the provider's ``created`` times (server, and the
        extra disk when there is one) and the local times at which those create responses arrived, else the lease's own
        ``created_at``. ``(None, "none")`` when billing is not hour-aligned or the server was never created."""
        if not self.billing.aligned or not rec.get("server_id"):
            return None, "none"
        try:
            return billing.anchor(
                [_provider_time(rec.get("server_created_at")), _provider_time(rec.get("volume_created_at"))],
                [_provider_time(rec.get("server_received_at")), _provider_time(rec.get("volume_received_at"))],
                parse_iso(rec.get("created_at")), now)
        except (ValueError, TypeError):
            return None, "none"

    def billed_hours(self, rec):
        """Started hours the provider bills so far (or in total, once released); None when not hour-aligned."""
        now = self.clock()
        start, _source = self._billing_start(rec, now)
        if start is None:
            return None
        return billing.billed_hours(start, parse_iso(rec.get("released_at")) or now)

    def estimated_cost(self, rec):
        hours = self.billed_hours(rec)
        if hours is not None:      # every started hour is billed in full
            return money(_hourly_total(rec["offer"]) * hours)
        end = parse_iso(rec.get("released_at")) or self.clock()
        start = parse_iso(rec.get("created_at"))
        hours = decimal.Decimal(max(0, int((end - start).total_seconds()))) / 3600
        return money(_hourly_total(rec["offer"]) * hours)

    def view(self, rec):
        now = self.clock()
        expires = parse_iso(rec.get("expires_at"))
        out = {k: rec.get(k) for k in ("lease_id", "name", "state", "task", "owner", "created_at", "expires_at",
                                       "lifetime_ends_at", "server_id", "firewall_id", "volume_id", "offer",
                                       "cleanup", "cleanup_reason", "released_at", "activity", "warnings",
                                       "errors", "host_key", "caller_uid", "admission", "disk")}
        out["schema"] = SCHEMA
        access = rec.get("access") or {}
        out["access"] = {"mode": access.get("mode"), "sources": access.get("sources")}
        out["address"] = access.get("address")
        out["ssh"] = None if not access.get("address") else {
            "user": GUEST_USER, "host": access["address"], "port": 22, "host_key_alias": rec.get("name"),
            "host_key": rec.get("host_key")}
        out["cost_estimate_net_eur"] = self.estimated_cost(rec) if rec.get("offer") else None
        out["billing"] = self._billing_view(rec, now)
        out["sealed"] = bool((rec.get("admission") or {}).get("sealed_at")) and rec.get("state") == "active"
        out["release_by"] = self._release_by(rec, now) if out["sealed"] else None
        if expires and rec.get("state") not in ("released",):
            out["time_left_s"] = int((expires - now).total_seconds())
            out["expired"] = now >= expires
            out["destroy_after"] = iso(expires + EXPIRY_GRACE)
        return out

    def _billing_view(self, rec, now):
        """What the provider bills for this cabin: granularity, started hours so far, and the end of the paid hour."""
        out = {"granularity": self.billing.granularity, "billed_hours": None, "paid_until": None,
               "release_window": None, "source": None}
        start, source = self._billing_start(rec, now)
        if start is None:
            return out
        out["billed_hours"] = self.billed_hours(rec)
        out["source"] = source
        if rec.get("state") not in ("released",):
            window = billing.release_window(start, now, margin=self.cfg.release_margin)
            out["paid_until"] = iso(window.paid_until)
            out["release_window"] = {"opens": iso(window.opens), "closes": iso(window.closes)}
        return out

    def _release_by(self, rec, now):
        by = self._release_by_dt(rec, now)
        return iso(by) if by is not None else None

    def _release_by_dt(self, rec, now):
        """When a sealed (idle) cabin will have been deleted by: the end of its release window, or the TTL or lifetime if
        that comes first. None once the window is over, or when billing is not hourly: the next reaper pass then."""
        deadlines = [d for d in (parse_iso(rec.get("expires_at")), parse_iso(rec.get("lifetime_ends_at"))) if d]
        by = None
        start, _source = self._billing_start(rec, now)
        if start is not None:
            window = billing.release_window(start, now, margin=self.cfg.release_margin)
            by = window.closes if now <= window.closes else None
        return None if by is None else min([by] + deadlines)

    def _own(self, rec):
        if self.uid not in (0, rec.get("caller_uid")):
            raise NotFound(f"no cabin {rec.get('lease_id')}", "motel cabin ls")
        return rec

    # -- inventory
    def inventory(self, cached=False):
        """Complete provider inventory. Callers other than the reaper may be served a recent copy so that
        frequent ls/status/doctor calls cannot use up the reaper's API budget."""
        if cached:
            hit = self.store.cache_get("inventory", CACHE_TTL_S)
            if hit is not None:
                return hit
        labels = {"managed-by": "motel", "role": "cabin", "controller": str(self.host_id)}
        inv = {kind: list_all(self.hz, kind, selector(**labels)) for kind in ("servers", "firewalls", "volumes")}
        self.store.cache_put("inventory", inv)
        return inv

    def orphans(self, inv, records):
        known = {r.get("lease_id"): r for r in records}
        out = []
        for kind in ("servers", "firewalls", "volumes"):
            for obj in inv[kind]:
                lease = (obj.get("labels") or {}).get("lease")
                rec = known.get(lease)
                recorded = rec.get(kind[:-1] + "_id") if rec else None
                if rec is None or (recorded is not None and recorded != obj.get("id")):
                    out.append({"kind": kind[:-1], "id": obj.get("id"), "lease": lease,
                                "note": "no matching root-owned intent; not adopted, operator review"})
                elif rec.get("state") == "released":
                    out.append({"kind": kind[:-1], "id": obj.get("id"), "lease": lease,
                                "note": "recorded as released but the resource still exists"})
                elif recorded is None and rec.get("state") in ("creating_firewall", "creating_volume", "creating_server"):
                    out.append({"kind": kind[:-1], "id": obj.get("id"), "lease": lease,
                                "note": "pending reconciliation by recorded intent"})
        return out

    def status(self, lease_id=None):
        """Own cabins for the caller; the empty-inventory proof is always global."""
        all_records = self.store.all()
        if lease_id:
            records = [self._own(self.store.load(lease_id))]
        else:
            records = [r for r in all_records if self.uid in (0, r.get("caller_uid"))]
        provider = {"inventory": "unavailable", "error": None}
        orphans, live = [], set()
        if self.hz is None:
            provider["error"] = "cabin project token not installed"
        else:
            try:
                inv = self.inventory(cached=self.uid != 0)
                provider["inventory"] = "complete"
                live = {o.get("id") for o in inv["servers"]}
                orphans = self.orphans(inv, all_records)
            except (ApiError, Unavailable, GuardRefused) as exc:
                provider["error"] = safe_error(exc)
        rows = []
        for r in records:
            v = self.view(r) if r.get("state") != "unreadable" else dict(r)
            if provider["inventory"] == "complete" and r.get("state") != "unreadable":
                v["server_present"] = bool(r.get("server_id")) and r.get("server_id") in live
            rows.append(v)
        pending = [v["lease_id"] for v in rows if v["state"] in LIVE_STATES + ("unreadable",)]
        global_pending = [r for r in all_records if r.get("state") in LIVE_STATES + ("unreadable",)]
        return {"schema": SCHEMA, "provider": provider, "cabins": rows,
                "orphans": orphans if self.uid == 0 else [],
                "orphan_count": len(orphans), "active_or_pending": pending,
                "global": {"live_or_pending_cabins": len(global_pending), "orphans": len(orphans)},
                "verified_empty": provider["inventory"] == "complete" and not global_pending and not orphans}

    # -- extend
    def extend(self, lease_id, ttl):
        if ttl > MAX_TTL_PER_CALL:
            raise PolicyRefused("an extension is at most 24h per call")
        with self.store.lease_lock(lease_id):
            rec = self._own(self.store.load(lease_id))
            now = self.clock()
            if rec["state"] != "active":
                raise PolicyRefused(f"cabin is {rec['state']}; only active cabins can be extended")
            if (rec.get("admission") or {}).get("sealed_at"):
                # Sealed means the guest admits no new work and deletion is scheduled: stretching the expiry would only
                # keep an idle server that nobody can use.
                raise PolicyRefused("this cabin is idle and sealed; check in a new one", "motel cabin checkin")
            old = parse_iso(rec["expires_at"])
            if now >= old + EXPIRY_GRACE:
                raise PolicyRefused("the cabin is past its expiry grace; it will be deleted")
            new = now + ttl
            if new <= old:
                raise PolicyRefused(f"the cabin already runs until {rec['expires_at']}")
            if new - old < MIN_EXTENSION:
                raise PolicyRefused(
                    f"the cabin already runs until {rec['expires_at']}; an extension must add at least "
                    f"{int(MIN_EXTENSION.total_seconds() // 60)} minutes", f"motel cabin extend {lease_id} --for <longer>")
            last = parse_iso(rec.get("last_extended_at"))
            if last is not None and now - last < EXTEND_SPACING:
                raise PolicyRefused("this cabin was extended a moment ago; wait a minute",
                                    f"motel cabin status {lease_id}")
            if new > parse_iso(rec["lifetime_ends_at"]):
                raise PolicyRefused(f"the maximum lifetime ends at {rec['lifetime_ends_at']}")
            if self.hz is None:
                raise Prerequisite("cabin project token is not installed")
            label = expiry_label(new)
            rec["extension_pending"] = iso(new)
            self.store.save(rec)
            try:
                self.hz.extend_server(rec, label)
            except GuardRefused as exc:
                raise Guarded(str(exc)) from None
            except ApiError as exc:
                raise Unavailable(f"extension not recorded at the provider ({safe_error(exc)}); expiry unchanged") \
                    from None
            rec.update(expires_at=iso(new), expiry_label=label, extension_pending=None, last_extended_at=iso(now))
            rec["warnings"] = [w for w in rec.get("warnings", []) if not w.startswith("expires soon")
                               and not w.startswith("guest expiry") and not w.startswith("disk label")]
            if rec.get("volume_id"):
                try:
                    self.hz.extend_volume(rec, label)
                except (GuardRefused, ApiError):
                    rec["warnings"].append("disk label not updated; the expires label on the disk is stale")
            try:
                self.guest.call(rec, "set-expiry", label)
            except GuestUnknown:
                rec["warnings"].append("guest expiry guard not updated; it may power off at the old expiry")
            self.store.save(rec)
            self.store.cache_drop()
            self.store.ledger("extended", lease=lease_id, expires=rec["expires_at"])
            return self.view(rec)

    # -- checkout and cleanup
    def checkout(self, lease_id):
        with self.store.lease_lock(lease_id):
            rec = self._own(self.store.load(lease_id))
            if rec["state"] == "released":
                return self.view(rec)
            self._cleanup(rec, "checkout")
            self.store.cache_drop()
            return self.view(rec)

    def resolve(self, lease_id, forget_node=False):
        """Operator: give up on a tailnet node that cannot be proven absent or recovered, then finish cleanup.

        The operator asserts the node is gone or irrelevant (check the tailnet admin console first). Only the
        tailnet node item can be forgotten, only on a lease already in cleanup_pending, and it is recorded.
        """
        if not forget_node:
            raise Usage("name what to resolve", f"sudo motel-desk cabin resolve {lease_id} --forget-node")
        with self.store.lease_lock(lease_id):
            rec = self.store.load(lease_id)
            if rec.get("state") != "cleanup_pending":
                raise PolicyRefused(f"cabin is {rec.get('state')}; only a cleanup-pending cabin can be resolved")
            if self._pending(rec, "tailnet_node") is False:
                raise PolicyRefused("the tailnet node is not pending; nothing to forget")
            rec["cleanup"]["tailnet_node"] = "done"
            rec["warnings"].append("tailnet node forgotten by the operator, absence not verified")
            self.store.ledger("operator_forget_node", lease=lease_id)
            self._cleanup(rec, "resolve")
            self.store.cache_drop()
            return self.view(rec)

    def _mark(self, rec, item, value):
        rec["cleanup"][item] = value

    def _pending(self, rec, item):
        return rec["cleanup"][item] not in ("done", "not_created")

    def _cleanup(self, rec, reason, budget=CLEANUP_BUDGET, server_wait=SERVER_WAIT):
        """Delete every cabin resource independently. Partial failure stays visible."""
        rec["cleanup_reason"] = rec.get("cleanup_reason") or reason
        if rec["state"] != "cleanup_pending":
            rec["previous_state"] = rec["state"]
        rec["state"] = "cleanup_pending"
        rec["errors"] = []
        self.store.save(rec)
        self.store.ledger("cleanup_start", lease=rec["lease_id"], reason=reason)
        if self.hz is not None:
            try:
                self.hz.isolated_project()
            except GuardRefused as exc:
                # Wrong project, or the host is visible: no write of any kind.
                for item in CLEANUP_ITEMS:
                    if self._pending(rec, item):
                        self._error(rec, item, exc)
                self.store.save(rec)
                self.store.ledger("cleanup_result", lease=rec["lease_id"], state=rec["state"],
                                  cleanup=rec["cleanup"], errors=rec["errors"])
                return {"state": rec["state"], "cleanup": dict(rec["cleanup"])}
            except ApiError:
                pass        # each step reports its own provider error and stays pending
        # Tailnet first: it cuts cabin access at once and does not depend on the server.
        if rec["access"]["mode"] == "tailscale":
            self._cleanup_tailnet(rec)
        started = self.clock()
        deadline = started + budget
        server_gone = self._cleanup_server(rec, min(deadline, started + server_wait))
        if server_gone:
            self._cleanup_primary_ip(rec, deadline)
            self._cleanup_firewall(rec, deadline)
            self._cleanup_volume(rec, deadline)
        else:
            for item in ("primary_ip", "firewall", "volume"):
                if self._pending(rec, item):
                    self._mark(rec, item, "pending: waits for verified server absence")
        if all(not self._pending(rec, k) for k in CLEANUP_ITEMS):
            rec["state"] = "released"
            rec["released_at"] = iso(self.clock())
            self.store.remove_keys(rec["lease_id"])
        self.store.save(rec)
        self.store.ledger("cleanup_result", lease=rec["lease_id"], state=rec["state"],
                          cleanup=rec["cleanup"], errors=rec["errors"])
        return {"state": rec["state"], "cleanup": dict(rec["cleanup"])}

    def _error(self, rec, item, exc):
        msg = safe_error(exc)
        self._mark(rec, item, "pending: " + msg)
        rec["errors"].append(f"{item}: {msg}")

    def _reconcile_ids(self, rec):
        """After a crash between create and record, recover IDs by recorded intent only."""
        if self.hz is None:
            return
        kinds = [("firewalls", "firewall_id"), ("servers", "server_id")]
        if rec.get("disk"):
            kinds.insert(1, ("volumes", "volume_id"))
        for kind, key in kinds:
            if rec.get(key) is None and rec["state"] not in ("intent",):
                found = self._find_intent(rec, kind)
                if len(found) > 1:
                    raise Fail(f"{len(found)} {kind} carry this cabin intent; operator review required",
                               "sudo motel-desk cabin doctor")
                if found:
                    rec[key] = found[0]["id"]
                    item = kind[:-1]
                    rec["cleanup"][item] = "present"
                    if kind == "servers":
                        ipv4 = ((found[0].get("public_net") or {}).get("ipv4") or {})
                        rec["primary_ipv4"] = primary_ipv4_record(ipv4)
                        rec["cleanup"]["primary_ip"] = "present"
                    self.store.ledger("reconciled", lease=rec["lease_id"], kind=kind, id=found[0]["id"])

    def _absent(self, path, deadline=None):
        """True on a 404. With a ``deadline``, a rate-limited read is waited out (at least RATE_LIMIT_S) instead of
        failing the item."""
        for _ in range(MAX_WAIT_STEPS):
            try:
                self.hz.get(path)
            except ApiError as exc:
                if exc.status == 404:
                    return True
                if deadline is None or not rate_limited(exc) or self.clock() >= deadline:
                    raise
                self.sleep(RATE_LIMIT_S)
                continue
            return False
        raise Unavailable("provider stayed rate limited")

    def _pause(self, step, *, cap=RETRY_MAX_S, floor=0):
        self.sleep(max(floor, min(cap, RETRY_FIRST_S * 2 ** min(step, 6))))

    def _retry_write(self, rec, item, call, deadline):
        """One guarded write. A transient refusal (in use, locked, rate limited) is retried with backoff until the
        deadline; anything else, or an expired budget, raises. There is always at least one attempt."""
        for step in range(MAX_WAIT_STEPS):
            try:
                return call()
            except (ApiError, Pending) as exc:
                if not transient(exc) or self.clock() >= deadline:
                    raise
                code = getattr(exc, "code", "pending")
                self.store.ledger("cleanup_retry", lease=rec["lease_id"], item=item, code=code)
                self._pause(step, floor=RATE_LIMIT_S if rate_limited(exc) else 0)
        return call()

    def _wait_server_gone(self, rec, sid, action_id, deadline) -> bool:
        """The server delete action finishes, and then the server reads as absent (404).

        The server can read as absent while the action still holds its firewall and primary IP, so absence alone
        is not enough when the provider gave an action to follow."""
        action_done = not is_pos_int(action_id)
        floor = 0
        for step in range(MAX_WAIT_STEPS):
            floor = 0
            if not action_done:
                try:
                    action = self.hz.get(f"/actions/{action_id}").get("action") or {}
                except ApiError as exc:
                    floor = RATE_LIMIT_S if rate_limited(exc) else 0
                    if exc.status == 404:
                        action_done = True      # no such action any more: absence decides
                    elif not (transient(exc) or exc.status >= 500 or exc.status == 0):
                        raise
                else:
                    if action.get("status") == "success":
                        action_done = True
                    elif action.get("status") == "error":
                        code = ((action.get("error") or {}).get("code") or "failed")
                        raise Fail(f"provider server delete action failed: {str(code)[:40]}")
            try:
                if action_done and self._absent(f"/servers/{sid}", deadline):
                    return True
            except ApiError as exc:
                if not rate_limited(exc):
                    raise
            if self.clock() >= deadline:
                return False
            self._pause(step, cap=5, floor=floor)
        return False

    def _cleanup_server(self, rec, deadline) -> bool:
        if self.hz is None:
            if (rec.get("server_id") is None and rec.get("firewall_id") is None and rec.get("volume_id") is None
                    and rec["state"] == "cleanup_pending" and rec.get("previous_state") == "intent"):
                return True
            self._error(rec, "server", Prerequisite("provider token not installed"))
            return False
        try:
            self._reconcile_ids(rec)
            if rec.get("server_id") is None:
                self._mark(rec, "server", "not_created")
                return True
            sid = rec["server_id"]
            action_id = None
            if not self._absent(f"/servers/{sid}", deadline):
                try:
                    reply = self._retry_write(rec, "server", lambda: self.hz.delete_server(rec), deadline)
                except ApiError as exc:
                    if exc.status != 404:
                        raise
                    reply = None            # gone between the check and the delete (or during a retry): confirm below
                self.store.ledger("server_delete_requested", lease=rec["lease_id"], server_id=sid)
                action = reply.get("action") if isinstance(reply, dict) else None
                action_id = action.get("id") if isinstance(action, dict) else None
                if not self._wait_server_gone(rec, sid, action_id, deadline):
                    self._mark(rec, "server", "pending: deletion requested, absence not yet verified")
                    return False
            self._mark(rec, "server", "done")
            return True
        except (ApiError, GuardRefused, Fail) as exc:
            self._error(rec, "server", exc)
            return False

    def _cleanup_primary_ip(self, rec, deadline):
        """The server's address goes with it (the provider deletes an auto-created one) or is deleted here by the
        ID recorded at creation, never by listing. It stays assigned for a moment after the server is gone, so a
        refusal for "in use" is waited out inside the budget."""
        pid = (rec.get("primary_ipv4") or {}).get("id")
        if pid is None:
            if rec["cleanup"]["primary_ip"] == "present":
                self._mark(rec, "primary_ip", "pending: primary IP ID unknown; verify in the provider console")
                rec["errors"].append("primary_ip: unknown ID")
            return
        last = None
        try:
            for step in range(MAX_WAIT_STEPS):
                floor = 0
                if self._absent(f"/primary_ips/{pid}", deadline):
                    self._mark(rec, "primary_ip", "done")
                    return
                try:
                    self.hz.delete_primary_ip(rec)
                    self.store.ledger("primary_ip_delete_requested", lease=rec["lease_id"], primary_ip_id=pid)
                except (ApiError, Pending) as exc:
                    gone = isinstance(exc, ApiError) and exc.status == 404
                    if not gone and not transient(exc):
                        raise
                    if not gone:
                        last = exc
                        floor = RATE_LIMIT_S if rate_limited(exc) else 0
                        self.store.ledger("cleanup_retry", lease=rec["lease_id"], item="primary_ip",
                                          code=getattr(exc, "code", "pending"))
                if self.clock() >= deadline:
                    break
                self._pause(step, floor=floor)
            if self._absent(f"/primary_ips/{pid}", deadline):
                self._mark(rec, "primary_ip", "done")
            elif last is not None:
                self._error(rec, "primary_ip", last)
            else:
                self._mark(rec, "primary_ip", "pending: deletion requested, absence not yet verified")
        except (ApiError, GuardRefused, Fail) as exc:
            if isinstance(exc, ApiError) and exc.status == 404:
                self._mark(rec, "primary_ip", "done")
            else:
                self._error(rec, "primary_ip", exc)

    def _cleanup_firewall(self, rec, deadline):
        if rec.get("firewall_id") is None:
            self._mark(rec, "firewall", "not_created")
            return
        try:
            if self._absent(f"/firewalls/{rec['firewall_id']}", deadline):
                self._mark(rec, "firewall", "done")
                return
            self._retry_write(rec, "firewall", lambda: self.hz.delete_firewall(rec), deadline)
            self._mark(rec, "firewall", "done")
        except (ApiError, GuardRefused, Fail) as exc:
            if isinstance(exc, ApiError) and exc.status == 404:
                self._mark(rec, "firewall", "done")
            else:
                self._error(rec, "firewall", exc)

    def _cleanup_volume(self, rec, deadline):
        if rec.get("volume_id") is None:
            if rec["cleanup"]["volume"] == "present":
                self._mark(rec, "volume", "pending: volume ID unknown; verify in the provider console")
                rec["errors"].append("volume: unknown ID")
            else:
                self._mark(rec, "volume", "not_created")
            return
        try:
            if self._absent(f"/volumes/{rec['volume_id']}", deadline):
                self._mark(rec, "volume", "done")
                return
            self._retry_write(rec, "volume", lambda: self.hz.delete_volume(rec), deadline)
            self.store.ledger("volume_delete_requested", lease=rec["lease_id"], volume_id=rec["volume_id"])
            if self._absent(f"/volumes/{rec['volume_id']}", deadline):
                self._mark(rec, "volume", "done")
            else:
                self._mark(rec, "volume", "pending: deletion requested, absence not yet verified")
        except (ApiError, GuardRefused, Fail) as exc:
            if isinstance(exc, ApiError) and exc.status == 404:
                self._mark(rec, "volume", "done")
            else:
                self._error(rec, "volume", exc)

    def _cleanup_tailnet(self, rec):
        tn = rec.get("tailnet") or {}
        if self.ts is None:
            for item in ("auth_key", "tailnet_node"):
                if self._pending(rec, item):
                    self._error(rec, item, Prerequisite("tailnet credential not installed"))
            return
        if tn.get("key_id"):
            try:
                self.ts.revoke_key(tn["key_id"])
                self._mark(rec, "auth_key", "done")
            except (ApiError, GuardRefused) as exc:
                self._error(rec, "auth_key", exc)
        elif self._pending(rec, "auth_key"):
            until = parse_iso(tn.get("key_expires_at"))
            if until is not None and self.clock() >= until:
                self._mark(rec, "auth_key", "done")
            else:
                self._mark(rec, "auth_key", f"pending: unconfirmed key expires at {tn.get('key_expires_at')}")
        if not self._pending(rec, "tailnet_node"):
            return
        try:
            self._cleanup_node(rec)
        except (ApiError, GuardRefused, Fail) as exc:
            self._error(rec, "tailnet_node", exc)

    def _cleanup_node(self, rec):
        """Delete the cabin node by its recorded ID and verify absence by that ID."""
        tn = rec["tailnet"]
        node = tn.get("device_id") or self._recover_node(rec)
        if node is None:
            self._mark(rec, "tailnet_node", "not_created")
            return
        device = self.ts.get_device(node)
        if device is None:
            self._mark(rec, "tailnet_node", "done")
            return
        self.ts.delete_device(rec, device)
        self.store.ledger("tailnet_node_delete_requested", lease=rec["lease_id"])
        if self.ts.get_device(node) is None:
            self._mark(rec, "tailnet_node", "done")
        else:
            self._mark(rec, "tailnet_node", "pending: deletion requested, absence not verified")

    def _recover_node(self, rec):
        """No recorded node ID: prove that no node used this cabin's key, or recover exactly one.

        Enrollment time bounds candidates; current tags and names are mutable and cannot prove absence. Unknown
        timestamps stay candidates. Nodes claimed by other cabins are excluded. Concurrent unrelated enrollment
        may require operator review.
        """
        tn = rec["tailnet"]
        if not tn.get("key_id") and rec["cleanup"].get("auth_key") == "not_created":
            return None
        start = parse_iso(rec["created_at"]) - timedelta(minutes=5)
        end = (parse_iso(tn.get("key_expires_at")) or parse_iso(rec["created_at"]) + BOOTSTRAP_DEADLINE) \
            + timedelta(minutes=5)
        claimed = {(r.get("tailnet") or {}).get("device_id") for r in self.store.all()
                   if r.get("lease_id") != rec["lease_id"]} - {None}
        candidates = []
        for dev in self.ts.devices():
            node = dev.get("nodeId") or dev.get("id")
            if node in claimed:
                continue
            try:
                created = parse_iso(dev.get("created")) if isinstance(dev.get("created"), str) else None
                if created is not None and created.tzinfo is None:
                    created = None
                if created is not None and not start <= created <= end:
                    continue
            except (TypeError, ValueError, OverflowError):
                created = None
            candidates.append((dev, created))
        if not candidates:
            return None
        if len(candidates) == 1 and candidates[0][1] is not None and candidates[0][0].get("hostname") == rec["name"]:
            node = self.ts.check_device(rec, candidates[0][0])
            tn["device_id"] = node
            self.store.save(rec)
            self.store.ledger("reconciled", lease=rec["lease_id"], kind="tailnet_node")
            return node
        raise Fail(f"{len(candidates)} unattributed node(s) may belong to this cabin; operator review",
                   "sudo motel-desk cabin doctor")

    # -- reap
    @staticmethod
    def _reap_priority(rec, now):
        """Order a pass by how surely a lease is billing, so a pass that runs out of budget drops the least urgent.

        0. a SEALED idle lease (the guest admits nothing more, and it is held for its release window). These go first and
           are cheap (no guest call), so a pass with slow leases in it can never carry one into a new billed hour;
        1. an ACTIVE lease past its expiry plus grace (billing, and due);
        2. ``cleanup_pending`` with its server not yet confirmed gone (billing for certain);
        3. the other ACTIVE leases (idle candidates);
        4. unfinished creations;
        5. ``cleanup_pending`` with the server done (only a firewall, address or volume is left).

        Least recently touched first inside a class, so what one pass does not reach comes first in the next. A record
        that cannot be ranked (a malformed time or timestamp) goes to the front (-1): the per-lease handler in
        ``reap`` then records its error and the other leases carry on."""
        try:
            state = rec.get("state")
            if state == "active":
                expires = parse_iso(rec.get("expires_at"))
                if (rec.get("admission") or {}).get("sealed_at"):
                    cls = 0
                else:
                    cls = 1 if expires is not None and now >= expires + EXPIRY_GRACE else 3
            elif state == "cleanup_pending":
                server_done = (rec.get("cleanup") or {}).get("server") in ("done", "not_created")
                cls = 5 if server_done else 2
            else:
                cls = 4
            touched = rec.get("updated_at") or ""
            if not isinstance(touched, str):
                raise TypeError("updated_at")
            return (cls, touched, str(rec.get("lease_id") or ""))
        except Exception:  # noqa: BLE001  ranking must never stop the pass
            return (-1, "", str(rec.get("lease_id") or ""))

    def reap(self, apply=False):
        now = self.clock()
        result = {"schema": SCHEMA, "apply": bool(apply), "provider": {"inventory": "unavailable", "error": None},
                  "actions": [], "planned": [], "warnings": [], "errors": [], "orphans": [], "busy": [],
                  "deferred": [], "held": []}
        records = self.store.all()
        if self.hz is not None:
            try:
                result["orphans"] = self.orphans(self.inventory(), records)
                result["provider"]["inventory"] = "complete"
            except (ApiError, Unavailable, GuardRefused) as exc:
                result["provider"]["error"] = safe_error(exc)
        else:
            result["provider"]["error"] = "cabin project token not installed"
        for rec in records:
            if rec.get("state") == "unreadable":
                result["errors"].append({"lease": rec.get("lease_id"), "error": "cabin record unreadable"})
        pass_deadline = now + REAP_PASS_BUDGET
        for rec in sorted((r for r in records if r.get("state") in LIVE_STATES),
                          key=lambda r: self._reap_priority(r, now)):
            lid = rec["lease_id"]
            if apply and self.clock() >= pass_deadline:
                result["deferred"].append(lid)      # the next pass starts here: nothing is skipped for good
                continue
            try:
                with self.store.lease_lock(lid):
                    rec = self.store.load(lid)
                    if rec.get("state") not in LIVE_STATES:
                        continue            # released (or gone) since the snapshot: nothing left to do
                    reason = self._reap_reason(rec, now, result, apply)
                    if not reason:
                        continue
                    if not apply:
                        result["planned"].append({"lease": lid, "reason": reason})
                        continue
                    outcome = self._cleanup(rec, reason, REAP_BUDGET, REAP_SERVER_WAIT)
                    result["actions"].append({"lease": lid, "reason": reason, **outcome})
                    if outcome["state"] != "released":
                        result["errors"].append({"lease": lid, "cleanup": outcome["cleanup"]})
            except Busy:
                result["busy"].append(lid)
                deadline = parse_iso(rec.get("bootstrap_deadline_at"))
                if deadline and now > deadline + timedelta(minutes=5):
                    result["warnings"].append(f"{lid}: busy beyond its bootstrap deadline")
            except Exception as exc:  # noqa: BLE001  one lease must never stop the pass for the others
                result["errors"].append({"lease": lid, "error": safe_error(exc)})
                self.store.ledger("reap_lease_error", lease=lid, error=type(exc).__name__)
        if result["deferred"]:
            result["warnings"].append(f"{len(result['deferred'])} lease(s) left for the next pass (time budget)")
        if apply:
            self.store.cache_drop()
            self.store.clean_scratch()
        return result

    def _reap_reason(self, rec, now, result, apply=False):
        lid, state = rec["lease_id"], rec["state"]
        if state not in LIVE_STATES:
            return None
        if state == "cleanup_pending":
            return rec.get("cleanup_reason") or "cleanup_pending"
        if state != "active":
            deadline = parse_iso(rec.get("bootstrap_deadline_at"))
            return "bootstrap_deadline" if deadline is None or now >= deadline else None
        expires = parse_iso(rec["expires_at"])
        if rec.get("extension_pending"):
            result["warnings"].append(f"{lid}: an extension was interrupted; the recorded expiry stays in force")
        if now >= expires + EXPIRY_GRACE:
            return "ttl"
        if (rec.get("admission") or {}).get("sealed_at"):
            # Sealed earlier: the guest admits no new work, so finish the idle cleanup (in its release window).
            return "idle" if self._release_now(rec, result) else None
        if now >= expires:
            result["warnings"].append(f"{lid}: expired; deleted after {iso(expires + EXPIRY_GRACE)} even if busy")
            return None
        if now >= expires - WARN_BEFORE:
            result["warnings"].append(f"{lid}: expires soon at {rec['expires_at']}")
        verdict, detail, doc = self._activity(rec)
        rec["activity"] = {"checked_at": iso(now), "result": verdict, "detail": detail}
        self.store.save(rec)
        guest_sealed = bool(doc and doc.get("sealed_at"))
        if verdict == "unknown" and not guest_sealed:
            result["warnings"].append(f"{lid}: activity unknown ({detail}); relying on TTL")
            return None
        if verdict != "idle" and not guest_sealed:
            return None
        if not apply:
            return "idle" if self._release_now(rec, result) else None
        # Seal at once, so no new work can start; deleting waits for the release window.
        if self._seal(rec, result) is None:
            return None
        return "idle" if self._release_now(rec, result) else None

    def _release_now(self, rec, result) -> bool:
        """An idle cabin is released at the end of an hour it has paid for, not the moment it goes idle: the provider
        bills every started hour from creation, so an early delete saves nothing. False records the cabin as held.
        The TTL and the lifetime are promises and are never extended: the window never reaches past them."""
        if not self.billing.aligned:
            return True
        now = self.clock()      # a pass can run for minutes: judge the window by the clock, not by the pass start
        start, source = self._billing_start(rec, now)
        if start is None:
            return True
        deadlines = [d for d in (parse_iso(rec.get("expires_at")), parse_iso(rec.get("lifetime_ends_at"))) if d]
        window = billing.release_window(start, now, margin=self.cfg.release_margin,
                                        deadline=min(deadlines) if deadlines else None)
        if window.release:
            return True
        result["held"].append({"lease": rec["lease_id"], "reason": "idle", "paid_until": iso(window.paid_until),
                               "release_from": iso(window.opens), "release_by": iso(window.closes),
                               "billing_source": source})
        return False

    def _seal(self, rec, result):
        """Ask the guest to close admission atomically. Only a verified seal permits idle deletion."""
        lid = rec["lease_id"]
        try:
            doc = self.guest.call(rec, "seal")
        except GuestUnknown as exc:
            result["warnings"].append(f"{lid}: idle seal not confirmed ({exc}); no cleanup")
            return None
        sealed, verified, reason = validate_seal(doc)
        if not sealed or verified is None or verified < self.idle_after.total_seconds():
            rec["activity"] = {"checked_at": iso(self.clock()), "result": "seal_refused", "detail": reason}
            self.store.save(rec)
            result["warnings"].append(f"{lid}: idle seal refused ({reason}); no cleanup")
            return None
        rec["admission"] = {"sealed_at": iso(self.clock()), "verified_idle_seconds": verified}
        self.store.save(rec)
        self.store.ledger("idle_sealed", lease=lid, verified_idle_seconds=verified)
        return "idle"

    def _activity(self, rec):
        try:
            doc = self.guest.call(rec, "activity")
        except GuestUnknown as exc:
            return "unknown", str(exc), None
        verdict, detail = classify_activity(doc, self.idle_after.total_seconds())
        return verdict, detail, validate_activity(doc)

    # -- doctor
    def _provider_checks(self) -> list:
        checks = []

        def check(name, ok, detail=""):
            checks.append({"check": name, "ok": bool(ok), "detail": detail})
        try:
            self.hz.isolated_project()
            check("dedicated cabin project; host not visible", True)
        except (GuardRefused, ApiError) as exc:
            check("dedicated cabin project; host not visible", False, safe_error(exc))
        try:
            default = self.cfg.types[0] if self.cfg.types else None
            offer = self.offer(default, self.cfg.default_ttl)
            check("default type price and availability", True,
                  f"{offer['server_type']} {offer['location']} EUR {offer['total_hourly_net_eur']}/h net incl. IPv4")
        except Fail as exc:
            check("default type price and availability", False, exc.message)
        try:
            orphans = self.orphans(self.inventory(), self.store.all())
            check("no unmanaged cabin resources", not orphans, f"{len(orphans)} orphan(s)" if orphans else "")
        except (ApiError, Unavailable, GuardRefused) as exc:
            check("provider inventory", False, safe_error(exc))
        return checks

    def doctor(self, local_checks=()):
        checks = list(local_checks)

        def check(name, ok, detail=""):
            checks.append({"check": name, "ok": bool(ok), "detail": detail})
        check("cabin cap", True, f"{self.cfg.max_concurrent} across all sessions; includes cleanup-pending cabins")
        check("access mode", True, self.mode)
        missing = self.missing_prerequisites()
        check("setup and prerequisites", not missing, "; ".join(missing))
        if self.fetch_metadata is not None and is_pos_int(self.host_id):
            try:
                self.verify_host_live()
                check("host identity and public address match setup", True)
            except Fail as exc:
                check("host identity and public address match setup", False, exc.message)
        if self.hz is not None:
            # Provider-wide checks do not depend on who asks. Users get a recent copy so that doctor cannot be
            # used to use up the reaper's API budget; root always asks the provider.
            shared = self.store.cache_get("doctor", CACHE_TTL_S) if self.uid != 0 else None
            if shared is None:
                shared = self._provider_checks()
                self.store.cache_put("doctor", shared)
            checks.extend(shared)
            if self.uid == 0:
                run_as = getattr(self.guest, "run_as", None)
                bad = self.store.key_access_problems(run_as[2] if run_as else None)
                check("controller key layout (root-owned, group-readable, no writes)", not bad,
                      "; ".join(bad[:5]) + (" (affected cabins read as 'unknown' until checked out)" if bad else ""))
                for rec in self.store.all():
                    if rec.get("state") == "active":
                        try:
                            self.hz.server(rec)
                            self.hz.firewall(rec)
                            check(f"{rec['lease_id']} firewall attachment", True)
                        except (GuardRefused, ApiError) as exc:
                            check(f"{rec['lease_id']} firewall attachment", False, safe_error(exc))
        if self.mode == "tailscale" and self.ts is not None:
            tailnet = self.store.cache_get("doctor-tailnet", CACHE_TTL_S) if self.uid != 0 else None
            if tailnet is None:
                try:
                    info = self.verify_tailnet()
                    tailnet = [{"check": "effective tailnet policy isolates the cabin tag", "ok": True,
                                "detail": f"{info['devices']} devices checked"}]
                except Fail as exc:
                    tailnet = [{"check": "effective tailnet policy isolates the cabin tag", "ok": False,
                                "detail": exc.message}]
                self.store.cache_put("doctor-tailnet", tailnet)
            checks.extend(tailnet)
        stuck = [r for r in self.store.all() if r.get("state") == "cleanup_pending"]
        own = [r["lease_id"] for r in stuck if self.uid in (0, r.get("caller_uid"))]
        others = len(stuck) - len(own)
        detail = ", ".join(own) + (f" (+{others} belonging to other users)" if others else "")
        check("no cleanup-pending cabins", not stuck, detail.strip())
        return {"schema": SCHEMA, "ok": all(c["ok"] for c in checks), "checks": checks}
