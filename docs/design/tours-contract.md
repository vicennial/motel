# Tours: implementation contract

Status: build contract for 0.1. The user-facing guide is `docs/tours.md`. This file fixes the seams between the
three parts so they can be built in parallel: the **concierge** (credential proxy), the **engine** (tours on
cabins: CLI, desk, supervisor, MCP, SDK) and the **guest** side (runner, adapters, egress profile, image bake).

## Words

- **Tour**: one submission. It has an owner (the invoking Unix user), limits and N guests.
- **Guest**: one bounded, non-interactive job. Task in, result record out, then its cabin is deleted.
- **Attempt**: one execution of a guest on one fresh cabin. A retry is a new attempt on a new cabin.
- **Binding**: a named upstream credential held by the concierge (for example `anthropic`, `openai`).
- **Placeholder token**: a per-attempt secret that only the concierge accepts. It is never an upstream credential.

## Invariants (every part must keep these)

1. No real credential ever reaches a guest: not in env, files, argv, cloud-init user data or images.
2. motel never reads or copies a subscription login (`~/.claude`, credential stores, keychains, ChatGPT/Codex auth
   files) and never runs `claude setup-token`. Bindings are API keys only. The one exception is the opt-in
   **own-login** mode (amendment below): the operator creates a token on their own machine and gives it to motel,
   which only ever places it on the one guest that asked for it.
3. Every attempt runs on a fresh cabin. A finished cabin is never reused.
4. Operator ceilings in `/etc/motel/motel.toml` bound every tour. A tour may ask for less, never more. Nothing the
   coordinating agent sends can raise a ceiling.
5. Execution status and cleanup status are separate. Power-off is not deletion.
6. Retries happen only for a failure the engine itself saw before it launched the runner (booking, copy, placeholder,
   forward preflight). The engine saves `command_started_at` before it launches the runner; from then on the attempt is
   never re-run, whatever `run.json` says. A guest-written "not started" is a claim, never a reason to run again.
7. Guest output is untrusted. The controller validates and caps everything it accepts and never executes or applies
   it.

## Configuration (`[tours]` in motel.toml)

```toml
[tours]
enabled = false
concierge_port = 7781            # the fixed port on the guest (the engine forwards it to the attempt's own port)
concierge_port_range = "17800-17999"   # controller 127.0.0.1 ports, one listener per live attempt
max_concurrent_guests = 4        # per host, across tours
max_guests_per_tour = 20
max_eur_per_tour = 5.0           # cloud spend reserved before create (hourly price x ceil(ttl hours))
max_usd_model_per_guest = 2.0    # concierge budget per attempt
max_ttl = "4h"                   # per attempt wall clock
default_image = "ubuntu-24.04"   # or a baked snapshot id / name
default_server_type = ""         # empty: [cabins] default
max_result_mb = 64               # bundle + artifacts per attempt
egress_allow = []                # hosts every tour may add to its allowlist; tours can only pick from these

[tours.bindings.anthropic]
upstream = "https://api.anthropic.com"
auth = "x-api-key"               # or "bearer"
key_file = "/etc/motel/llm-keys/anthropic.key"
paths = ["/v1/messages", "/v1/messages/count_tokens"]
profile = "claude-code"          # optional; "claude-code" or "codex": the beta headers that CLI sends (docs/concierge.md)
```

```toml
# Opt-in alternative to API keys (amendment, see "Own-login mode"). Root only; off by default.
[tours.own_login]
enabled = false
claude_token_file = "/etc/motel/credentials/own-login/claude.token"   # root:root 0600
max_concurrent = 2               # guests using own-login at once, host-wide
max_ttl = "2h"                   # per attempt; a token cannot be revoked per job, so keep it short
```

The engine reads `[tours.own_login]`; the concierge ignores it (`parse_tours_section(table, strict=False)` leaves
unknown top-level keys alone). The engine reads the rest of `[tours]` through `motel.concierge.settings`.

## Concierge (owner: concierge part)

- Runs as system user `motel-concierge` (`useradd --system --user-group`), as `motel-concierge.service`.
- Key files are `root:motel-concierge 0640`, in `/etc/motel/llm-keys/` (`root:motel-concierge 0750`). The concierge never
  writes them and cannot read `/etc/motel/credentials` (root 0700), where the own-login token lives
  (`/etc/motel/credentials/own-login/claude.token`; only root, the controller, reads it).
- **Guest listeners:** one HTTP listener per attempt, on `127.0.0.1:<port from concierge_port_range>` of the controller; there is no
  shared listener. `issue` opens it and the ledger keeps its port; revoke or expiry closes it; a restart re-opens it on the same
  port, from systemd's file-descriptor store only: the service never binds a live attempt's port again. An attempt whose socket
  did not come back is revoked and listed in `health()["lost_listeners"]` as `not_preserved`, so the engine sees `gone` from
  `endpoint` and treats it as cut; `issue` fails with `busy` if the socket cannot be stored. `--dev-rebind` is the unsafe dev exception. A token is accepted only on its own attempt's listener, and each listener has its own small connection pool, so one
  guest cannot delay another before it authenticates. Path shape `/u/<binding>/<upstream path>`.
  - Token from `x-api-key` or `Authorization: Bearer`. Constant-time lookup.
  - The binding comes from the URL path and must be in the token's bindings. The upstream origin, auth header
    and key come only from config. No guest header, host, query or body field selects a credential or destination.
  - Strip every inbound auth header; inject the real one; forward only to the binding's fixed origin and allowed
    paths; verify TLS; never follow redirects; stream responses (SSE) through.
  - Budget: refuse before dispatch when the attempt is over budget or expired or revoked; pace with a per-attempt
    and per-host concurrency limit. Parse usage from responses (including streams) into an append-only JSONL ledger.
  - Refusals are JSON `{"type": "error", "error": {"type": "motel_<reason>", "message": ...}}` with status 401, 403,
    429 or 502.
- **Admin socket:** `/run/motel/concierge.sock`, mode 0600, `SO_PEERCRED` uid 0 only. JSON lines, one request per
  connection. Python client: `motel.concierge.admin.ConciergeClient`.

```python
class ConciergeClient:                       # motel.concierge.admin
    def issue(self, *, tour: str, guest: str, attempt: str, bindings: list[str], expires_at: str,
              budget_usd: float, max_concurrent: int = 2) -> str: ...   # returns the placeholder token
    def revoke(self, *, attempt: str | None = None, tour: str | None = None) -> int: ...  # number revoked
    def usage(self, *, attempt: str) -> dict: ...  # {"usd", "input_tokens", "output_tokens", "requests", "refused"}
    def endpoint(self, *, attempt: str) -> dict: ...  # {"port": int}: the attempt's own listener (AdminRefused kind "gone" once revoked)
    def health(self) -> dict: ...
```

- The engine's tests use `motel.testing.FakeConcierge` (same methods, in memory). The concierge part provides it.
- The concierge part also provides `motel.concierge.install.unit_text() -> str` (the old `port` argument is accepted and ignored) and `layout()` (users, dirs,
  modes) for the installer. The engine part wires them into `installer.py`.

## Guest side (owner: guest part)

- **Inputs** copied by the controller into `/var/lib/motel-guest/in/` (root-owned, readable by user `guest`):
  `input.bundle` (git bundle of the base commit), optional `overlay.patch`, and `card.json`:

```json
{"schema": "motel/guest-card/v1", "tour": "t-…", "guest": "fix-auth", "attempt": "a-…",
 "base": "<commit sha>", "branch": "motel/<tour>/<guest>",
 "adapter": "claude" | "codex" | "command", "prompt": "…", "argv": ["…"],
 "env": {"NAME": "value"}, "bindings": ["anthropic"], "concierge_port": 17781,
 "auth": "api-key" | "own-login", "placeholder": "<token>", "ttl_s": 3600, "max_turns": 30, "max_output_tokens": 8000,
 "artifacts": ["reports/*.json"],
 "max_result_mb": 64}
```

- **Runner** `motel.tours.guest.runner` (stdlib only, shipped as package data, run as user `guest` by the
  controller over ssh): clone the bundle to `~/work`, check out `base`, apply the overlay, set
  `ANTHROPIC_BASE_URL=http://127.0.0.1:<concierge_port>/u/anthropic`, `ANTHROPIC_API_KEY=<placeholder>` (and the
  OpenAI pair for an `openai` binding), set `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` and
  `CLAUDE_CODE_MAX_OUTPUT_TOKENS=<card.max_output_tokens>` in the adapter's environment (see below), run the adapter in its own process group with the wall-clock limit, cap
  stdout/stderr to files, then commit all changes on `branch`, write `out/result.bundle` (`base..HEAD`), copy
  declared artifacts into `out/artifacts/` (regular files only, no links), copy the agent's `RESULT.json` if present
  (64 KB cap), and write `out/run.json`:

```json
{"schema": "motel/guest-run/v1", "attempt": "a-…", "exit_code": 0, "termination": "exited" | "timeout" | "killed",
 "started": "…Z", "finished": "…Z", "head": "<sha>", "changed_files": ["…"], "bundle_sha256": "…",
 "artifacts": [{"path": "…", "bytes": 0, "sha256": "…"}], "stdout_bytes": 0, "stderr_bytes": 0}
```

- **Own-login runner input** (amendment). When `card.auth` is `"own-login"` the card has `bindings: []`, an empty
  `placeholder` and no token. The runner reads **one line from stdin** (`token\n`, at most 4096 bytes, then stops
  reading), keeps the token in memory only, and passes it to the `claude` adapter as `CLAUDE_CODE_OAUTH_TOKEN` in
  that adapter's environment. It sets no `ANTHROPIC_BASE_URL`/`ANTHROPIC_API_KEY`. It never writes the token to a
  file, `run.json`, `RESULT.json` or a log. If the line is missing or malformed the runner exits before the adapter
  starts. The engine does not retry it (the runner was already launched). With `auth = "concierge"` the runner reads no stdin.
- **Card field `max_output_tokens`** (integer, default 8000): the output cap the guest's agent asks for. The runner exports it
  as `CLAUDE_CODE_MAX_OUTPUT_TOKENS`, which makes `claude` send `max_tokens: <value>` (checked; without it `claude` asks for
  128000 on Opus 5.5, reserving USD 2.56 per request, more than the default `max_usd_model_per_guest`, so the first request
  would get `403 budget_exhausted`). The concierge reserves `max_tokens x price`, so this field is also the worst-case cost of
  one request. The guest enforces an integer from 256 to 128000 (`motel.tours.guest.MIN_OUTPUT_TOKENS`, `MAX_OUTPUT_TOKENS_LIMIT`); the engine
  validates the spec field with those constants and writes the card field only when the spec sets it.
- **Claude environment** (set by the runner for the `claude` adapter, on top of the placeholder pair):
  `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` and `CLAUDE_CODE_MAX_OUTPUT_TOKENS=<max_output_tokens>`. The first makes the
  CLI send only the three betas the concierge's `claude-code` profile allows (`claude-code-20250219`,
  `interleaved-thinking-2025-05-14`, `effort-2025-11-24`); without it the CLI sends eight, one of which
  (`advanced-tool-use-2025-11-20`) enables server tools, the concierge answers `403`, and the CLI exits without retrying. The
  `codex` adapter needs neither (its requests carry no beta).
- **Adapters:** `claude` → `claude -p <prompt> --output-format json --max-turns N`; `codex` → `codex exec --json
  <prompt>`; `command` → `argv` as given. The adapter set is a table, easy to extend. Agent CLIs come from the image.
- **Egress profile** (cloud-init, applied when the cabin is a tour guest): nftables output policy drop, except
  loopback, established/related, DNS to the configured resolvers, and the tour's allowlisted hosts (resolved at
  boot). The concierge is reached through the controller's ssh reverse forward, so it is loopback on the guest.
  The cloud firewall keeps inbound ssh from the controller only. For an own-login guest the profile also allows
  `api.anthropic.com` (and nothing else extra), because that guest talks to Anthropic directly, not through the
  concierge: the engine calls `userdata_extras(hosts, dns_resolvers, concierge_port=..., own_login=True)` (the allowlist holds at most 64 hosts, 63 for own-login).
  The guest sshd must allow remote forwarding (`AllowTcpForwarding remote` or `yes` for user `cabin`): the engine
  runs a preflight with the same `-R` before the command starts, so a refusal is a retryable pre-start failure.
  Integration as built: the cabin's readiness check is tour-aware. The tour user data writes the drop-in
  `04-cabin-tour.conf` (`AllowTcpForwarding remote`, `PermitListen 127.0.0.1:PORT`) and widens the cabin key options
  to `port-forwarding,permitlisten="127.0.0.1:PORT"`; the guest agent reports `forward_port` only when `sshd -T` and
  the key line match exactly, and the controller accepts exactly that port (derived from the key options) and
  nothing else. Any extra forward permission, port, local forwarding, agent or X11 forwarding still fails readiness.
  The runner files are installed to `/usr/local/lib/motel-tour/` (root, 0644), the inputs to
  `/var/lib/motel-guest/in/`; the preflight reads `/var/lib/motel-guest/tour-ready.json`. A runner exit of 70 means
  `run.json` could not be written. Tailscale-access cabins are refused for tours (the egress filter would cut the tailnet).
- **Bake:** `motel.tours.bake` builds a snapshot from a fresh cabin plus a setup script: run the script, clean
  cloud-init state, ssh host keys, machine-id and logs, verify no credential files, power off, snapshot with
  `managed-by=motel, role=tour-image` labels, delete the server. Images are listed and deleted only through motel.
  Needs `create_image`, `list_images`, `delete_image` on the cabins provider (guest part adds them).

## Engine (owner: engine part)

- **User CLI** (unprivileged; sudoers pins each verb): `motel tour start FILE|- [--json]`, `tour ls`,
  `tour status ID`, `tour wait ID [--any|--all] [--timeout S]`, `tour result ID GUEST`, `tour fetch ID GUEST`
  (bundle into the current repo as branch `motel/<tour>/<guest>`), `tour logs ID GUEST`, `tour cancel ID`.
  Operator verbs (root): `tour reap [--apply]`, `tour bake …`, `tour image ls|rm`.
- **Spec** (TOML or JSON): `[tour] name`, `[defaults] server_type image ttl adapter env secrets egress budget_usd
  max_turns artifacts`, `[[guests]] id prompt|argv repo base overlay artifacts`. The CLI makes the git bundle as
  the invoking user from that user's repo and hands it to the desk; the desk copies it with `O_NOFOLLOW`, a size cap
  and a digest into root-owned tour state. The desk validates every field (ids, sizes, durations, hosts against
  `egress_allow`, bindings against config, limits against ceilings).
- **Supervisor:** started by the desk as a transient systemd unit `motel-tour-<id>` (root). State lives in
  `/var/lib/motel/tours/<id>/` with intent recorded before every create. Housekeeping (`tour reap --apply`, run
  with the cabin reap) reconciles: a dead supervisor is restarted or its attempts are cancelled and cleaned.
- **Per attempt:** reserve cloud and model budget → `Controller.checkin` → issue a placeholder → copy inputs →
  run the runner over the unprivileged cabin ssh with `-R 127.0.0.1:<concierge_port>:127.0.0.1:<endpoint port>` (the port from `ConciergeClient.endpoint(attempt=...)`) → collect
  `out/` with caps → validate → revoke the token → `Controller.checkout` → record usage and cost.
- **Statuses:** execution `queued | booking | running | collecting | done | failed | timed_out | over_budget |
  cancelled`; cleanup `live | deleting | deleted | deletion_pending`.
- **Results** are stored under the root-only state directory. Users read them through the desk verbs `result`
  (JSON), `fetch` (raw bytes) and `logs` (capped text); the desk answers "not found" to anyone but the owner (and
  root). `tour result` prints the record: controller-verified facts (bundle digest and size, head and changed files
  computed from the fetched bundle, saved artifacts, cost, usage) separate from `runner_reported` (exit code,
  termination: what the runner said) and from `guest_claims` (`RESULT.json`), plus `violations` (what motel refused) and `auth`.
- **MCP tools:** `tour_start`, `tour_status`, `tour_wait`, `tour_result`, `tour_cancel`. **SDK:** `Motel.tour(spec)`
  with `as_completed()`, `results()`, `cancel()`. The SDK also switches `cabin_run` to `cabin run --json`.
- **Installer:** `[tours] enabled` adds the tour verbs to sudoers, the concierge user and unit (from the concierge
  part), and `tour reap --apply` to housekeeping.

## Own-login mode (amendment)

An opt-in alternative to the API-key concierge path. API keys through the concierge stay the default and the
recommended path. Own-login exists because some operators want agent jobs to run under their own Claude plan.

**Operator side (root only).** `[tours.own_login] enabled = true` turns it on. The operator makes the token on their
own machine with `claude setup-token` and stores it with `sudo motel-desk tour own-login set --stdin`, which writes
`claude_token_file` as `root:root 0600` (O_NOFOLLOW, atomic replace, 4 KiB cap). `tour own-login status` shows
presence and modification time only; `tour own-login clear` removes it. motel never reads `~/.claude`, a credential
store or a keychain and never runs `setup-token` itself. `motel doctor` warns while own-login is enabled.

**Spec.** `auth = "concierge"` (default) or `"own-login"`, in `[defaults]` or per guest. The desk accepts own-login
only when the operator enabled it, only with the `claude` adapter (Codex own-login is out of scope in 0.1), with no
`secrets` and no `budget_usd` (nothing is metered), and with `ttl` within `own_login.max_ttl`. Concurrency is held
to `own_login.max_concurrent` by a host-wide slot pool, separate from `max_concurrent_guests`. The CLI prints one
warning on start: the token is placed on the guest, has no per-job budget, is account-wide, and its use is the
operator's and user's responsibility under the plan's terms.

**Delivery.** The supervisor reads the token file and sends it to the runner on the **ssh stdin of the run command
only**, as one line `token\n`. It is never in `card.json`, any argv, a file on the guest, the environment of the
ssh process, the logs, the tour state or a result. The engine redacts the token from the runner log and from any
fetched `stdout.log`, `stderr.log` and `RESULT.json`. The card carries `"auth": "own-login"` and nothing secret. No
concierge placeholder is issued for that attempt.

**Result.** The result record shows `auth: "own-login"` and `usage: {"metered": false, "note": "not metered: ..."}`.
Cloud cost is still computed. **Revocation is not possible**: a stolen token stays valid until the operator revokes
it in their Anthropic account, so cancel cannot revoke it and the TTL ceiling is the only bound. The token is
account-wide and long-lived; a prompt-injected agent on the guest can read and send it to any allowed host.
