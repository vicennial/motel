# Tours: the guest side

Draft section for `docs/tours.md`. The binding contract is `docs/design/tours-contract.md`; this page says what the
guest part builds on top of it and where it had to add detail.

A tour guest is one bounded, non-interactive job on a fresh cabin. The guest part has four pieces:

| Piece | Where | Runs on |
|---|---|---|
| Runner | `src/motel/tours/guest/runner.py` (+ `adapters.py` beside it) | the guest, as user `guest` |
| Adapters | `src/motel/tours/guest/adapters.py` | the guest |
| Egress profile, `guest` user, input directory | `src/motel/tours/guest/egress.py`, rendered into the cabin user data | built on the controller, applied by cloud-init |
| Image bake | `src/motel/tours/bake.py`, image methods in `src/motel/cabins/provider.py` | the controller (root) |

## The runner

`runner.py` is standard library only and written for Python 3.10+, because it runs on the guest. It needs
`adapters.py` next to it; `motel.tours.guest.runner_files()` returns both, and the controller copies them to
`/usr/local/lib/motel-tour/` (root-owned). They are not in the user data because together they exceed its size limit.
`motel.tours.guest.runner_argv()` is the command that runs it as `guest` (through `sudo` from the cabin account).

Inputs are in `/var/lib/motel-guest/in/` (root-owned, group `guest`, mode 0750): `card.json`, `input.bundle`, and an
optional `overlay.patch`. The runner:

1. validates the card (strict types, ids, sizes, reserved environment names, `max_output_tokens`);
2. unbundles the input into `~/work`, checks out `base` on `branch`, applies the overlay **as its own commit**
   (`overlay_commit` in `run.json`, so the engine can tell the overlay from the agent's work);
3. writes the agent's config files into the fixed HOME;
4. runs the adapter in its own session and process group (`setsid`) under the wall-clock limit: TERM, then KILL after
   10 seconds. A TERM/INT/HUP aimed at the runner (for example the controller's ssh dropping) stops the agent the same
   way and the runner still writes `run.json`;
5. **kills everything the agent left behind.** The agent shares the runner's user, and a child that called `setsid`
   leaves the process group, so the group kill is not enough. The runner marks itself a child subreaper (orphans
   are re-parented to it) and, after the agent ends, kills every descendant with SIGKILL, repeating until none is
   left or 10 seconds have passed; when it runs as the dedicated `guest` account it also does `kill(-1, SIGKILL)`,
   which reaches every process of that user. A process that survives the bound fails the attempt with
   `stragglers_left`, and then the runner collects nothing the agent could shape: no bundle, no artifacts, no
   `RESULT.json` (the logs and `run.json` only, with that reason). Only then does it empty `out/` (the agent may have
   written anything there, or replaced the directory by a link, or built a tree thousands of levels deep: the clean-up
   is iterative and works on directory descriptors) and write the logs;
6. keeps stdout and stderr in memory, redacts secret tokens in the stream, then keeps the first and last halves up to
   `LOG_CAP_BYTES` (1 MiB per stream, the same constant the engine fetches by; the marker in between counts against
   the cap, so a log file is never larger than `LOG_CAP_BYTES`), and writes
   `out/stdout.log` and `out/stderr.log` itself, after step 5. A token cut by the cap cannot survive, because
   redaction comes before the cut; a partial token (at least six bytes of one) at the very end of a stream is
   redacted too;
7. makes the result **in a repository of its own**: a fresh repository (empty template: no hooks, no `info/`; fixed
   configuration; replace refs and lazy fetching off) that gets the controller's input bundle and then the agent's
   `HEAD` (fetched with object checks on). The agent's repository and its configuration are never used again. In it,
   under the agent's work tree as working directory, the runner commits whatever is left under the bot identity
   `motel guest <guest@motel.invalid>` (FIFOs and sockets are removed first, because git blocks on a FIFO),
   computes `changed_files` (`--no-renames`, so a rename lists both paths), scans the raw bytes of every object in
   `base..HEAD` for the secret tokens (see below), and writes `out/result.bundle` (`base..HEAD`) from that repository, so
   the bundle holds exactly what was scanned;
8. copies the declared artifacts to `out/artifacts/` and the agent's `RESULT.json`, removes from `out/` everything it
   did not write itself, and writes `out/run.json` last and atomically.

The runner's exit status is 0 whenever `run.json` was written, whatever happened to the agent. It is 70 only when
`run.json` itself could not be written.

**Environment.** The adapter gets only: the card's `env`; the binding variables; and a fixed base (`PATH`
`/usr/local/bin:/usr/bin:/bin`, `HOME` the `guest` account's home, `USER`, `LOGNAME`, `SHELL`, `LANG`, `TERM`,
`TMPDIR`, `CI`, `NO_COLOR`, `XDG_*`, a fixed git identity, and `MOTEL_TOUR`, `MOTEL_GUEST`, `MOTEL_ATTEMPT`,
`MOTEL_BASE`, `MOTEL_BRANCH`, `MOTEL_WORK`, `MOTEL_RESULT`). The runner's own environment is never passed on.
The card cannot set `PATH`, `HOME`, `LD_*`, `GIT_*`, `MOTEL_*`, `PYTHON*`, `NODE_*`, proxy and CA variables, or any
credential or base-URL variable (`ANTHROPIC_*`, `OPENAI_*`, `CLAUDE_*`, `CODEX_*`, `AWS_*`, `AZURE_*`, `GOOGLE_*`, `GCP_*`,
`HCLOUD_*`, `GITHUB_*`, `GH_*`, `SSH_*`, `SUDO_*`, `BASH_*`; compared in upper case). The list is one constant in
`adapters.py`, exported as `motel.tours.guest.RESERVED_ENV_NAMES`, `RESERVED_ENV_PREFIXES` and `env_name_reserved()`;
the engine's spec check imports it, so the two cannot drift.
`MOTEL_RESULT` is where the agent may write its `RESULT.json` (outside the repository); a `RESULT.json` in the
repository root is also read, and is left out of the commit unless the base tracks it.

**`run.json`.** Required fields are as in the contract. `termination` is `exited`, `timeout`, `killed` or `failed`.
Additive fields:

| Field | Meaning |
|---|---|
| `reason` | with `failed`: `adapter_missing`, `invalid_card`, `input_missing`, `base_missing`, `overlay_failed`, `config_failed`, `git_missing`, `git_timeout`, `runner_error`, `runner_is_root`, `own_login_missing_token`, `own_login_invalid_token`, `secret_leak`, `stragglers_left`, `out_not_clean` (the final clean-up of `out/` failed; `run.json` is still written, with a `detail`) |
| `command_started` | `false` when the agent command never ran. Only those failures may be retried (invariant 6). `secret_leak` has it `true` |
| `signal`, `duration_s`, `auth`, `tour`, `guest`, `adapter`, `base`, `branch`, `overlay_commit` | what they say; `exit_code` is `128 + n` for a signal, `127` for `adapter_missing` |
| `bundle_bytes`, `changed_files_truncated` | `bundle_sha256` is `null` when nothing changed or the bundle was withheld |
| `stdout_bytes`, `stderr_bytes` | size of the log file as left on disk; `stdout_total_bytes` / `stderr_total_bytes` are what the agent wrote; `*_truncated` |
| `result_json` | `true` only when the runner parsed `RESULT.json`, found no secret in it and wrote it (in canonical form) to `out/RESULT.json`; `false` otherwise, and then there is no such file. The engine should fetch the claims only when it is `true` |
| `stragglers_killed`, `stragglers_left`, `pruned_out` | processes killed after the agent ended; processes that could not be killed (with `termination: "failed"`); entries removed from `out/` that the runner had not written (normally 0) |
| `rejected` | items the runner would not hand over: `{"path", "reason"}` with `symlink`, `not_regular`, `symlinked_parent`, `special_file`, `bad_path`, `too_large`, `too_many_files`, `invalid_json`, `secret_leak`, `scan_failed`, `bundle_failed`, `git_broken` (this one with a `detail`) |
| `redactions` | `{"stdout", "stderr", "files"}`: how many replacements |
| `concierge_refusals` | see below |

**Concierge refusals.** The concierge refuses over-budget requests with 403 and over-concurrency ones with 429, with a
JSON body whose `error.type` starts with `motel_`. The runner cannot see those responses, but every agent CLI prints
the error it received, so each adapter has a scanner (`RefusalScanner` in `adapters.py`) that watches the captured
output as it streams, before the log cap applies. `run.json` then carries, always present (an empty list when none):

```json
"concierge_refusals": [{"type": "motel_budget_exceeded", "status": 403, "count": 2},
                       {"type": "motel_concurrency", "status": 429, "count": 3}]
```

`status` is the HTTP status when the agent printed it just before the body, otherwise `null`. This is evidence of what
the agent saw, not a ledger: a refusal the CLI retried silently is not counted, and `Retry-After` is a response header,
so it is not visible. The concierge's own ledger (`ConciergeClient.usage(attempt=...)["refused"]`) is the complete
count; the engine should use the runner's field to explain why an attempt ended, and the ledger for accounting.

### Never put the placeholder token, or a login token, in an output

Two guards run over everything the runner hands over. Streams are redacted as they go by (before the log cap). The
history is checked as raw objects in the runner's own repository, not as `git log -p` in the agent's: a diff in the
agent's repository can be made to hide a token (committer name, merge commits, `refs/replace`, a `textconv` driver in
the repository's own config), and none of those reach the runner's repository.

* the **placeholder token** is redacted (`[motel:redacted]`) in the captured logs and in working-tree files that would
  be committed or copied. If it is found in any object of `base..HEAD` (commit headers and messages, trees, files), the
  bundle is withheld; in an artifact or `RESULT.json`, that item is withheld and the attempt fails with `reason:
  "secret_leak"`. It is also scrubbed from `run.json` as a last resort;
* an **own-login token** (below) gets the same treatment, except that it is never "fixed" inside repository content:
  a copy in the working tree (tracked or untracked, not git-ignored), in a commit, in an artifact or in `RESULT.json`
  withholds that item and fails the attempt with `secret_leak`.

`RESULT.json` is parsed and written back in canonical form (sorted keys, ASCII only, no spaces); the raw bytes and the
canonical form are both scanned, so a token spelled with JSON escapes (`\u0073k-...`) is found. Only the canonical form
is handed over.

Limits: the scan is for the token's exact bytes. An agent that encodes the token first (base64, split across
lines) is not caught. The placeholder is harmless outside the guest (only the concierge accepts it, on the guest's
loopback, until the attempt is revoked); a login token is not, which is why own-login stays opt-in.

## Adapters

`adapters.py` holds a table, `ADAPTERS`, of `Adapter` objects: the executable to look up on the clean PATH, an `argv`
function, an `env` function, a `files` function (config files under HOME), the bindings the adapter requires, and the
refusal scanner. A missing executable is `termination: "failed"`, `reason: "adapter_missing"`, `command_started:
false`, `exit_code: 127`. To add an agent CLI, add one `Adapter` to the table.

| Adapter | argv | Notes |
|---|---|---|
| `claude` | `claude -p --output-format json --max-turns N --no-session-persistence --dangerously-skip-permissions -- <prompt>` | needs binding `anthropic`, or own-login. Behind the concierge it also gets `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` and `CLAUDE_CODE_MAX_OUTPUT_TOKENS` (see below) |
| `codex` | `codex exec --json --skip-git-repo-check --ephemeral --dangerously-bypass-approvals-and-sandbox -- <prompt>` | needs binding `openai`; `max_turns` has no Codex flag |
| `command` | the card's `argv` | no prompt needed |

Prompts go after `--` so a prompt that starts with `-` is not read as an option. A prompt is at most 100,000 bytes,
because one argument cannot exceed 128 KiB.

**Sources for the base URL and key settings** (checked against Claude Code 2.1.x and codex-cli 0.160.x using the
CLIs' own `--help` output and the option and setting names in the shipped binaries; no API request was made):

* Claude Code: `ANTHROPIC_BASE_URL` and `ANTHROPIC_API_KEY` (documented in the Claude Code docs, "Authentication" and
  "LLM gateway"); it appends `/v1/messages`, so the base is `http://127.0.0.1:<port>/u/anthropic`. In `-p` mode the
  environment key is always used (docs, "Authentication precedence", item 3). Settings: `~/.claude/settings.json`
  (`permissions.defaultMode`) and `~/.claude.json` (`hasCompletedOnboarding`).
* Codex: a custom provider in `$CODEX_HOME/config.toml`: `model_provider`, and `[model_providers.<id>]` with
  `base_url`, `env_key` (the NAME of the variable that holds the key) and `wire_api = "responses"`. Codex appends
  `/responses`, so the base is `http://127.0.0.1:<port>/u/openai/v1`. The id `motel` avoids the reserved built-in ids.
  `OPENAI_BASE_URL` and `OPENAI_API_KEY` are also set (the contract names them; a plain OpenAI SDK reads them).
* Other bindings get `MOTEL_BINDING_<NAME>_URL` and `MOTEL_BINDING_<NAME>_TOKEN`.
* Behind the concierge, `claude` is started with `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` and
  `CLAUDE_CODE_MAX_OUTPUT_TOKENS=<max_output_tokens>`. Source: the capture of claude 2.1.319 in the concierge part
  (`docs/concierge.md`, "Agent CLI profiles", branch `tours-concierge`). Without the first, the CLI sends beta headers
  that the concierge's `claude-code` profile refuses and every request gets 403. Without the second, the CLI asks
  for 128000 output tokens on Opus 5.5 and the concierge reserves the price of that for one request (about 2.56 USD),
  which is over a 2 USD guest budget. The card field `max_output_tokens` (integer, 256 to 128000, default 8000)
  sets it. Neither variable is set in own-login mode, which has no concierge.
* Optional hardening, not enabled: `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` is meant to make Claude Code scrub credentials
  from the environment of the processes it starts. It needs bubblewrap in the image, so it cannot be a default (this
  was not tested here, and the variable name comes from the review, not from a source this part checked). A card cannot
  set it (`CLAUDE_*` is reserved); an operator who bakes bubblewrap into the image can add it to the `claude` adapter's
  `env` function.

The key is only ever an environment value. It is never written to a config file or an argument.

## Own-login mode (opt-in, `claude` only)

API keys through the concierge are the default. A card with `"auth": "own-login"` runs the `claude` adapter with the
owner's own subscription login instead:

* the runner reads exactly one line from its stdin (at most 4 KB) and passes it to the adapter process only, as
  `CLAUDE_CODE_OAUTH_TOKEN`. `ANTHROPIC_BASE_URL` and `ANTHROPIC_API_KEY` are not set. The token is held in memory
  and is never written to disk by the runner, nor placed in an argument;
* an empty stdin fails with `own_login_missing_token` before anything runs; a line over 4 KB or with spaces or
  control characters fails with `own_login_invalid_token`. The card needs no `bindings`, `placeholder` or
  `concierge_port`, and may not list the `anthropic` binding;
* the controller should send the token over the ssh channel's stdin, not in an argument or in `card.json`. It is a
  long-lived credential (`claude setup-token` issues a one-year token that can only make model requests, per the
  Claude Code docs, "Authentication"). Guest root and the agent can read the process environment, so use it only for
  work the owner would run with that login anyway.

Claude Code is started with the settings that cut non-essential traffic. Sources: Claude Code docs, "Environment
variables" and "Enterprise network configuration" (section "Network access requirements"):
`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` (auto-update, telemetry, error reporting, feedback, release notes, the two
Datadog intake hosts and feature-flag fetching), `DISABLE_TELEMETRY`, `DO_NOT_TRACK`, `DISABLE_ERROR_REPORTING`,
`DISABLE_AUTOUPDATER`, `ENABLE_CLAUDEAI_MCP_SERVERS=false` (no claude.ai connector fetch through
`mcp-proxy.anthropic.com`) and `CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL`. They are set for every `claude`
guest.

## The egress profile

`motel.tours.guest.userdata_extras(egress_hosts, resolvers, *, concierge_port=None, own_login=False, ports=(443,),
refresh_s=300, refresh_window_s=21600) -> motel.cabins.userdata.GuestExtras` is what the engine calls. Pass the result
to `render_user_data(..., extras=...)` (the controller's `checkin` needs a matching parameter). Only valid for
`access = "ssh-allowlist"`: a tour guest's filter would cut a tailnet connection, so tailscale cabins are refused.

A tour guest gets a second nftables table, `inet cabin_tour`, next to the cabin's own `inet cabin` (which is untouched,
so the cabin's integrity check and its metadata drop still apply). Its output chain has policy **drop**, except:

* loopback (the concierge forward is loopback on the guest);
* established and related traffic, so the controller's inbound ssh replies flow. The input chain is the cabin table's
  and still admits ssh from the controller's exact address only;
* DNS (UDP and TCP 53) to the configured resolvers, nothing else, and only from root (`meta skuid 0`). Port 53 is
  dropped for every other user before loopback is accepted, so no resolver service on the machine can be used either;
* DHCP renewal (UDP 68 to 67) to the broadcast address and the default gateway, which is the DHCP server on the
  provider's network;
* TCP `ports` (default 443 only) to addresses in the set `allow4`.

**DNS.** The guest user cannot ask any resolver anything, so it has no DNS channel: a query name can carry data to
whoever runs the authoritative server for it, and a resolver forwards it. On a tour guest `systemd-resolved` is stopped
and masked, and `/etc/resolv.conf` is a plain file that lists the configured resolvers (at most three, as glibc reads),
for root's own lookups. (apt does not work in the tour state: its `_apt` user is not root and the package mirrors are not allowlisted. The bootstrap installs packages before the filter loads.) The refresher, which runs as root, does its own lookups: a small stdlib DNS
client sends A queries straight to the configured resolvers (never through `/etc/hosts` or NSS), with a random
transaction id and a fresh random source port from a connected UDP socket, a time limit per resolver (one absolute deadline, also for the TCP retry when the reply is truncated), and a check that every reply matches the question (id, response flag, the one question,
and only records that lead from the name through its CNAME chain are believed; compression loops are bounded). A reply
that does not match is ignored until the time runs out. The refresher writes each resolved name and its addresses into
a managed block of `/etc/hosts` (between `# BEGIN motel-tour` and `# END motel-tour`; the rest of the file is kept),
next to adding them to `allow4`, and the guest resolves the allowlisted names through `nsswitch`'s `files` entry.
Consequences: a tool that does its own DNS (a static binary with a built-in resolver, DNS over HTTPS) cannot resolve
anything and fails; a name that is not allowlisted fails after the libc timeout instead of resolving; a name that gets a
new address after the refresh window is stale. What is left open: an allowlisted host can itself carry data out (it is
reachable on 443), and so can anything that shares its address. A name the guest looks up that is not in `/etc/hosts`
cannot leave the machine as a DNS query at all.

**Boot order.** The rules file that the firewall unit loads (`/etc/motel-cabin/tour.nft`) is not shipped. The unit's
`ExecStartPre=` runs `/usr/local/sbin/motel-tour-nft` as root: it fills the one token of the shipped template
(`tour.nft.in`) with the DHCP server (the default gateway, remembered in `/var/lib/motel-guest/dhcp-server` because
the route does not exist yet early in a reboot), checks the result with `nft -c`, and only then moves it into place.
`ssh` and `ssh.socket` `Require=` the unit, so nothing can start the firewall (or sshd) before its rules are complete,
on the first boot and on every reboot. The rendered rules were checked with `nft -c -f` (nftables 1.0.9) in a
container and loaded there; the policy and the port-53 drop are the loaded form.

`allow4` starts empty. `/usr/local/sbin/motel-tour-egress` resolves each allowlisted name to IPv4 addresses, keeps only
globally routable ones (so a rebinding answer cannot open a private or metadata address) and adds them. It runs at
boot (the bootstrap retries for about a minute), then every `refresh_s` seconds until `refresh_window_s` after its first
run, then disables its own timer. Addresses are added, never removed, and capped at 512. The service also runs at
every later boot: if the set is empty but the state file remembers addresses (the firewall unit loads an empty set
when the machine reboots), they are put back, also after the window has ended. The service is ordered before
`ssh`, which `Requires=` it, so a guest that cannot load the filter does not come up. The bootstrap writes
`/var/lib/motel-guest/tour-ready.json` (`tour_nft`, `allowed_ips`); the engine should wait for `tour_nft: loaded`.

Limits: the allowlist is by IP address, so anything sharing an address with an allowed name (a CDN) is reachable too;
names are exact (no wildcards); IPv6 is off on cabins.

**Size.** The provider limits user data to 32 KiB. At most `MAX_EGRESS_HOSTS` (64) hosts are accepted in all (an
own-login guest's `api.anthropic.com` is one of them, so it gets 63 of its own), and the names may take at most
`MAX_EGRESS_BYTES` (1700) of `egress_cost(hosts)` (each name's length plus `EGRESS_HOST_OVERHEAD`). With the budget
full the rendered user data is at or below `USER_DATA_TARGET` (31,744 bytes, 1 KiB under the limit) for the usual
cabin configuration (two resolvers, two source addresses, a few packages); the 1 KiB left over is the room for longer
lists. The refresher and the other tour files are stored gzip+base64 (cloud-init `encoding: gz+b64`) and `tour.json`
is compact to make room. All of these are exported by `motel.tours.guest`, so the engine's
spec validation can fail early with the same numbers.

`own_login=True` adds `api.anthropic.com` to the allowlist and nothing else, and ignores `concierge_port`. Source:
Claude Code docs, "Network access requirements": `api.anthropic.com` carries the Claude API requests; `claude.ai`,
`platform.claude.com` and `sso.claude.com` are for interactive sign-in and OAuth refresh, which a `setup-token` token
does not use; the Datadog hosts are optional telemetry switched off above.

The profile also creates the unprivileged user `guest` (locked password, no sudo, home mode 0750) and the input
directory `/var/lib/motel-guest/in` (root-owned, group `guest`, 0750). If a `guest` account already exists (a baked
image) it must be exactly that: uid not 0, no group but its own, and `sudo -l -U guest` must say it may not run sudo.
Otherwise the bootstrap stops, the cabin never reports ready, and nothing runs (fail closed).

**Reverse forward.** With `concierge_port`, an sshd drop-in (`04-cabin-tour.conf`, listed before `10-cabin.conf`
because sshd keeps the first value) allows remote forwarding for the cabin account to `127.0.0.1:<port>` only, and the
cabin key gains `port-forwarding,permitlisten="127.0.0.1:<port>"`. The cabin's own readiness probe expects forwarding
fully disabled and a key line of exactly the stock shape, so it needs a tour-aware variant; see the open items in the
build report. `/etc/motel-cabin/tour.json` records the profile for that probe.

## Image bake

`motel.tours.bake` builds a snapshot from a fresh cabin plus an operator's setup script:

* `bake(controller, setup_script_path, name, *, server_type=None, ttl=1h, owner="motel-bake", setup_timeout_s=1800)`
  returns `{"schema": "motel/tour-image/v1", "id", "name", "lease", ...}`;
* `images(controller)` lists this host's tour images; `delete(controller, image_id)` deletes one.

The flow is: refuse a duplicate name; check in a cabin; run the setup script as root; run one **finalize** session that
cleans (cloud-init state and the bootstrap's `cloud-init.disabled` marker, ssh host keys, machine-id, shell history,
the journal and `/var/log`, root and home caches, `/tmp`, and the cabin bootstrap's own files and units, so the next
boot starts from the stock image), scans, and only if clean schedules a power-off; wait for the server to be off;
snapshot it with `managed-by=motel`, `role=tour-image`, `controller=<host id>` labels; wait until the image is
available; check the cabin out. The cabin is checked out in a `finally`, so it is deleted when the setup fails, a
credential is found, or anything raises; the error carries `.cleanup` (the cabin's cleanup state). The scan runs after
the cleanup and in the same session, because the cleanup removes the admission gate.

A bake is refused on a tailscale-mode host (the cabin would hold a tailnet node key; tour guests never use tailscale).

The scan refuses the bake if it finds, under `/root`, `/home`, `/opt`, `/srv`, `/mnt`, `/etc`, `/usr/local`, `/var/lib`
or `/var/www`: by path, `.claude/.credentials.json` and other `credentials` files of the claude, codex and gemini CLIs,
`.aws`, `.config/gcloud`, gcloud application default credentials, `.azure`, `.docker/config.json`, `.netrc`,
`.git-credentials`, `.npmrc`, `.pypirc`, `.kube/config`, private ssh keys and ssh host keys, the tailscale state, `.env` files
(not `.env.example`/`.sample`), `*.p12`/`*.pfx`/`*.jks`; by content, in any text file up to 512 KiB (not under
`/var/lib`, which holds databases and package state), a private-key block (`PRIVATE KEY-----`), `sk-ant-`, an `sk-`
key, `ghp_`, `github_pat_`, `xox?-` and `AKIA` tokens; and in `.git/config` and `.gitconfig` a URL with a user name
and password (`https://user:token@host/`). Because it reads `/etc`, it covers `/etc/environment` and the shell rc
files; because it reads `/root` and `/home`, it covers each account's `.bashrc` and `.profile`. CA bundles and
public keys pass. It prints paths only, never contents. `node_modules`, `site-packages` and `.git` directories are not
searched for the general patterns.

**This scan is best effort.** It finds credentials in the usual places and shapes. A secret that is encoded, split
over lines, stored in a file over 512 KiB, a database or an archive, in a directory outside those roots, or in a
shape the patterns do not know, passes. Treat the setup script as the place that must not write credentials and the
scan as the net under it.

Each image id is recorded in `tour-images.json` in the cabins state directory (root-owned, mode 0600) when the
provider returns it, and `delete` refuses an image with no such record, as well as one without this host's labels.
An intent (image name and lease) is written before the snapshot call and settled by its answer. If the answer is lost,
a failed bake looks the image up by its labels (name and source lease) and deletes it; if that fails too, the pending
intent still lets `delete` remove an image whose labels name exactly that name and lease (nothing else).
`images()` rows carry `recorded`. A record for an image that no longer exists at the provider is dropped by `delete`
(it answers `already_gone`). A record file that is a link or is damaged is refused, not repaired.

The provider side (`GuardedApi.create_image/image/list_images/delete_image`) is label-scoped like every other write:
an image is only listed, read or deleted when it is a snapshot with this host's `managed-by`, `role` and `controller`
labels and a valid `image-name`; every write re-proves that the project holds only motel cabins and cannot see this
host; `create_image` requires the lease's own server to be powered off.

Image bake and the runner never copy a login into an image. An agent CLI installed by the setup script stays logged
out; API keys stay in the concierge.
