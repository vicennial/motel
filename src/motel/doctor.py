"""``motel doctor``: read-only installation checks."""

from __future__ import annotations

import os
import shutil
import stat

from motel.config import Paths
from motel.errors import INSTALL_CMD, INSTALL_HINT, MotelError
from motel.providers import registry
from motel.state import SCHEMA
from motel.units import parse_iso

TOOLS = ("mount", "umount", "tune2fs", "resize2fs", "blkid", "lsblk", "findmnt")
LOCAL_TOOLS = ("losetup", "mkfs.ext4")
SEARCH = "/usr/sbin:/usr/bin:/sbin:/bin"


def run_doctor(paths: Paths, build, concierge_health=None) -> dict:
    """``build()`` returns a ``motel.rooms.Ctx`` (raising MotelError when config or setup is broken). ``concierge_health``
    is a ``() -> dict`` for the concierge's ``health()``; on a real system it defaults to the admin socket client."""
    checks = []

    def check(name: str, level: str, detail: str = "") -> None:
        checks.append({"check": name, "ok": level != "fail", "level": level, "detail": detail})

    ctx = None
    try:
        ctx = build()
        check("config valid", "ok", paths.config_file)
        check("host set up", "ok", f"provider {ctx.receipt.provider}, server {ctx.receipt.server_id}, {ctx.receipt.location}")
    except MotelError as exc:
        check("config valid and host set up", "fail", f"{exc.message} (next: {exc.hint})")

    if paths.strict:
        for label, path in (("state directory private", paths.state_dir),
                                       ("credentials directory private", paths.credentials_dir)):
            try:
                st = os.stat(path)
                good = st.st_uid == 0 and stat.S_IMODE(st.st_mode) & 0o077 == 0
                check(label, "ok" if good else "fail", f"{path} owner {st.st_uid} mode {stat.S_IMODE(st.st_mode):o}")
            except OSError as exc:
                check(label, "fail" if "state" in label else "warn", str(exc))

    if ctx is not None:
        cfg, receipt, provider, store = ctx.cfg, ctx.receipt, ctx.provider, ctx.store
        spec = registry.SPECS.get(cfg.provider)
        credential = None  # the secret file this cloud reads, if any
        if spec is not None and spec.needs_token:
            credential = paths.token_file(cfg.provider)
        elif spec is not None and spec.credentials_file and os.path.exists(os.path.join(paths.credentials_dir, spec.credentials_file)):
            credential = os.path.join(paths.credentials_dir, spec.credentials_file)
        if credential is not None:
            token = credential
            try:
                st = os.stat(token)
                good = (not paths.strict) or (st.st_uid == 0 and stat.S_IMODE(st.st_mode) & 0o077 == 0)
                check("token file root-owned 0600", "ok" if good else "fail", token)
            except OSError as exc:
                check("token file root-owned 0600", "fail", str(exc))
        try:
            who = provider.whoami()
            same = who.server_id == receipt.server_id
            check("this server matches the setup receipt", "ok" if same else "fail",
                  f"metadata says {who.server_id}, receipt says {receipt.server_id}")
        except MotelError as exc:
            check("this server matches the setup receipt", "fail", exc.message)
        try:
            inventory = provider.visible()
            others = [s for s in inventory.servers if s != receipt.server_id]
            check("provider reachable, inventory complete", "ok", f"{len(inventory.volumes)} volume(s), {len(inventory.servers)} server(s) visible")
            if cfg.provider != registry.LOCAL:
                check("what else the token can see", "warn" if others or len(inventory.volumes) > 0 else "ok",
                      f"{len(others)} other server(s) and {len(inventory.volumes)} volume(s) in this project are visible "
                      "to the credentials; the allowlist guard is what keeps motel off them")
            by_id = {v.id: v for v in inventory.volumes}
            for vid in sorted(receipt.never_touch | cfg.never_touch):
                vol = by_id.get(vid)
                if vol is None:
                    continue
                if vol.delete_protected:
                    check(f"never-touch volume {vid} delete protection", "ok", "on")
                else:
                    check(f"never-touch volume {vid} delete protection", "warn", "off; consider enabling it at the provider")
        except MotelError as exc:
            check("provider reachable, inventory complete", "fail", exc.message)
        try:
            from motel.rooms import REFUSAL_ESCALATION, Rooms
            owned = provider.list_owned({"managed-by": "motel", "role": "floor", "host": receipt.server_id})
            known = store.live_volume_ids()
            orphans = [v.id for v in owned if v.id not in known]
            check("no unrecorded motel-labelled volumes", "warn" if orphans else "ok",
                  f"orphans (left alone): {', '.join(orphans)}" if orphans else "")
            pending = [f["id"] for f in store.floors() if f["state"] == "intent"]
            check("no unresolved create intents", "warn" if pending else "ok", ", ".join(pending))
            rooms = Rooms(ctx)
            check("caps", "ok", str(rooms._capacity()))
            stuck = ctx.sysops.abandoned_scan()
            check("no stuck holder scan", "warn" if stuck else "ok",
                  f"pid {stuck} is still stuck on a hung network or FUSE mount; busy checks answer Busy until it exits "
                  "(find the mount with `findmnt -t nfs,nfs4,fuse`)" if stuck else "")
            now = rooms.now()
            stuck = [f"{kind} {rec['id']} (since {rec['refused_since']})"
                     for kind, recs in (("room", store.rooms()), ("floor", store.floors())) for rec in recs
                     if rec.get("refused_since") and now - parse_iso(rec["refused_since"]) > REFUSAL_ESCALATION]
            check("no housekeeping refusal older than 24 h", "warn" if stuck else "ok",
                  "; ".join(stuck) + (" - something keeps housekeeping from finishing; see `sudo motel-desk reap`" if stuck else ""))
        except MotelError as exc:
            check("floor inventory", "fail", exc.message)

        mount_root = paths.real(cfg.mount_root)
        try:
            st = os.lstat(mount_root)
            good = stat.S_ISDIR(st.st_mode) and ((not paths.strict) or (st.st_uid == 0 and not st.st_mode & 0o022))
            check("mount root root-owned, not writable by others", "ok" if good else "fail", mount_root)
        except OSError:
            check("mount root root-owned, not writable by others", "ok", f"{mount_root} will be created at the first checkin")
        try:
            with open(paths.real("/etc/fstab"), encoding="utf-8") as handle:
                stray = [line.strip() for line in handle if cfg.mount_root in line and not line.lstrip().startswith("#")]
            check("no motel mounts in /etc/fstab", "ok" if not stray else "fail", "; ".join(stray)[:200])
        except OSError:
            check("no motel mounts in /etc/fstab", "ok", "no fstab")
        tools = TOOLS + (LOCAL_TOOLS if cfg.provider == "local" else ())
        missing = [t for t in tools if not shutil.which(t, path=SEARCH)]
        check("required system tools", "ok" if not missing else "fail", ", ".join(missing))

    if ctx is not None:
        from motel.installer import (
            CABINS_ON,
            cabin_mode,
            cabin_state_exists,
            load_existing_config,
            render_sudoers,
            tours_from_data,
            tours_mode,
        )
        sudoers = paths.real("/etc/sudoers.d/motel")
        # the same rule the installer uses: disabling cabins keeps the reaper and the verbs that end a cabin
        cabins_on = cabin_mode(load_existing_config(paths.config_file),
                               state_exists=cabin_state_exists(paths.real("/var/lib/motel/cabins"),
                                                               paths.real("/etc/motel/cabins-receipt.json")))
        if ctx.cfg.cabins_problem:
            check("cabins config valid", "fail", f"{ctx.cfg.cabins_problem} (rooms are unaffected; fix [cabins], then: sudo motel-desk cabin doctor)")
        # the installer's rule, from the raw file: an invalid [tours] table must not also look like a sudoers mismatch
        raw_config = load_existing_config(paths.config_file)
        tours_on = tours_mode(raw_config, cabins=cabins_on,
                              state_exists=cabin_state_exists(paths.real("/var/lib/motel/tours"),
                                                              paths.real("/etc/motel/tours-receipt.json")))
        if tours_from_data(raw_config) and cabins_on is not CABINS_ON:
            check("tours have cabins", "warn", "[tours] is enabled but [cabins] is not, so tours run in cleanup mode: no new tour "
                  "can start, and users keep only the verbs that read or end a tour. Enable [cabins] (docs/cabins.md), then run: "
                  + INSTALL_HINT)
        try:
            with open(sudoers, encoding="utf-8") as handle:
                installed = handle.read()
            same = installed == render_sudoers(list(ctx.cfg.allow_users), cabins=cabins_on, tours=tours_on)
            check("sudoers rule matches allow_users", "ok" if same else "fail",
                  sudoers if same else f"{sudoers} differs from what install.sh renders from allow_users; run: {INSTALL_HINT}")
        except OSError:
            check("sudoers rule matches allow_users", "warn", f"{sudoers} not found: only root can use the desk until `{INSTALL_CMD}` runs")

    if ctx is not None and getattr(ctx.cfg, "tours_problem", None):
        check("[tours] is valid", "fail", ctx.cfg.tours_problem + ". Rooms and the cleanup of running tours still work; "
              "starting a tour does not until you fix /etc/motel/motel.toml")
    if ctx is not None and getattr(ctx.cfg, "tours", None) is not None and ctx.cfg.tours.own_login_enabled:
        check("own-login is enabled", "warn",
              "tours may use the operator's Claude token: it is placed on guests, has no per-job budget, is account-wide and "
              "cannot be revoked per job. API keys through the concierge are the recommended path (docs/tours.md)")

    if ctx is not None and getattr(ctx.cfg, "tours", None) is not None and ctx.cfg.tours.enabled:
        if concierge_health is None and paths.strict:
            def concierge_health():
                from motel.tours import deps
                return deps.concierge_client().health()
        if concierge_health is not None:
            try:
                lost = (concierge_health() or {}).get("lost_listeners") or {}
            except Exception as exc:  # noqa: BLE001  doctor reports, never raises
                check("concierge listeners", "warn", f"the concierge could not be asked ({type(exc).__name__}); "
                      "tour guests need it running (docs/concierge.md)")
            else:
                if lost:
                    check("concierge listeners", "warn",
                          f"{len(lost)} tour attempt(s) lost their listener in a concierge restart and were revoked: "
                          + "; ".join(f"{a}: {str(r)[:120]}" for a, r in sorted(lost.items())[:5])
                          + ". Those guests failed with concierge_listener_lost; start them again (docs/concierge.md)")
                else:
                    check("concierge listeners", "ok", "no attempt lost its listener since the concierge started")

    if ctx is not None and getattr(ctx.cfg, "tours", None) is not None and ctx.cfg.tours.enabled:
        from motel.tours import bundle
        problems = bundle.setup_problems(paths.real(bundle.INSPECT_ROOT))
        check("tour bundle inspection is set up", "fail" if problems else "ok",
              "; ".join(problems) if problems else
              f"git runs as {bundle.INSPECT_USER} in {bundle.INSPECT_ROOT} under prlimit; git and prlimit are installed")

    if paths.strict:
        from motel.installer import CLI, DESK, LIB, audit_chain, audit_entry, audit_tree
        if os.path.isdir(LIB):
            problems = []
            for directory in (LIB, os.path.dirname(DESK), os.path.dirname(CLI)):
                problems += audit_chain(directory)
            problems += audit_tree(LIB)
            check("installed code is root-owned and not writable by others", "ok" if not problems else "fail",
                  LIB if not problems else "; ".join(problems[:4]))
            entries = audit_entry(DESK, "motel.desk") + audit_entry(CLI, "motel.cli")
            check("installed entry files (motel-desk, motel) are root-owned and unmodified", "ok" if not entries else "fail",
                  f"{DESK}, {CLI}" if not entries else "; ".join(entries[:4]))
        else:
            check("installed code is root-owned and not writable by others", "warn", f"{LIB} not found: motel was not installed with install.sh")

    if ctx is not None and cabins_on:
        cabin_timer = paths.real("/etc/systemd/system/motel-cabins-housekeeping.timer")
        cabin_cron = paths.real("/etc/cron.d/motel")
        reaped = os.path.exists(cabin_timer)
        if not reaped and os.path.exists(cabin_cron):
            with open(cabin_cron, encoding="utf-8") as handle:
                reaped = "cabin reap" in handle.read()
        check("cabin housekeeping scheduled", "ok" if reaped else "warn",
              "" if reaped else "none found: cabins are deleted only at checkout until `sudo motel-desk cabin reap --apply` runs; "
              "run `sudo motel install --upgrade`")
    timer = paths.real("/etc/systemd/system/motel-housekeeping.timer")
    cron = paths.real("/etc/cron.d/motel")
    scheduled = os.path.exists(timer) or os.path.exists(cron)
    check("housekeeping scheduled (systemd timer or cron)", "ok" if scheduled else "warn",
          timer if os.path.exists(timer) else cron if os.path.exists(cron) else
          "none found: rooms will not expire until `sudo motel-desk reap --apply` runs")
    return {"schema": SCHEMA, "ok": all(c["ok"] for c in checks), "checks": checks}
