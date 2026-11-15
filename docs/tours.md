# Tours: a swarm of agent jobs on disposable servers

A **tour** runs N bounded, non-interactive agent jobs ("guests"). Each guest gets its own fresh [cabin](cabins.md)
(a disposable server), works on a copy of your git repository, hands back commits and files, and then its server is
deleted. Real API keys never reach a guest: they stay with the [concierge](concierge.md), which swaps a per-guest
placeholder for the real key and meters the cost.

Status: experimental, like cabins. Hetzner only. The live harness runs tours of command guests on real cabins (no model, no real key); agent guests with real keys are tested against fakes only. Read [What is not protected](#what-is-not-protected).

Other parts of the tours feature:
[the guest side](tours-guest.md) (runner, adapters, egress filter, image bake) and
[the concierge](concierge.md) (credential proxy). The binding design contract is
[design/tours-contract.md](design/tours-contract.md).

| Word | Meaning |
|---|---|
| **Tour** | One submission. It has an owner (you, the Unix user who ran `motel tour start`) and N guests. |
| **Guest** | One job: a task in, a result record out. |
| **Attempt** | One run of a guest on one fresh cabin. A retry is a new attempt on a new cabin. |
| **Binding** | A named upstream API key held by the concierge, for example `anthropic`. |

## First tour

A tour of one command guest needs no model and no API key. It needs [cabins](cabins.md#first-cabin) working, with tours on
(`sudo uvx --from motel motel install --cabins --tours --users $USER` on a new host, or see
[Operator setup](#operator-setup)). In your git repository:

```bash
echo 'guests = [{ id = "check", adapter = "command", argv = ["make", "test"], secrets = [], budget_usd = 0 }]' > tour.toml
T=$(motel tour start tour.toml)     # prints the tour id
motel tour wait "$T"                # blocks until the guest finishes
motel tour result "$T" check        # what motel verified, and what the guest claimed
```

For agent guests (`claude`, `codex`) with a budget and commits to fetch, read on.

## Quick start

`tour.toml`, in the root of your repository:

```toml
[tour]
name = "fix-flaky-tests"

[defaults]
adapter = "claude"            # or "codex", or "command"
ttl = "45m"                   # wall clock per guest
secrets = ["anthropic"]       # concierge bindings the guest may use
budget_usd = 1.50             # model spend per guest, enforced by the concierge
egress = ["github.com"]       # hosts the guest may reach (must be allowed by the operator)
artifacts = ["reports/*.json"]

[[guests]]
id = "fix-auth"
prompt = "Fix the failing tests in tests/auth. Do not change the public API."

[[guests]]
id = "fix-billing"
prompt = "Fix the failing tests in tests/billing."
base = "origin/main"          # commit, branch or tag to start from (default HEAD)

[[guests]]
id = "lint"
adapter = "command"
argv = ["make", "lint"]
secrets = []
budget_usd = 0
```

```console
$ motel tour start tour.toml
motel: tour t-3fa91c0b72 started · 3 guest(s). Try: motel tour wait t-3fa91c0b72
t-3fa91c0b72
$ motel tour wait t-3fa91c0b72            # blocks until all guests finish (exit 9 on --timeout)
$ motel tour result t-3fa91c0b72 fix-auth # what motel verified, and, separately, what the guest claimed
$ motel tour fetch t-3fa91c0b72 fix-auth  # import the commits as branch motel/t-3fa91c0b72/fix-auth
$ git log motel/t-3fa91c0b72/fix-auth     # review before you merge anything
```

`fetch` imports a branch and nothing else: it never checks out, merges or rebases, and it refuses to overwrite a branch
that has other history. Guest output is untrusted. Review the diff like a pull request from a stranger.

## The spec

TOML or JSON. Unknown keys are errors. `[defaults]` apply to every guest; a guest can override them.

| Key | Where | Meaning |
|---|---|---|
| `tour.name` | tour | Label: 1-64 characters from letters, digits, space, `.`, `_`, `-`; it starts with a letter or digit. |
| `id` | guest | 1-32 characters of `a-z`, `0-9`, `-`, starting and ending with a letter or digit. Unique in the tour. |
| `prompt` | guest | The task, for `claude` and `codex`. At most 64 KiB (65,536 bytes). |
| `argv` | guest | The command, for the `command` adapter (a list; no shell). |
| `adapter` | both | `claude` (default), `codex`, `command`. |
| `repo`, `base`, `overlay` | guest | Repository directory (default: the spec file's directory), start point (default `HEAD`), and an optional patch applied on top as its own commit. |
| `ttl` | both | Wall-clock limit per guest, such as `45m`. Default 30 minutes, at most the operator's `max_ttl`. |
| `secrets` | both | Concierge bindings, such as `["anthropic"]`. Not API keys: names. |
| `budget_usd` | both | Model spend per guest. Required above zero when `secrets` is set. |
| `egress` | both | Hosts the guest may reach. At most 64 hosts and 1700 bytes of names in all (the guest's firewall profile has to fit in the cabin's start-up data); `api.anthropic.com` counts when `auth = "own-login"`. A longer list is refused when you start the tour (exit 3), before any cabin exists. Exact names, no wildcards. |
| `env` | both | Plain environment variables for the job. Names that look like credentials or change `PATH`, proxies or base URLs are refused. |
| `artifacts` | both | Glob patterns of files to bring back, relative to the repository. Regular files only. |
| `server_type`, `image` | both | A cabin type, and a baked image name or id (see [Operator setup](#operator-setup)). |
| `max_turns` | both | Turn limit for the `claude` adapter (1-500). |
| `max_output_tokens` | both | Largest model reply, 256-128000 (the claude adapter sets `CLAUDE_CODE_MAX_OUTPUT_TOKENS`). Omit it to keep the guest's own default (8000). |
| `auth` | both | `concierge` (default) or `own-login` (see [below](#using-your-own-claude-login-opt-in)). |

A tour can only ask for less than the operator's ceilings. Nothing in a spec can raise a limit, and the front desk
checks every field again, so a hand-made request gets the same answer. A refusal exits 3 and names the ceiling.
Do not retry it in a loop.

## Commands

All unprivileged. Each is one pinned `sudo` rule for the front desk; add `--json` for machine-readable output.

| Command | What it does |
|---|---|
| `motel tour start FILE\|-` | Bundle your repository as you, hand it to the desk, start the supervisor, print the tour id. |
| `motel tour ls` | Your tours. |
| `motel tour status ID` | Status of each guest, cleanup (are the servers deleted?), spend. A guest that is booking or never ran shows its last attempt's phase and why (see [A guest stuck in booking](#a-guest-stuck-in-booking)). |
| `motel tour wait ID [--any\|--all] [--timeout S]` | Block. Exit 0 when satisfied, **9** when the timeout came first (the tour keeps running). |
| `motel tour result ID GUEST` | The result record (below). |
| `motel tour logs ID GUEST [--stream stdout\|stderr\|runner\|boot] [--tail N]` | Capped text of the guest's output. Untrusted. A guest that never ran its command gets its boot report instead (`--stream boot` always): per attempt, the phase reached, what failed, and what the controller last saw while the cabin was booting. |
| `motel tour fetch ID GUEST [--artifacts DIR]` | Import the commits as a branch; save declared artifacts to `DIR`. Checks every file against its recorded digest. |
| `motel tour cancel ID` | Revoke placeholders, stop the guests, collect what can be, delete every server. |

Operator commands (root): `tour reap [--apply]`, `tour bake NAME --script FILE [--type T]`, `tour image ls|rm ID`,
`tour own-login set --stdin|status|clear`. The JSON shapes are in [cli-json.md](cli-json.md).

You can read results only through these commands: the tour state lives in a root-only directory, and the desk answers
"not found" to anyone but the owner (and root).

## A guest stuck in booking

A cabin gets 20 minutes to finish its bootstrap. While the supervisor waits, every attempt record (and `tour status --json`,
under `attempt_list[].boot`) keeps what the controller saw on its last poll:

- `channel`: the guest channel's last error: the exit status of the ssh client (`rc`), the last informative line of its stderr (host-key
  warning boxes and the known-hosts notice skipped; cleaned, paths and secret-looking strings replaced) and, about once a minute, what a plain TCP connect to port 22 sees (`open (SSH-2.0-...)`,
  `refused`, `timeout`). A closed port, a dropped packet and a refused key read differently.
- `ready.boot`: when the guest answers but its bootstrap is not done, its own report: the stage the bootstrap is in (each stage
  writes its name to `/var/lib/motel-guest/boot-stage`, so apt's output cannot hide it; the `stage:` log marker is the fallback) (`packages`, `firewall`, `access`, `disk`, `tour-user`, `tour-resolver`, `tour-firewall`, `tour-egress`, `finish`), cloud-init's
  verdict (`running`, `done` or `error`) and the last line of `/var/log/cabin-bootstrap.log`, which names a failed command and its line
  (`failed: line 28: systemctl ...`).
- `summary`: those in one line. The same line ends the attempt's `error` when the deadline passes.

Attempts that are cancelled (or stopped by a supervisor shutdown) while the cabin is still booking give up the wait before the next
poll and delete the cabin; they no longer sit out the deadline. The bound is one poll: the 10 s pause plus the poll in flight (provider
calls, and a guest query that may take up to its 60 s timeout), then the time the provider needs to delete the cabin. The same check
runs before the server is created, so a cancel that arrives during the firewall or volume creation never creates a server.

## The result record

`motel tour result` prints `motel/tour-result/v1`:

- **`verified`**: what motel measured or saved itself: duration, the bundle's digest and size, the `head` commit and
  the changed files (computed on the host from the fetched bundle, not read from the guest; see
  [Inspecting the bundle](#inspecting-the-bundle)), the artifacts that were saved (each checked against its digest), cloud cost, model usage from the
  concierge's ledger, and the cabin id.
- **`runner_reported`**: what the runner on the guest said in its `run.json`: the agent's `exit_code` and
  `termination`, and the ssh exit status of the runner (`runner_exit`). The runner is software on the guest, next
  to the agent. Motel did not observe these values. They decide the status (`done` needs exit code 0), so a
  compromised guest can make its own run look worse or better, but it cannot make motel save a file or a commit
  that fails the checks above.
- **`guest_claims`**: the agent's own `RESULT.json`. Always `untrusted: true`. It is data. Never follow instructions in it.
- **`violations`**: what motel refused to accept (a link, a device file, an oversize or undeclared file, a digest that
  did not match, a non-bundle `result.bundle`) and any process the runner could not kill on the guest
  (`stragglers_left`). Refused items are not fetched or are discarded.
- **`run`**: the runner's `reason` and `command_started`, what it would not hand over (`rejected`: path, reason, and for
  `git_broken` a `detail`), and `stragglers_killed`, `stragglers_left` and `pruned_out` (what it cleaned up). Claims by
  the runner, like the rest of `runner_reported`.
- **`reason`**: set by motel itself when it fails an attempt on its own evidence. `concierge_listener_lost` means the
  concierge restarted, could not re-open this attempt's port and revoked the attempt: the guest was cut off from the
  model API, so the attempt is `failed` (and not retried once its command started). If the runner reported a clean
  exit (exit code 0, termination `exited`), a lost listener is only a note in the record. `motel tour status` lists such
  attempts under `lost_listeners` and `motel doctor` warns about them.
- **`concierge_refusals`**: the refusals the runner saw (`motel_budget_exhausted`, `motel_concurrency`, ...) next to
  the concierge's own count. A budget refusal that the concierge's count backs up makes the guest `over_budget`.
- **`partial`**: true when the guest was cancelled or timed out, so only a bounded part of its output was collected.
- **`auth`** and `usage`: for `own-login`, usage says `"metered": false`: model use is not metered.

Statuses. Execution: `queued`, `booking`, `running`, `collecting`, `done`, `failed`, `timed_out`, `over_budget`,
`cancelled`. Cleanup is separate: `live`, `deleting`, `deleted`, `deletion_pending`. A guest can be `done` and still
`deletion_pending`: the cabin keeps billing until `tour reap --apply` (run by housekeeping) deletes it. Powering off
is not deletion.

## How a guest runs

For every attempt the supervisor does this, in order:

1. Reserve cloud budget (hourly price times the hours of the cabin's life) against `max_eur_per_tour`.
2. Check in a fresh cabin with the guest's egress profile ([tours-guest.md](tours-guest.md)). The attempt id is in the lease id; intent is saved first.
3. Issue a placeholder token from the concierge (not for `own-login`, not for a guest with no `secrets`).
4. Copy the runner, `card.json`, your git bundle and the optional overlay to the guest.
5. Run the runner as the unprivileged `guest` user over the cabin's unprivileged ssh, with a reverse forward to the
   concierge. A preflight with the same forward comes first, so a forward failure is a retryable pre-start failure.
6. Collect `out/` with size caps and validate it: safe relative paths, regular files only, declared and requested
   files only, digests checked.
7. Revoke the placeholder, check the cabin out (deleted), settle cost and usage.

A guest is retried at most once, on a new cabin, and only for a failure the engine itself saw before it launched the
runner: a failed booking, copy, concierge placeholder or forward preflight. The engine saves the start time before it
launches the runner. From then on the attempt is never run again, whatever the guest's `run.json` says (for example
`command_started: false`): a guest could write that to get a second run with a fresh budget. A cabin is never reused.
The guest's processes are stopped before every collection, so nothing still runs while motel reads its output. A guest
that is going to be retried shows `queued`, never a finished status, so `wait` does not report it done.

Cancel order: revoke, stop, bounded collect (4 MiB), delete. If the supervisor has died, the desk finishes the job
itself. `tour reap --apply` restarts dead supervisors (up to three times), cancels hopeless tours, retries pending
deletions and revocations, and removes finished tours after 14 days. When systemd stops the supervisor (SIGTERM), it
tears down the running attempts first (revoke, collect, delete); the tour stays `running` and the next supervisor
carries on with the guests that did not start. A stop before a guest's command started does not use the guest's one
retry, and the restart after it does not count toward the three. The copies of your repository bundle that the desk staged are deleted
as soon as the tour ends, and `reap` removes any that remain.

Per-user ceilings (exit 3 when exceeded): `max_active_tours_per_user` (default 3 unfinished tours) and
`max_staged_mb_per_user` (default 1024 MiB of staged files). A start is also refused when fewer than the staged bytes
plus 2 GiB of disk would remain free. The checks and the staging are serialised by a lock per user and one for the host,
so concurrent starts cannot both pass a ceiling.

## Operator setup

Tours need cabins and are off until you turn them on. `sudo motel install --tours` writes a config with `[tours]`
enabled, or add the table to `/etc/motel/motel.toml` and run `sudo motel install --upgrade`. The installer then:

- adds one pinned sudoers rule per user verb (never `tour *`);
- creates the `motel-concierge` user, `/etc/motel/llm-keys` (`root:motel-concierge 0750`) and the concierge units,
  and enables them;
- adds `motel-tours-housekeeping.timer`, which runs `tour reap --apply` in its own unit with its own timeout (a hung
  rooms or cabins pass never delays it). If you turn tours off, the timer stays while `[tours]` is in the config or tour state exists (unfinished
  tours bill by the hour), as the cabin reaper does (see "Turning tours off" below);
- creates the root-only state directory `/var/lib/motel/tours` and `/etc/motel/credentials/own-login`.

### Turning tours off, and tours without cabins

`enabled = false` in `[tours]` works like it does for cabins: it stops new work and keeps what exists safe.

- `tour start` is refused, and the sudoers rule loses the `start` line. Users keep `ls`, `status`, `wait`, `result`, `fetch`, `logs` and
  `cancel`. The concierge units and the tour reaper stay installed while `[tours]` is in the config or tour state exists. With no
  `[tours]` table and no tour state the next install removes the tour reaper, and the tour verbs disappear from the rule.
- A running supervisor asks the config file before it starts each attempt (and again when a slot frees up). A guest that has not
  booked a cabin yet ends `cancelled` with the reason `tours_disabled`: nothing is booked, no token is issued, the result record says
  why. A missing `motel.toml`, a missing `[tours]` table and `enabled = false` all count as disabled. The same happens with the
  reason `cabins_disabled` when `[tours]` is enabled but `[cabins]` is not `enabled = true`, because tours run on cabins. This
  includes the retry of a guest whose first attempt failed before its command started. Attempts that already hold a cabin
  finish normally. A config that cannot be parsed, or cannot be read for a reason other than "no such file" (such as a permission
  error), does not cancel anything: a typo or a transient error must not drop queued work, and `motel doctor` reports it.
- **A guest cancelled with `tours_disabled` or `cabins_disabled` stays cancelled.** Turning tours (or cabins) back on does not
  bring it back to the queue, and the supervisor does not retry it. The tour ends with those guests cancelled, and users must start
  them again with a new `tour start`.
- Tours run on cabins. If `[tours]` is enabled but `[cabins]` is not, `sudo motel install --upgrade` does not fail: it logs a note and
  installs tours in the same cleanup mode, and `motel doctor` shows a `tours have cabins` warning. A brand-new install that asks for
  `--tours` without `--cabins` is still refused, because it would write a config that cannot work.
- `motel doctor` takes the raw `enabled` flag, like the installer, so an invalid `[tours]` table is reported once ("[tours] is valid")
  and not again as a sudoers mismatch.

### Upgrading the concierge units

`sudo motel install --upgrade` compares both concierge units with what the installer would write now and rewrites each one that is
missing or different, not only on a first install. A unit from an older version can lack `FileDescriptorStoreMax=` or the
`MOTEL_FDSTORE_MAX` marker, and the service refuses to start without them. After the writes it runs `systemctl daemon-reload`, then
`systemctl restart motel-concierge.service` when the service unit was replaced, then `systemctl enable --now` for the socket and the
service. A re-run that changes nothing writes nothing and only repeats the `enable`. The installer then reads the service unit
back and fails with a message if it has no or too small a store or no marker. It picks the unit shape from `systemctl --version`;
if the version cannot be read it logs a warning and writes the shape for systemd older than 254. `--dry-run` prints the plan
(`would run: ...`), and `--no-systemd` writes the files and prints the commands for you to run.

**Upgrade between tours.** The first upgrade from a unit that has no working store restarts the service with an empty store, so
every live attempt ends once as `not_preserved` (the installer logs a note when this applies). Later upgrades keep the stored
listeners across the restart. A changed socket unit is reloaded but not restarted: restart `motel-concierge.socket` yourself
when you want the change now (it recreates the admin socket).

### Removing

Stop and disable `motel-concierge.service` and `motel-concierge.socket`, `motel-housekeeping.timer` and
`motel-tours-housekeeping.timer`, then delete the units. Also run:

```
sudo systemctl clean --what=fdstore motel-concierge
```

This drops the descriptors systemd still holds for the service (each is a listening port of a live attempt). Without it a
stopped service on systemd 254 or newer keeps them, because the unit sets `FileDescriptorStorePreserve=yes`.

Put each API key in `/etc/motel/llm-keys/NAME.key` (`root:motel-concierge 0640`). The concierge cannot read
`/etc/motel/credentials`. Tour guests need cabins with `access = "ssh-allowlist"`; tailscale cabins are refused,
because the guest's egress filter would cut the tailnet.

```toml
[tours]
enabled = true
max_concurrent_guests = 4       # per host, across tours
max_guests_per_tour = 20
max_eur_per_tour = 5.0          # cloud spend reserved before any server is created
max_usd_model_per_guest = 2.0   # concierge budget per attempt
max_ttl = "4h"                  # per attempt wall clock
max_result_mb = 64
max_active_tours_per_user = 3   # unfinished tours of one user
max_staged_mb_per_user = 1024   # staged repository bundles of one user's unfinished tours
egress_allow = ["github.com", "pypi.org", "files.pythonhosted.org"]   # tours can only pick from these
default_image = "ubuntu-24.04"

[tours.bindings.anthropic]
upstream = "https://api.anthropic.com"
auth = "x-api-key"
key_file = "/etc/motel/llm-keys/anthropic.key"
paths = ["/v1/messages", "/v1/messages/count_tokens"]
profile = "claude-code"         # the beta headers `claude -p` sends; a binding with no profile allows none
```

The concierge opens one loopback listener per attempt (ports from `concierge_port_range`, default 17800-17999, at most 512 ports) and the
engine forwards the guest's fixed `concierge_port` to it, so a revoked attempt gets "connection refused". The `codex`
adapter's default model, `gpt-6.1-sol`, has no built-in price: add a `pricing."gpt-6.1-sol"` entry to its binding
([concierge.md](concierge.md#pricing)) or its calls are billed at the high default rate and hit the budget early.

A bad `[tours]` table does not stop rooms, `cancel`, `reap` or the cleanup of tours that already run (they fall back
to built-in defaults); it only stops `tour start`, and `motel doctor` shows the problem.

Operator verbs are for root and for an administrator who is not in `allow_users`. A user in `allow_users` who reaches the
desk through sudo is still a user: the sudoers rule never offers the operator verbs, and the desk checks again. Root
and such an administrator (who need not be in `allow_users`) see every tour with `ls --all` and may run `status`,
`wait`, `result`, `logs` and `cancel` on any tour. `start` and `fetch` stay with tour users. Nobody else sees other users' tours.

All keys, the binding options, and the `[tours.own_login]` table: [configuration.md](configuration.md) and
[concierge.md](concierge.md). A baked image (`sudo motel-desk tour bake NAME --script setup.sh`) holds your agent CLIs;
`tour image ls|rm` manages them: `ls` shows `recorded` for each image (true when this host baked it), and `rm` only
removes recorded images. Users can also start an ordinary cabin from these images: see [Start from a baked image](cabins.md#start-from-a-baked-image). A bake is refused on a tailscale host. `motel doctor` checks the sudoers rule and warns while own-login is enabled.

## Inspecting the bundle

Every result bundle is read by git on the host before motel keeps it, because git parses data the guest wrote. That
git never runs as root:

- It runs as `motel-inspect`, a system account the installer creates when `[tours]` is enabled (no home, no login
  shell, no groups), in a per-attempt directory under `/var/lib/motel-inspect` that only that account can read. The
  directory is removed afterwards. If the account is missing, the bundle is not inspected, so it is withheld.
- It runs under hard limits: about 1 GiB of address space, 60 s of CPU and a file size limit per process, 256 open
  files, a single pack thread, a byte cap on what it prints, a cap on the size and file count of the scratch
  directory, and a total of four minutes per attempt. A git that goes over a limit is killed.
- No hooks, no system or user git config, no replace objects, and every object is checked (`fsckObjects`).
- The bundle you fetch is not the file the guest wrote. Motel rebuilds it from the scratch repository
  (`refs/heads/motel/<tour>/<guest>` minus the base), so it holds exactly the objects that were checked, under one ref.
  A guest bundle that names any ref other than `HEAD` (what the runner writes) or the branch is refused, and objects
  that no ref reaches are not passed on.
- With own-login, those objects (commits, trees, file names and file contents) are also read for the token, so a token
  committed to the repository is found even though git compresses it. Such a bundle is withheld and the guest
  `failed` with `secret_leak`. A bundle with too much new data to scan is withheld too.

`motel doctor` checks this setup (the `motel-inspect` user is not root, `/var/lib/motel-inspect` is root-owned with mode
0711, `git` and `prlimit` are installed), and `motel tour start` refuses with exit 3 before anything is staged or booked
when it is not in place; the message names `sudo motel install --upgrade`.

A scratch directory is removed when its inspection ends. If a supervisor or the desk died in the middle of one, the
directory is left behind: a restarted supervisor removes its own tour's, and `tour reap --apply` removes a stale one. It
removes only direct children of `/var/lib/motel-inspect` that are named like an attempt's scratch, are real
directories (never links), are older than 15 minutes, and belong to an attempt that is finished or that no tour knows;
the age alone never decides. Disk: the scratch directories live on the same disk as the tour state, so the free-space
check at `tour start` already sees what live scratch uses. It does not reserve room for scratch still to grow (up to
4 GiB per running inspection, at most one per running guest), so keep `min_free_after_staging_mb` above that if many
guests run at once.

Known limit: every inspection runs as the same `motel-inspect` user, so two inspections that run at the same time, for
different guests or tours, are not separated from each other by user id (each has its own directory, mode 0700, and
nothing in it is read back except the rebuilt bundle). A fresh user id for each attempt would close that and is not
built yet.

If git cannot establish the head, the changed files or the absence of the token for any reason, the bundle is
withheld with a violation and the guest is `failed`. Motel never hands over a bundle it could not inspect.

## Using your own Claude login (opt-in)

API keys through the concierge are the default and the recommended path. They have a per-job budget, are revocable,
and never leave the concierge. Own-login is an opt-in for an operator who wants jobs to run under their own Claude
plan. Read the table first.

| Risk | What it means |
|---|---|
| Stealable | The token is placed on the guest. A prompt-injected agent can read it and send it to any host the guest may reach. |
| No budget | Nothing meters or caps model use. A runaway job spends your plan's usage. |
| Long-lived | A token from `claude setup-token` stays valid for a long time. |
| Account-wide | It is not scoped to one job or one repository. |
| No revoke | motel cannot revoke it per job. `tour cancel` cannot take it back. Revoke it in your Anthropic account. The short `max_ttl` is the only bound. |

**Terms.** Subscription use must stay within your plan's terms. Anthropic's
[Claude Code legal and compliance page](https://code.claude.com/docs/en/legal-and-compliance) says plan limits "assume
ordinary, individual usage" and that developers may not "route requests through Free, Pro, Max, or Ultra plan
credentials on behalf of their users". Run own-login only on a host where every allowed user is you. For anything
shared, or any product, use the API-key path. Check the page yourself; it can change.

**motel never collects your login.** It never reads `~/.claude`, a credential store or a keychain, and never runs
`claude setup-token`. You make a token on your own machine and give it to motel yourself.

To enable it, as the operator:

```console
$ claude setup-token                                  # on your own machine; copy the token
$ sudo motel-desk tour own-login set --stdin          # paste it; stored as root:root 0600
$ sudo motel-desk tour own-login status               # present or not, and when; never the token
```

```toml
[tours.own_login]
enabled = true                  # default false
allow_users = ["alice"]         # who may use it; empty (the default) means nobody
claude_token_file = "/etc/motel/credentials/own-login/claude.token"
max_concurrent = 2              # guests using own-login at once, host-wide
max_ttl = "2h"                  # per attempt; keep it short
```

Then, in a spec: `auth = "own-login"` (per default or per guest). It works only when enabled, only with the `claude`
adapter (Codex own-login is not supported in 0.1), with no `secrets` and no `budget_usd`, within `max_ttl` and
`max_concurrent`, and only for a user named in `allow_users` (an empty list means nobody). `motel tour start` prints a one-line warning every time.

How the token travels: the supervisor reads the token file and writes it to the runner's **ssh standard input**, as
one line `token\n` (at most 4 KB). It is never in `card.json`, a command line, a file on the guest, the environment of
the ssh process, a log, the tour state or a result; motel also redacts it from fetched output. The guest's egress
allows `api.anthropic.com` and nothing else beyond your list. The result shows `auth: own-login` and model usage as
"not metered". `sudo motel-desk tour own-login clear` removes the stored token.

## From an agent

- **MCP:** `motel mcp --with-tours` adds `tour_start`, `tour_status`, `tour_wait`, `tour_result`, `tour_fetch` and
  `tour_cancel`. Results are compact (a summary and branch names), and guest text is labelled untrusted.
- **SDK:** `Motel().tour(spec)` returns a `Tour` with `as_completed()`, `results()`, `wait()`, `fetch()` and `cancel()`.
  See [sdk.md](sdk.md).
- **Skill:** [skills/motel/SKILL.md](https://github.com/vicennial/motel/blob/main/skills/motel/SKILL.md) says when a
  tour is worth it.

## What is not protected

- A guest runs untrusted work with a shell. It is a disposable server, not a sandbox for your secrets: do not put
  anything in the repository bundle you would not hand to the agent.
- The egress allowlist is by IP address, resolved at boot: a site that shares an address with an allowed host is
  reachable too. Wildcards are not supported by the guest profile.
- With the API-key path the agent holds a placeholder that works against the concierge until the attempt is revoked
  (at most its ttl). Its spend is capped by `budget_usd`.
- With own-login the agent holds your real token (see above).
- Cloud cost is estimated from the hourly price. Check the invoice. A guest reserves the started hours of its cabin's
  lifetime plus the 10 minute grace before it is booked. The reservation is a bound, not a guarantee: when the reaper (not the
  tour) deletes the cabin and a pass runs late, or the provider's clock runs behind, the cost can exceed it by one started
  hour, one hourly price per attempt.
- The live harness covers command guests only. Agent guests that call a model through the concierge are tested against
  fakes; the reverse forward needs the guest's sshd to accept it (the guest part configures that, and the cabin's
  readiness check accepts exactly that forward and nothing wider).
