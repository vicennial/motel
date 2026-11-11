# Lifecycle

```
          checkin                 checkout time        grace                 lost_and_found
 (intent) ───────> active ───────────────────> grace ──────────> lost & found ─────────> purged
                     ▲  │ extend                 │ extend            │ restore
                     │  └────────────────────────┴─ checkout ───────▶│
                     └────────────────────────────────────────────────┘
```

| state | data | what revives it |
|---|---|---|
| active | in `<mount_root>/<floor>/rooms/<room>` | `extend` pushes the checkout time out |
| grace | intact, still in place | `extend` |
| lost & found | moved to `<floor>/.lost-and-found/<room>`, unreachable for the caller | `restore` |
| purged | deleted | nothing |

`checkout` moves a room straight to lost & found; `checkout --now` also purges it. With
`lost_and_found = "0h"` a checkout purges at once. Limits: each `checkin` and `extend` is capped by
`max_ttl_call`, and the total lifetime by `max_lifetime`.

## Floors

A floor is created on the first checkin that needs one (new volume, attached, formatted, labelled,
mounted), grown when rooms need more space and the floor cap is reached, and deleted `retire_after`
after its last room (including lost & found) is purged. Suites get an exact-size floor of their own.

**Hourly billing.** Hetzner bills a volume per started hour from creation, so deleting an empty one early inside a paid hour
saves nothing. An empty *shared* floor that has waited `retire_after` is therefore kept until its release window (the last 10
minutes before `release_margin`, 6 minutes by default (4m to 30m), ahead of the next billed hour) and retired then; the reaper
reports it under `held` (an object per floor: `floor`, `reason`, `paid_until`, `release_from`, `release_by`, `billing_source`;
the human `reap` prints it as "held: floor X is empty and kept for packing until ..."). A new checkin can pack onto it meanwhile, which also keeps it. It is retired at once, as before, for a `--suite`
floor (nothing can pack onto it, so `checkout --now` on a suite is never delayed), with `retire_after = "0m"`, and on a cloud
whose billing is not per started hour or not confirmed ([providers.md](providers.md#how-each-cloud-bills)). `checkout --now`
still purges the room's data at once; only the empty floor waits. `lost_and_found = "0h"` is unchanged.

## Housekeeping

`motel-desk reap [--apply] [--json]`. Without `--apply` it is a dry run. It: reconciles pending creates
by label, moves rooms through grace and lost & found, purges, warns about rooms near checkout, retires
empty floors (unmount, detach, delete, each behind the guard), and lists orphans, refusals and errors. Open
files in a room (exit 4 on the CLI, "refused" in the report) postpone the move; nothing is killed. It exits 5
when the report contains errors, so a failed run shows up in `systemctl status`.

`install.sh` installs `motel-housekeeping.service` and `.timer` (every 10 minutes). The cron alternative:

```
# /etc/cron.d/motel
*/10 * * * * root /usr/local/sbin/motel-desk reap --apply --json >/dev/null
```

(`sudo ./install.sh --cron` writes this file; `--no-systemd` installs neither.)

## Crashes

A floor record (`intent`) is written before the provider create call; if the response is lost the desk
finds the volume by its unique label and name. A room record is written before its directory exists. `restore` saves the record before it moves the data, and
housekeeping moves data back if a crash left an active room's data in lost & found, so a crash never loses data. Retiring
a floor is resumable: the record says `retiring` and the next run continues unmount, detach, delete.
After a reboot the desk remounts floors that still have rooms; nothing is added to `/etc/fstab`.
