# Cabins: disposable servers by the hour

> **Experimental.** The `ssh-allowlist` mode is exercised against real Hetzner projects by the live harness
> (`tests/live/`); the `tailscale` mode and the tailnet policy analysis are tested against recording fakes only. Use a
> dedicated, empty cloud project and a low price cap, and watch your first checkout complete.

A cabin is one fresh Hetzner Cloud server, with its own per-lease firewall and an optional extra disk,
that you take for a few hours, use over SSH and check out. **Checkout deletes it. There is no restore.**
Copy results out first.

```
motel cabin run --for 90m -- make test      # check in, sync this git checkout, run, check out
```

## First cabin

Five lines, on the Linux machine that will launch cabins. This machine must **not** be in the cabin project.

```bash
# 1. In the Hetzner console: create a NEW, EMPTY project and a Read & Write API token in it.
sudo uvx --from motel motel install --cabins --users $USER   # 2. a new config enables cabins
sudo motel-desk cabin setup hetzner                          # 3. paste the token at the hidden prompt
sudo motel-desk cabin doctor                                 # 4. every line should say ok
motel cabin run --for 1h -- uname -a                         # 5. check in, run, check out
```

If `/etc/motel/motel.toml` already exists (you use rooms), the installer keeps it: add the `[cabins]` table described
below, then run `sudo uvx --from motel motel install --cabins --upgrade`. The steps again, in detail:

1. Create a **new, empty Hetzner project** for cabins and an API token with read/write rights in it.
2. Add a `[cabins]` table to `/etc/motel/motel.toml` (see below). Users who may take cabins are the same
   `[host] allow_users` as for rooms.
3. As root, on the machine that will launch cabins:
   `sudo motel-desk cabin setup hetzner` (the token is typed at a hidden prompt) and then
   `motel cabin ls` and `sudo motel-desk cabin doctor`.
   For automation: `sudo motel-desk cabin setup hetzner --token-file /etc/motel/cabins.token` (a root-owned
   regular file, mode `0600` or stricter, at most 4096 bytes, never a symlink). The token is never printed
   and is not accepted from an argument or the environment.
4. `motel cabin run --for 1h -- uname -a`.

## Configuration

```toml
[host]
allow_users = ["alice", "@staff"]

[cabins]
enabled = true
access = "ssh-allowlist"        # or "tailscale"
types = ["cpx32", "cpx52"]      # allowed server types; the FIRST one listed is the default
dns_resolvers = ["185.12.64.1", "185.12.64.2"]   # the only DNS the cabin firewall allows; set in the guest too
max_eur_per_hour = 0.20         # live price cap, net, including IPv4 and any extra disk
default_ttl = "2h"              # per call, at most 24h
idle_timeout = "30m"            # 30m is the minimum; longer is allowed
release_margin = "6m"           # an idle cabin is deleted this long before the next billed hour starts (4m to 30m)
max_concurrent = 4              # across all users and sessions
max_lifetime = "7d"             # hard ceiling including extensions (at most 14d)
location = "auto"               # "auto" = this host's location from the setup receipt
max_disk_gb = 500
image = "ubuntu-24.04"
guest_packages = ["git", "rsync", "ca-certificates", "curl"]
```

Unknown keys are errors. The cloud token lives in `/etc/motel/credentials/cabins.token` (root, `0600`)
and the setup receipt in `/etc/motel/cabins-receipt.json` (nonsecret). The core `Config` loader should hand
the `cabins` table to `motel.cabins.config.parse_section`.

## The cloud project must be dedicated

A cabin token must see **only motel cabins**. Setup and every checkin re-prove this:

- every server, volume and firewall in the project carries `managed-by=motel`, `role=cabin` and this host's
  `controller` label, and a `motel-` name;
- no project firewall is attached by label selector or to a protected server (a selector firewall could
  silently cover a cabin or the host);
- the host that runs motel is **not visible** to the token (`GET /servers/<host id>` returns 404 after a
  complete inventory);
- at setup, every labelled resource also has a root-owned intent record on this host.

If any check fails, motel refuses and writes nothing: no token is stored, no server is created, and
cleanup performs no write. A resource that carries motel labels but has no local intent record is reported
as an **orphan** and never adopted or deleted.

The host's own server ID, public IPv4 and IPv6 are read from the host's metadata service during setup and
re-verified before each checkin; they are not configured by hand.

## Access modes

### `ssh-allowlist` (default)

The cabin gets a public IPv4 address (no IPv6). Each lease has **its own cloud firewall** created with the
server:

- inbound: **TCP/22 only, only from this host's exact public addresses** (one `/32`, and one `/128` if the
  host has a global IPv6 address). Never `0.0.0.0/0`, never a subnet; the guard refuses such a rule
  even if the lease record is forged.
- outbound: DNS to the configured resolvers, HTTP and HTTPS to the public IPv4 internet minus reserved
  and private ranges and minus this host's own address.

Inside the guest a second packet filter allows the same sources only, SSH is key-only, and SSH is reached
through a forced-command **admission gate**. Reachability from anywhere else is not possible, but this
is not a private network: SSH on a public address can be probed by anyone, and the host's address is
the only thing standing in front of it. Choose `tailscale` if that is not acceptable.

### `tailscale` (opt-in)

The cabin has no inbound firewall rule and sshd listens only on its tailnet address. It joins with a
one-use, ephemeral, preauthorized, tagged key that is revoked at checkout. Setup
(`sudo motel-desk cabin setup tailscale`, two hidden prompts for an OAuth client) and every checkin check
that the tailnet policy gives the cabin tag no outbound access and gives the controller TCP/22 only,
using a static analysis of the policy and the provider's own policy tests. Required config:

```toml
[cabins.tailscale]
tag = "tag:motel-cabin"
controller_addresses = ["100.64.0.1", "fd7a:115c:a1e0::1"]   # every address of this host's tailnet device
allowed_tag_owners = ["autogroup:admin"]
```

The policy analysis is deliberately conservative and generic. If it refuses a policy that is in fact safe,
simplify the policy.

## Installer prerequisites

Enable `[cabins]` in `/etc/motel/motel.toml` first, then run the installer (again, on a host that already has
rooms): `sudo uvx --from motel motel install --cabins --upgrade`. It creates the user and layout below, the cabin
sudoers verbs and the cabin reaper. `cabin doctor` names this command when the user or the keys tree is missing.

The guest ssh client never runs as root. The installer must create a system user and group `motel-cabin` (no
login shell; `useradd --system --user-group motel-cabin`, because the ssh client runs with
`--clear-groups --regid <primary gid>`, so the tree's group has to be the user's primary group) and this layout, all **owned by root, never by that user**:

| path | owner, mode |
|---|---|
| `/var/lib/motel-cabin` (the user's home) | `root:motel-cabin` 0750 |
| `/var/lib/motel-cabin/keys` | `root:motel-cabin` 2750 (setgid) |
| `keys/<lease>/` (created by the desk) | `root:motel-cabin` 0750, group inherited from `keys` |
| `keys/<lease>/controller_ed25519`, `known_hosts` | `root:motel-cabin` 0640 |

The user can read the keys through its group but can write nothing there, so it cannot swap a directory for a
symlink to redirect what root writes. The desk never chowns anything: it generates keys in a root-only scratch
directory, then creates each file exclusively (`O_EXCL|O_NOFOLLOW`) relative to a directory descriptor it has
verified with `fstat` (owned by root, not group- or world-writable). OpenSSH accepts a key owned by another uid.
The layout is verified lazily: `checkin` is refused, before any provider call, unless `keys` is
`root:<motel-cabin primary gid>` mode 2750 and the home is root-owned, group-traversable and not writable; and every
guest call re-checks it. A bad layout therefore never blocks `checkout` or `reap`: the guest simply reads as
"unknown" and cabins fall back to TTL-only cleanup. `motel-desk cabin doctor` (as root) reports the same check and
lists cabins whose keys do not fit it; such a cabin
(for instance one created before the layout existed) reads as "unknown" and is bounded only by its TTL until it is
checked out.

The front desk runs `ssh` through `setpriv --reuid ... --clear-groups --no-new-privs` (or `runuser`). If
neither tool nor the user is available, `checkin` reports a missing prerequisite and the guest channel is
refused; the reaper then relies on the TTL. Guest replies are read with a hard 64 KiB cap and a time limit,
strictly type-checked, and any malformed reply counts as "unknown", never "idle".

Accepted residual risk: one system user can read every lease's controller key. Someone who compromises that
user (for example through a bug in the ssh client while it talks to a hostile guest) could ask other cabins'
guests to report status, seal an idle cabin or move its guest-side expiry. These keys only open the restricted
`cabinctl` command on cabins; they cannot create, delete or reach anything else. Per-lease isolation would need
a user or group per lease and was judged not worth the complexity for this first version.

## Using a cabin

| command | what it does |
|---|---|
| `motel cabin run [--type T] [--for D] [--task N] [--disk SIZE] [--image NAME] [--keep] -- CMD...` | check in, `rsync` the current git checkout (tracked files plus untracked files that are not ignored) to `~/work`, run `CMD` there, check out. The exit status is the command's when the command ran and the cabin is released or only waits for provider cleanup (see the exit rules below). With `--keep` it stays. With `--json` the last line of stdout is one object `{schema, ok, cabin, exit_code, kept, released, hourly_net_eur, cost_estimate_net_eur}` (plus `billed_hours`, the started hours the estimate counts, when the provider bills per started hour, and `image`, the image name, when a baked image was used); the command's own stdout and stderr are passed through untouched, and the `motel:` status lines stay on stderr. |
| `motel cabin run --pool NAME [--type T] [--for D] [--disk SIZE] [--image NAME] [--json] -- CMD...` | opt-in per-user warm pool: reuse a matching idle cabin of yours (syncing `~/work` with `rsync --delete` so stale workspace files do not leak) or check in a new one (`--task pool-NAME`, default `--for 1h`) and keep it warm in the pool. Not combined with `--cabin`, `--keep` or `--task`. `--json` adds `pool` and `pool_hit`. |
| `motel cabin run --cabin ID [--json] -- CMD...` | the same sync and run in a cabin you already hold: no checkin, no checkout, the cabin keeps its own checkout time. Not combined with `--type`, `--for`, `--task`, `--disk`, `--image`, `--keep` or `--pool`. |
| `motel cabin pool ls [NAME] [--json]` | list your pooled cabins with live state, busy/idle lock status, type, image, `paid_until` and `release_window`, pruning released entries |
| `motel cabin pool drain [NAME] [--json]` | check out every idle (unlocked) cabin in the named pool (or all pools), skipping busy locked cabins with a note |
| `motel cabin checkin [--type T] [--for D] [--task N] [--disk SIZE] [--image NAME] [--json]` | take a cabin; prints its id |
| `motel cabin images [--json]` | the baked images you can start a cabin from: name, id, created, size |
| `motel cabin ls [--json]` / `status ID [--json]` | your cabins; provider inventory status |
| `motel cabin doctor [--json]` | the setup checks, for your own cabins (root sees all); a failing check exits 8 |
| `motel cabin ssh ID [-- CMD...]` | open a shell, or run `CMD` |
| `motel cabin extend ID --for D [--json]` | late checkout, within the 24h-per-call and maximum-lifetime limits; it must add at least 5 minutes and the same cabin can be extended once a minute |
| `motel cabin checkout ID [--json]` | delete the cabin (final) |

`--disk 200G` adds a volume in the same cabin project. It is attached at creation, mounted at
`/mnt/cabin-disk` (symlinked as `~/disk`), carries the cabin's labels and expiry, and is deleted with the
cabin after the server is verified absent. It is priced into the hourly cap.

### Warm pools (opt-in)

Default `motel cabin run` and `motel tour` always start a **fresh server per job**. When you run many short, trusted
jobs back-to-back (for example, iterating on a test suite) and want to reuse a server during the hour you already paid
for, opt in with `--pool NAME`:

```bash
motel cabin run --pool ci -- pytest                 # cold miss: checks in a cabin (--for 1h) and keeps it warm
motel cabin run --pool ci -- pytest tests/auth      # warm hit: syncs ~/work with --delete, runs in seconds
motel cabin pool ls                                 # inspect live state, busy/idle lock, paid_until / release_window
motel cabin pool drain ci                           # check out idle cabins now (skips busy cabins)
```

- **Strictly per Unix user, client-side only.** Pool indexes and per-cabin `fcntl.flock` lockfiles live under your own
  `~/.local/state/motel/pools/<name>/` (directories `0700`, files `0600`). Cabins are never shared across Unix users,
  and pooling requires no changes to `motel-desk` or sudoers.
- **Exact shape matching.** A pooled cabin is only reused when its live `(server_type, image, disk)` matches your
  request exactly, its state is `active` and not `sealed`, its per-cabin lock is free, and at least 8 minutes remain
  before `min(expires_at, billing.release_window.opens)`. Dead, released, sealed or expiring entries are pruned cleanly.
  If every matching cabin is currently busy running another job, `--pool` checks in another cabin (up to `max_concurrent`).
- **Lifecycle and cost.** Pooled runs do not call `checkout` on completion; when the pool goes idle, `motel-desk`'s
  existing idle reaper seals the cabin and deletes it in the paid-hour release window before the next billed hour starts.
  Measured live on Hetzner `cpx42` (`fsn1`): a cold miss took `64.8 s` (`ready in 63s`), while a subsequent warm hit
  took `1.24 s` (**52× faster**) on the same cabin inside the same paid hour. Run `motel cabin pool drain [NAME]` to
  release idle pooled cabins immediately.
- **Honest isolation boundary.** On pooled runs, `~/work` is synced with `rsync --delete` so files left behind in the
  workspace by an earlier job are removed, but **system state outside `~/work`** (installed packages, `/tmp`, caches,
  background state) persists across runs in the same pool. Use `--pool` only for same-owner, mutually trusted jobs; use
  default `motel cabin run` (no `--pool`) or `motel tour` whenever a job needs a clean VM.

### Start from a baked image

A bare cabin is a stock Ubuntu server. An operator can bake an image once (a snapshot with software already installed,
made with `sudo motel-desk tour bake NAME --script setup.sh`, see [Tours](tours.md#operator-setup)), and a cabin can then
start from it: `motel cabin checkin --image NAME` or `motel cabin run --image NAME -- CMD`. `motel cabin images` lists the
names. `--image` takes a name or the numeric id from that list.

- **Who may use an image.** Any user who may use cabins (`allow_users`) may start from any image baked on this host. There is
  no list per user. The operator chose the script, and the bake refuses to snapshot a server that holds known credential
  files, but the scan is best effort: put nothing private in an image.
- **What the desk accepts.** The desk checks the argument itself and does not trust the client. It must be a plain name (1 to 40
  lowercase letters, digits and hyphens) or digits only, and it must match exactly one image that this host baked: a snapshot with this
  host's motel labels, recorded in the host's own image list and available. It never selects another snapshot, a
  system image or an image of another host in the cloud project. An unknown or ambiguous name is refused (exit 3; for two images
  with the same name, use the id).
- **Receipt.** The receipt and `--json` show the image name (`receipt.image`, `offer.image_name`) when a baked image was used,
  and nothing when the stock image was.
- **Upgrading.** `motel cabin images` needs one new line in the sudoers rule. After upgrading the `motel` package, the
  operator runs `sudo uvx --from motel motel install --upgrade` to install the new desk and rule. Until then `--image` and
  `images` fail with a message that says so, and no cabin is started. `motel doctor` reports a rule that differs.
- **Cost.** A snapshot is billed by the cloud for its size and the time it exists. Delete it with
  `sudo motel-desk tour image rm ID` when you no longer need it. Software in an image does not update itself: bake again to
  change it.

`run` first checks the cap against the live provider price, so it can refuse with exit 3 before creating
anything. The sync honours `.gitignore`, so ignored secrets such as `.env` files are not copied. Untracked
files that are *not* ignored are copied, so check `git status` first.

`ls`, `status` and `doctor` are rate-limited for ordinary users: they are served a copy of the provider
inventory at most 30 seconds old (any checkin, extend, checkout or reaper pass refreshes it), so they cannot use
up the reaper's API budget. Root, and the reaper, always ask the provider. `doctor` shows only your own cabins;
root sees all. The default server type is the first one in `types`. The owner label is your account name when
it is a valid label (starts and ends with a letter or digit) and `uid-<number>` otherwise.

Exit codes follow the core: 0 ok, 2 usage, 3 refused (policy, cap, "no vacancy"), 4 busy, 5 error,
6 guard refused, 7 not found, 8 not set up, 9 incomplete (cleanup pending).

`motel cabin run` exits as follows:

| situation | exit status |
|---|---|
| checkin refused or failed (no vacancy, not set up, provider error) | the desk's code (3, 8, 5, ...); nothing ran and no cabin was created |
| the sync (rsync) failed | rsync's exit status; the cabin is still checked out |
| the command ran, checkout released the cabin | the command's exit status |
| the command ran, checkout ended `cleanup_pending` (the desk exited 9 and the cabin is `cleanup_pending`) | the command's exit status, with a `WARNING ... NOT complete` line on stderr and `released: false` in `--json`. The cabin is already shut out and only the provider is slow; it keeps billing until the reaper finishes it. Agents must check `released` (SDK: `CabinRun.released`; MCP: the warning is kept in the result) |
| checkout was busy (the reaper held the cabin's lock for a short activity check) | retried with a short backoff for up to 90 seconds, then as the next row |
| checkout failed any other way (busy past the wait, error, guard refused, not found) | the desk's code, with a message saying the cabin may still be ACTIVE and billing until it is idle or expires, and `Try: motel cabin checkout ID`. The command's own status is named in the message |

`motel cabin checkout` exits 9 whenever cleanup is still pending.

## Cleanup does not need a working config

`checkout` and `reap` read only the stored lease records, the token and whatever keys of `[cabins]` are still
valid. A missing `[cabins]` table, `enabled = false`, or an invalid unrelated key never stops cleanup of an
existing lease; new checkins stay refused. Deleting a lease's own resources proves identity only (id, name,
labels, nonce, no provider protection); exposure checks (firewall applied, attached volumes) apply to create,
bootstrap and extend, never to delete.

`enabled = false` means "no new checkins" and nothing more: the installer keeps the cabin reaper (its own timer) and the user verbs `cabin ls`, `status` and `checkout` for as long as a `[cabins]` table or any cabin state exists. A bad key in `[cabins]` never stops the rooms; the doctor reports it.

If a tailnet node can be neither deleted nor proven absent, the lease stays `cleanup_pending`. After checking
the tailnet admin console, an operator can run `sudo motel-desk cabin resolve LEASE --forget-node`; it records
the override in the guest book, marks only the node item done and finishes the rest of the cleanup.

## Lifecycle

```
intent -> creating_firewall -> [creating_volume] -> creating_server -> bootstrapping -> active
any state -> cleanup_pending -> released
```

- **Intent first.** The lease record (with a unique label and a 32-hex nonce) is written before any
  provider call, so a lost response is reconciled by labels, never by guessing, and a crash cannot orphan a
  resource silently.
- **Expiry.** A cabin lives until its checkout time. After the time plus a **10 minute grace** it is
  deleted, even if busy. `extend` is allowed until the grace ends. The maximum lifetime is a hard ceiling.
- **Idle.** A cabin is also deleted after 30 minutes (configurable upward) of *verified continuous idle*, at the end of the
  billed hour it is in (see [Hourly billing](#hourly-billing)):
  no admitted SSH session, no process of the cabin login (so a `nohup` build counts), no managed job, no
  container. An unreadable source, a stale sample or a sampling gap means **unknown, which is never idle**.
  The reaper asks the guest to *seal* admission atomically with the same lock the gate uses; a seal that is
  refused or unconfirmed cancels the idle cleanup. Once sealed, the cabin admits nothing new and `extend` is refused
  (`this cabin is idle and sealed; check in a new one`); it is deleted in its release window. `ls` and `status` show it as
  `sealed (idle); deleted by <release_by>`. A sealed cabin still counts toward `max_concurrent` while it waits, but it does not
  keep a new checkin out for lack of room: when a checkin (or a tour booking) would be refused for no vacancy and a sealed
  cabin exists, motel deletes the sealed cabin with the earliest `release_by` at once and admits the checkin. If that cabin's
  cleanup does not finish (the provider refuses or is slow), it stays `cleanup_pending`, still counts against the cap, and the
  checkin is refused as before. The ledger entry `idle_evicted_for_checkin` records the cabin and the caller and task that
  triggered it.
- **After bootstrap.** The guest filter drops all traffic to the provider metadata service (which serves the
  cloud-init user-data), cloud-init's local copies of the user-data are deleted, and (tailscale) the one-use
  enrollment key is revoked as soon as the node has enrolled. The provider still holds the user-data, including
  the cabin's host private key, until the server is deleted; guest root can read the host key by necessity.
  Bootstrap also creates `/etc/cloud/cloud-init.disabled`, so a reboot cannot make cloud-init lose its datasource,
  adopt a new instance id and regenerate the pinned host key.
- **Dead-man's switch.** Inside the guest a systemd timer powers the machine off once the expiry plus
  grace plus two minutes has passed. This limits what a cabin can do if the reaper never runs. A powered
  off server **still bills**: only the reaper (or `checkout`) deletes it.
- **Checkout and cleanup.** Resources are deleted independently and absence is verified by a 404 for the
  server, then the primary IP, the firewall, the volume and (tailscale) the key and node. The server delete is
  an asynchronous provider action: checkout follows it to the end (up to 2 minutes) before the primary IP, the
  firewall and the volume are touched, because the provider refuses to delete them while the server still holds
  them. A refusal that only needs time (resource in use, locked, rate limited) is retried with backoff inside a
  fixed budget of 3 minutes per pass (rate-limit refusals wait at least 10 seconds); any other error is not retried.
  The reaper uses a shorter budget per lease (60 seconds, of which 45 for the server action) and bounds a whole pass
  to about 10 minutes. Leases are taken in this class order: (0) a sealed ACTIVE lease (idle, held for its release window:
  its window is short, and a slow lease ahead of it must not make it miss the window); (1) an ACTIVE lease past its expiry
  plus grace; (2) `cleanup_pending` with its server not yet confirmed gone (billing for certain); (3) the other ACTIVE leases
  (idle candidates); (4) creations that did not finish; (5) `cleanup_pending` with the server done (only a firewall,
  address or volume is left). Inside a class the least recently touched lease goes first, and a record the reaper cannot rank
  (a malformed time) goes before all of them so that its error is reported. What a pass does not reach is listed as
  `deferred` and, being the least recently touched, is first in its class next time. The primary IP is deleted only
  by the ID recorded when the server was created, and only if it is assigned to nothing or to this cabin's own server.
  If the record lacks that provenance (no recorded address), the guard refuses with `missing primary IP provenance`:
  delete the primary IP (the message names its ID) in the provider console if it outlived its server; the next reap
  releases the lease.
  If anything is left when the budget runs out, the lease stays `cleanup_pending`, counts
  against `max_concurrent`, shows in `motel cabin ls` and is retried by the reaper. There is no restore:
  a released cabin is gone.
- **Housekeeping.** `sudo motel-desk cabin reap` (dry run) and `reap --apply` run from a timer. Run the
  dry run before trusting it. `status`/`ls` report `verified_empty` only when the provider inventory is
  complete, no lease is pending and no orphan exists.

## Hourly billing

Hetzner bills a server, its IPv4 and an extra disk **per started hour from creation**: a cabin deleted after 5 minutes costs a
full hour, and one deleted at minute 61 costs two. Deleting early inside a paid hour therefore saves nothing, and an explicit
`checkout` inside a paid hour does not refund it. The estimate in `ls`, `status` and `checkout` output counts started hours
(`est. EUR 0.0620 net for 2 started hours`; `billing.billed_hours` and `billing.paid_until` in `--json`). The provider invoice is
authoritative. The table of how every cloud bills is in [providers.md](providers.md#how-each-cloud-bills).

What the reaper does about it, for a cabin it finds idle (30 minutes of verified idle, by default):

- It **seals admission at once**, as before: no new work can start.
- It then **deletes the cabin only in the release window**: the last 10 minutes (one reaper pass) before `release_margin`
  (6 minutes by default) ahead of the next billed hour, counted from the billing start (see below). With the defaults the window
  is minute 44 to 54 of each billed hour. A cabin that goes idle at minute 31 is sealed at minute 31 and deleted by the first
  pass that finds the window open: the pass that runs between minute 44 and 54, or the one just before it when the following
  pass would come too late (from about minute 43 when passes are a minute apart). A cabin found idle inside the window, or in
  the last minutes of the hour, is deleted by that pass.
- If the **next pass would come too late** (it could fall after the window closed), it deletes now, so a sealed idle cabin is
  never carried into a new billed hour by a pass that could have prevented it. The timer runs every 10 minutes
  (`billing.REAP_INTERVAL` equals the installer's timer interval; a test keeps them equal), and a pass that starts up to a
  minute late is allowed for. `release_margin` is at least 4 minutes so a pass that starts late still has time to finish
  its deletes inside the paid hour.
- **TTL and `max_lifetime` are promises and are never extended for billing.** The cabin is not kept past its TTL, and it
  is deleted now when the next pass would come after the TTL. A busy cabin is still deleted at its TTL plus the 10 minute
  grace, wherever that falls in a paid hour.
- **Explicit `checkout` and default `cabin run` delete at once**, because you asked for it. Default `cabin run` and
  `tours` always create a fresh server per job and never share a cabin across users. For same-owner iterative work
  within a paid hour, you can opt in to per-user reuse with `cabin run --pool NAME` (see
  [Warm pools (opt-in)](#warm-pools-opt-in)).
- A cloud whose billing is not per started hour, or is not confirmed (`unknown`), gets none of this: an idle cabin is deleted as
  soon as it is sealed.

`motel-desk cabin reap` reports cabins it is holding under `held` (with `release_from` and `release_by`). Every cabin view in
`--json` carries `billing`: `{granularity, billed_hours, paid_until, release_window: {opens, closes}, source}`, and `sealed`
and `release_by` for a sealed cabin.

**The billing start** is the earliest of three times: the server's `created` time at the provider, the volume's `created`
time (a disk is billed from its own creation), and the time the create response reached motel. Taking the earliest keeps
a provider clock that runs ahead of motel's from pushing a deletion into the next billed hour. It is not exact: when the
provider's time is missing or later than the arrival time, the start used is the arrival time, which can be later than the
real creation by the API round trip (seconds), so a deletion can be that much late against the provider's own hour. A
provider time more than 5 minutes before motel started the create is not believed. A time without a time zone is ignored.
`source` says which time was used: `provider`, `received` (motel's own arrival time) or `record` (motel's start of the create,
used when the provider gave nothing usable). That billing starts "from creation" is an assumption of motel's, not something
a provider page states exactly; see [providers.md](providers.md#how-each-cloud-bills).

## Costs

You pay the provider for the time between creation and verified deletion, per the provider's
hourly billing (see above), plus a primary IPv4 and any extra disk while it exists. A cabin that failed to clean up keeps
billing: `motel cabin ls` shows it, and `cabin doctor` flags it. The price check uses the live net hourly
price of the server type, the IPv4 and the disk, against `max_eur_per_hour`. `motel cabin status` shows an
estimate; the provider invoice is authoritative.

## Honest limits

- **The guest user has sudo.** `cabin` can become root in its own cabin. The guest hardening (admission
  gate, packet filter, key-only SSH, forwarding off) protects the *controller side* and the *lease
  accounting* against accidents and ordinary mistakes. It does **not** protect against a hostile lease
  user: such a user can reconfigure or remove the guest filter, the sampler and the expiry timer inside
  their own cabin. The cloud-side controls (per-lease firewall limited to one source, egress allowlist,
  expiry labels, and a reaper that deletes from outside) are what remain in force.
- A cabin can reach the public internet (HTTP/HTTPS) and so can exfiltrate whatever you sync to it.
  Sync only what you would hand to the cabin's user.
- IPv4 only; outbound is HTTP/HTTPS and DNS only. Other protocols are blocked by the cloud firewall.
- The host key is pinned (generated per lease, installed through cloud-init, recorded locally). The
  cloud-init user-data holds the cabin's own host private key (and, in tailscale mode, a one-use key); it
  is deleted from the guest at the end of bootstrap, but the provider keeps the user-data for the life of the
  server.
- One project, one host: motel assumes it is the only controller using the cabin project.
- Experimental: the live harness covers the `ssh-allowlist` mode on one image and server type. Other images,
  server types and locations, the metadata IPv6 parsing and the tailnet policy analysis have only been checked against
  fakes.
- A cabin is created in one location (`cabins.location`). When the provider has no capacity for the server type there
  (Hetzner answers 412 `resource_unavailable`), the checkin fails; choose another server type or location and retry.

## What the front desk allows

The sudoers rule for ordinary users must pin **each** user verb on the cabin desk, one line per verb:
`motel-desk cabin checkin *`, `... ls *`, `... status *`, `... extend *`, `... checkout *`, `... doctor *`, `... images *`. It must
never be `motel-desk cabin *`: `setup`, `reap` and `resolve` are operator commands (root, or an administrator
using `sudo motel-desk cabin ...`), and the housekeeping timer calls `motel-desk cabin reap --apply`.
`motel.cabins.desk.USER_VERBS` is the list to render from.
