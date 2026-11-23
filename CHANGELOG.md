# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.0] - 2026-11-23

### Added
- Opt-in, per-user warm cabin pooling (experimental, client-side only — no changes to `motel-desk`, sudoers or tours):
  - `motel cabin run --pool NAME [--type T] [--image IMG] [--for TTL] [--disk SIZE] [--json] -- CMD...`: reuses a
    matching idle cabin owned by the same Unix user within its already-paid hour (syncing `~/work` with `rsync --delete`,
    verifying live `active`, unsealed status with at least 8 minutes before `min(expires_at, billing.release_window.opens)`),
    or checks in a new cabin (`--task pool-NAME`, default `--for 1h`) and leaves it warm for `motel-desk`'s idle reaper
    to delete before the next billed hour starts. State and per-cabin `fcntl.flock` lockfiles live under
    `~/.local/state/motel/pools/<name>/` (`0700` dirs, `0600` files). `--json` adds `pool` and `pool_hit`. Measured live
    on Hetzner `cpx42` (`fsn1`): `64.8 s` cold miss to `1.24 s` warm hit (**52× faster** inside the same paid hour).
  - `motel cabin pool ls [NAME] [--json]` lists pooled cabins with live state, busy/idle lock status, type, image,
    `paid_until` and `release_window`, pruning released entries.
  - `motel cabin pool drain [NAME] [--json]` checks out idle pooled cabins while skipping busy locked cabins with a note.
  - Default `motel cabin run` and `motel tour` are unchanged and still allocate a fresh server per job.
- Live 3-run CPython `3.13.9` PGO + full test suite benchmarks for dedicated Hetzner `CCX33` (`8 vCPU / 32 GiB`: `4.71 min/round`, `1.7%` spread, 17% faster / 4× RAM / 2.5× lower cost per round at full hour vs E2B) and 4-core `CPX32` (`3/3 ok`, 3.9× lower cost at full hour vs Daytona), plus refreshed `docs/assets/benchmarks.png` and `docs/benchmarks.md`.

## [0.1.0] - 2026-11-21

First release. motel gives an agent disk and servers by the hour: private scratch directories on temporary cloud
volumes (rooms), disposable servers (cabins) and swarms of bounded agent jobs on fresh cabins (tours). A root-owned
front desk holds the cloud token, so the agent never sees it.

### Added

#### Launch additions (baked images, Omnigent setup, receipts, benchmarks)
- `motel omnigent setup | status | mcp-config` (experimental): one command that checks cabins and Omnigent, installs motel
  into Omnigent's environment when the provider is missing there, and writes the `sandbox:` block into the Omnigent server
  config (backup first, atomic, idempotent, other keys and comments kept). It never restarts the server. `status` is the
  same checks, read-only. `mcp-config` prints the MCP snippet for `motel mcp --with-tours`. See
  [docs/omnigent.md](docs/omnigent.md) and [docs/cli-json.md](docs/cli-json.md).
- Baked images for cabins and the Omnigent provider (experimental): `motel cabin checkin|run --image NAME` starts a cabin from an
  image the operator baked on this host, `motel cabin images` lists them, and the desk accepts only this host's recorded, labelled
  snapshots. The provider takes `image:` in its `motel:` block. `motel omnigent bake-script` prints the bake script,
  `motel omnigent setup --image NAME` writes `image` and `bootstrap: none`, and `status` checks that the image exists. Run
  `motel install --upgrade` to add the one new sudoers line. See [docs/cabins.md](docs/cabins.md#start-from-a-baked-image) and
  [docs/omnigent.md](docs/omnigent.md#faster-first-session-bake-an-image).
- Tour spec key `model` (experimental), in `[defaults]` or per guest, for the `claude` and `codex` adapters: it is passed as
  `--model` to the agent CLI. A model the concierge does not price is billed at its highest rate, so it cannot get past a
  budget. It shows in `tour status` and the result record only when set. See [docs/tours.md](docs/tours.md).
- Receipts on `cabin checkout`, `cabin run` and `tour`, grouped help, first-run hints, offline cabin demo (`motel try`),
  and measured 3-run CPython benchmarks & cost calculator (`bench/`, [docs/benchmarks.md](docs/benchmarks.md), [docs/cost.md](docs/cost.md)).

#### Rooms
- `motel checkin / ls / status / extend / checkout / restore / doctor`: a private `0700` directory on a temporary cloud
  volume attached to your own host, with a size and a checkout time. Stdout carries only the path (or JSON).
- Floors (shared volumes) hold many rooms and are packed, grown online from real usage and retired when empty;
  `--suite` gives a room a floor of its own.
- Lifecycle: active, grace (6 h), lost & found (24 h, `restore` brings a room back), purged. The busy check waits and
  reports; it never kills a process.
- Caps (floors, GB per floor, total GB, projected monthly EUR) are reserved before any create or resize; a refusal is
  exit 3, before any cloud call.
- Ownership guard: before every provider write the desk re-reads the volume and requires a root-owned creation record,
  motel's labels and an id outside the never-touch set (every volume that existed at `setup`). Intent records are saved
  before a create, lost create responses are reconciled by label, and orphans are reported, never adopted.
- Housekeeping (`reap`, every 10 minutes by systemd timer or cron) expires rooms, empties lost & found and retires
  floors; it survives deep trees, hung mounts and bad rooms.

#### Cabins
- `motel cabin run | checkin | ls | status | extend | checkout | doctor | ssh`: a fresh Hetzner Cloud server with its own
  per-lease firewall (SSH from the host only, outbound HTTP/HTTPS and DNS) and an optional extra disk (`--disk`).
  `cabin run -- CMD` syncs a git checkout, runs the command, returns its exit status and deletes the cabin. Checkout is
  final: there is no restore.
- Access by SSH allowlist (default) or, opt-in, by Tailscale. The host key is generated per lease and pinned.
- Checkout waits for the provider and deletes the server, its primary IP and its firewall, retrying "in use", locked and
  rate-limited refusals with backoff. Anything left stays `cleanup_pending` and is finished by the reaper.
- Caps on concurrent cabins, price per hour, lifetime and disk size; an idle timeout and a TTL that the reaper enforces
  from outside the cabin.

#### Tours
- `motel tour start | ls | status | wait | result | logs | fetch | cancel`: N bounded, non-interactive agent jobs
  ("guests"), each on its own fresh cabin that is deleted afterwards. Experimental, off by default (`[tours] enabled`).
- The concierge, a credential proxy, keeps real API keys away from guests: a guest holds a per-attempt placeholder, the
  concierge swaps in the real key, applies a request policy and meters the cost against `budget_usd`. A guest's egress
  is limited to the concierge and an operator allowlist. Opt-in own-login mode passes a Claude token you made over ssh
  stdin only (not metered, not revocable per job; read the risks in docs/tours.md).
- Output is validated and size-capped. `tour fetch` imports a guest's commits as branch `motel/<tour>/<guest>` and never
  checks out or merges. Retries happen once, and only before the guest command starts. Cancel revokes, stops, collects
  and deletes, and also interrupts a cabin that is still booting.
- Boot diagnostics: a guest stuck in booking records why (guest boot stage, ssh and port 22 probes, cloud-init verdict, last
  log lines, redacted), shown by `tour status` and `tour logs --stream boot`.
- Operator verbs `tour reap | bake | image | own-login`; adapters for `claude`, `codex` and plain commands.

#### Billing
- One table (`motel.billing`) of how each provider bills servers and volumes (per started hour, per second, unknown), each
  entry with its official source. Hetzner bills per started hour from creation, so deleting early inside a paid hour saves
  nothing.
- Paid-hour-aware release: an idle cabin is sealed at once and deleted in a release window just before the next billed
  hour (`cabins.release_margin`, 6 minutes by default); an empty shared floor waits for its window
  (`rooms.release_margin`). TTLs, explicit checkouts, `checkout --now` data purges and clouds without hourly billing are
  never delayed. A sealed cabin is deleted at once when a checkin would otherwise be refused for no vacancy.
- Cost estimates in `status`, `ls`, `checkout` and `cabin run` count started hours; JSON adds `billing` (`billed_hours`,
  `paid_until`, `release_window`). The billing clock starts at the earliest of the provider's created time and the time the
  create response arrived.

#### Providers
- Hetzner Cloud (rooms and cabins): live-verified by the live harness against real projects (see Known limitations).
- Beta, written against provider documentation and fake APIs only: DigitalOcean, Linode, Vultr, Scaleway, AWS EBS,
  Google Persistent Disk and Azure Managed Disks (rooms only; cabins run on Hetzner). Clouds that hand out blank volumes get an ext4
  filesystem only after positive proof the device is blank and the right one; AWS, GCP and Azure need no token (instance
  identity). See docs/providers.md and docs/providers/.
- `local` (loop files, for testing) and an in-memory fake with failure injection for writing adapters.
- One provider registry: `host.provider`, `setup <name>`, the doctor and the installer read it.

#### Integrations and SDK
- Python SDK (`motel.Motel`): rooms, cabins (`cabin_run` reports `released` and `kept`) and tours as typed objects;
  failing doctor checks are data, not exceptions. Standard library only.
- MCP server (`motel mcp`, `--with-cabins`, `--with-tours`): typed tools that call `motel ... --json` as the agent's user,
  so they add no privilege.
- Agent skill (`motel skill print | path | install`, or `npx skills add vicennial/motel`).
- GitHub Action: run a CI job on a fresh cabin (`cabin-run`) or lease a room (`room`), and always check it out.
  On GitHub-hosted runners it removes group and other write permission from the install directories first
  (`/usr/local/bin` is mode 777 there), because the installer refuses root-run code under a writable directory.
- Optional Omnigent sandbox provider: `sandbox.provider: motel` in the server config (or `omnigent sandbox create --provider
  motel` from an Omnigent source checkout) runs a session's host on a cabin. Credentials pass by variable name over ssh stdin. motel's core never imports Omnigent.
- JSON output on every command with stable exit codes (0 and 2 to 9); see docs/cli-json.md.

#### Install and onboarding
- `uvx motel try`: a guided, offline tour of one full stay (no root, no cloud, no config).
- `sudo motel install` from a wheel or a root-owned clone (or `install.sh`): one narrow sudo rule, the root-owned front
  desk, a systemd timer or cron for housekeeping, `--prefix` and `--dry-run`, `--upgrade`. Tours add a concierge user and
  units only when enabled.
- `sudo motel-desk setup hetzner` (hidden token prompt, or `--token-file PATH` for automation) and `setup local`;
  `motel doctor` checks the installation.
- A live harness (`tests/live/run_live.py`) that runs against dedicated, empty cloud projects and deletes everything it made.

### Known limitations
- A cabin is created in one configured location (`cabins.location`, by default the host's own). When the provider has no
  capacity for the server type there (Hetzner answers 412 `resource_unavailable`), the checkin fails, and a tour retry tries
  the same location again. Pick another server type or location in `[cabins]` and retry.
- Hetzner is live-verified for rooms, cabins, tours whose guests run plain commands, and the Omnigent provider (0.21.0,
  server-managed sessions). The live harness calls no model: agent guests with real API keys, own-login, the concierge
  against a real upstream and the GitHub Action's `cabin-run` on a real cloud are tested against fakes only (CI does run
  the Action's install, `setup local` and `motel doctor` on a real GitHub runner). Cabins, tours and the Omnigent provider are experimental.
- The other clouds are beta: nobody has run them against a real account yet. Treat the first run as a supervised test on a
  scratch project.
- Cabins run on Hetzner Cloud whatever the room provider is, over IPv4 only.
- motel is not a sandbox and does not protect against a hostile root user on the host. On Hetzner the token sees the whole
  project of the host's server, so use a project that holds little else.
- Room size is a packing hint: a room can fill its floor (quotas are not implemented). The busy check does not see into
  overlay or other stacked filesystems whose layers sit inside a room.
- The front desk needs Linux. `motel try` is plain Python and is tested on Linux only.
- Monthly caps are estimates from size times price, not limits the provider enforces.

[Unreleased]: https://github.com/vicennial/motel/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/vicennial/motel/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/vicennial/motel/releases/tag/v0.1.0
