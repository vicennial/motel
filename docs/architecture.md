# Architecture

```
agents / users ──> motel (unprivileged CLI) ──sudo -n──> motel-desk (root) ──HTTPS + token──> cloud provider
                                                           │  ├─ /etc/motel         config, setup receipt, credentials
 housekeeping timer ──> motel-desk reap --apply ───────────┘  ├─ /var/lib/motel     state, lock, guest book
                                                              └─ /mnt/motel/<floor>/rooms/<room>
```

## Vocabulary

| motel word | meaning |
|---|---|
| front desk | `motel-desk`, the root-owned helper; the only privileged component |
| room | a private `0700` directory owned by the caller, with a size, a checkout time and a task name |
| floor | a provider volume that holds many rooms, mounted at `<mount_root>/<floor id>` |
| suite | a room with a floor of its own (`--suite`) |
| lost & found | where an expired or checked-out room waits, restorable, before it is purged |
| housekeeping | `motel-desk reap`: expires, purges, retires empty floors, reconciles crashes |
| guest book | append-only JSONL ledger, `/var/lib/motel/guestbook.jsonl` |

## Components

- `motel` (`motel.cli`): validates arguments, runs `sudo -n /usr/local/sbin/motel-desk ... --json`, prints
  a path, a table or JSON. It never sees the token and passes no paths or provider ids.
- `motel-desk` (`motel.desk`): argparse with a closed set of verbs, caller identity from `SUDO_UID`
  (checked against `allow_users`), then `motel.rooms`. The installed desk reads no environment variable
  that changes its behaviour.
- `motel.rooms`: placement, caps, lifecycle and housekeeping, under one global `flock`.
- `motel.guard`: the ownership allowlist, applied before every provider write.
- `motel.providers`: `base.py` (the Protocol), `hetzner.py`, `local.py` and an in-memory `fake.py`.
- `motel.sysops`: mount, umount, tune2fs, resize2fs, blkid, lsblk, findmnt and the open-file scan behind
  one small interface, so tests never need root.
- `motel.state`: atomic JSON records, the lock and the guest book. Paths inside caller-owned directories
  are written through a no-follow directory descriptor.

A later `motel.cabins` module (disposable servers) slots in beside `motel.rooms` and reuses the desk, the
lock, the guest book and the housekeeping job.

## State

`/var/lib/motel` (root, `0700`): `floors/<id>.json` (the intent and ownership record of every volume
motel creates), `rooms/<id>.json`, `.lock`, `guestbook.jsonl`. A floor record is written **before** the
provider create call, so a lost response can be reconciled by its unique label and name. A volume with
motel labels but no record is an orphan: reported, never adopted.

## Safety rules

1. Only the desk reads the token, and only from `/etc/motel/credentials/<provider>.token` (root, `0600`).
2. Every provider write follows a fresh read of the volume and the allowlist in [`guard.py`](../src/motel/guard.py).
3. Caps (floors, GB per floor, total GB, projected monthly EUR) are checked and reserved under the lock
   before a create or resize call.
4. A floor is mounted `nodev,nosuid,noatime,discard`, never through `/etc/fstab`; the device serial and
   filesystem type are verified before it is formatted-labelled or mounted.
5. A room is moved or deleted only while its floor's mount point is a mount of the floor's own verified
   device (checked before and after mounting, and again before every move or delete). Busy (exit 4) means:
   a mount sits at or under the room, or a process holds the floor's filesystem by cwd, root, exe, open file or
   mapping. Evidence is by device, so containers and other mount namespaces count. For a room, a reference from
   another mount namespace is placed by walking its path below `/proc/PID/root` (no symlink followed, no `..`)
   and comparing the directory identities with the room's; a reference that cannot be placed at all blocks every
   room on that floor. A reference is only stat'ed when its mount (`mnt_id` from `/proc/PID/fdinfo`, looked up in
   the process's mountinfo) is not known to be on another device, so a hung network mount elsewhere is not touched.
   An unlinked ("(deleted)") file holds the floor, not a room. The scan runs in a child
   process with a hard timeout (a hung NFS or FUSE mount cannot freeze the desk while it holds the lock);
   a timeout means "unknown", reported as Busy, never as idle. A child that survives its kill is recorded in
   `/var/lib/motel/.abandoned-scan`; while it lives no new scan starts (Busy) and `motel doctor` warns. Before a floor is detached its device must be
   mounted nowhere else. Nothing is ever killed.
6. Files the desk writes inside a room (`.motel-room.json`) go through `O_NOFOLLOW` directory descriptors
   and rename, so a caller cannot redirect them with a symlink. Room trees are removed by
   `motel.fsutil.safe_rmtree`: iterative, one directory descriptor at a time (depth cannot exhaust descriptors
   or the stack), symlinks unlinked and never followed, and it refuses to cross onto another filesystem.
7. One room never stops housekeeping for the others: an `OSError` in a room or floor is reported in the run's
   `errors` (exit 5) and the rest is still processed.
   A Busy or Guard refusal that repeats for the same room or floor for more than 24 hours is also reported as an
   error (exit 5), and `motel doctor` warns about it.
8. After a provider resize (including one whose response was lost) the filesystem is grown online on the verified
   device; a marker (`fs_grow_pending`) keeps this to be retried by housekeeping until it succeeded.
9. The installer refuses to put root-run code under a directory that group or others can write, and doctor
   re-checks the installed tree and the sudoers rule.

## Transport: sudo only

`sudo` is the only supported way to reach the desk. The desk requires euid 0, takes the calling user from
`SUDO_UID` (a missing `SUDO_UID` with euid 0 is a root login, uid 0), and checks that user against `allow_users`.
`doas`, `pkexec` and setuid wrappers are not supported: they set no `SUDO_UID`, and the argument-pinned sudoers
rule is what keeps `setup` and `reap` away from `allow_users`. An administrator running `sudo motel-desk setup ...`
or `sudo motel-desk reap ...` is an operator (euid 0) and needs no entry in `allow_users`.

## Exit codes

0 ok · 2 usage · 3 no vacancy or policy refusal · 4 busy · 5 provider or system error ·
6 safety guard · 7 not found · 8 not set up · 9 inventory incomplete.
Exit 8 prints `sudo motel-desk setup hetzner|local` when the desk is installed but not set up, and
`sudo ./install.sh` when the desk binary is absent.
Every error line starts with `motel:` and ends with `Try: <next command>`.
