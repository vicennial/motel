# The concierge

The concierge is the credential proxy for [tours](tours.md). A tour guest runs an agent on a throwaway cabin.
The agent needs a model API, but the cabin must never hold a real key. So each attempt gets a **placeholder token**.
The concierge accepts that token, swaps in the real key, and forwards the request to an allowed upstream.

```
cabin (guest)                              controller host
  agent ──http──> 127.0.0.1:7781 ──ssh -R──>  127.0.0.1:<this attempt's port> ──> concierge ──https──> api.anthropic.com
  holds only a placeholder token             (one listener per attempt)          holds the real key, the ledger, the budget
```

It is one stdlib-only Python process: `motel-concierge.service`, running as the system user `motel-concierge`.

## Threat model

**Who is trusted.** The operator (root) and the upstream provider. The guest is not: it runs arbitrary agent code on
a disposable machine, and the agent can be steered by prompt injection from anything it reads.

**What it protects**

| Asset | How |
|---|---|
| The real API key | Held only by the concierge. Read from `key_file` (`root:motel-concierge 0640`) when used. Injected into the upstream request only. Never logged, never written to the ledger, redacted from anything an upstream echoes back (headers and body, also across stream chunks). |
| The choice of destination | The origin, the auth style and the key come only from `[tours.bindings.*]`. The guest's `Host`, `X-Forwarded-*`, query string, absolute-URI request line, body and `%`-escapes cannot change them. Redirects are never followed. TLS is verified. |
| The operator's money | Per-attempt budget, reserved before dispatch. Per-attempt and per-host concurrency. Body, response and time caps. |
| The control plane | The admin socket accepts uid 0 only (file mode and `SO_PEERCRED`). Each attempt has its own listener, on `127.0.0.1` only. |
| Other attempts | A token is bound to one attempt and the bindings it was issued for. Revoking or expiring one never touches another. Only the SHA-256 of a token is stored. |

**What it does not protect**

- **Misuse of the allowed API.** A guest with a valid token can spend its budget on anything the allowed paths
  accept, and can send data it reads to the upstream. Allow only the paths the agent needs.
- **A hard dollar cap.** See "Budget semantics". The budget bounds spend to a small overshoot, not to the cent.
- **A compromised controller host or a compromised root.** They hold the keys by definition.
- **Subscription logins.** Bindings are API keys. motel never copies `~/.claude` or Codex logins to a cabin.
- **The cabin's other egress.** What else a cabin can reach is the tour's egress allowlist, not the concierge.
- **Traffic between the cabin and the controller.** It runs inside the ssh reverse forward. The placeholder token
  is the only secret on that path, and it is worthless without the concierge.
- **A key the upstream itself reflects in another form.** The concierge redacts the exact key (raw and
  JSON-escaped). A provider that echoes a masked or re-encoded key is out of its reach.
- **Denial of service by a guest to itself.** A guest can burn its own budget or hold its own slots.

## Set up a binding

1. Put the key in a file the service can read and nobody else can. The key is the whole file content, one printable
   token, no spaces:

   ```
   sudo install -d -m 0750 -o root -g motel-concierge /etc/motel/llm-keys
   sudo install -m 0640 -o root -g motel-concierge /dev/stdin /etc/motel/llm-keys/anthropic.key
   ```

   `/etc/motel/llm-keys` holds the concierge's keys and nothing else. `/etc/motel/credentials` stays `root 0700`: the
   service cannot enter it, so the provider token and the own-login token there (`own-login/claude.token`, read by
   root only, never by the concierge) are out of its reach.
2. Declare the binding in `/etc/motel/motel.toml`:

   ```toml
   [tours]
   enabled = true
   concierge_port = 7781              # the fixed port on the guest; the controller does not listen on it
   concierge_port_range = "17800-17999"   # controller loopback ports, one per live attempt
   max_usd_model_per_guest = 2.0     # no attempt can be issued more than this

   [tours.bindings.anthropic]
   upstream = "https://api.anthropic.com"
   auth = "x-api-key"                 # or "bearer"
   key_file = "/etc/motel/llm-keys/anthropic.key"
   paths = ["/v1/messages", "/v1/messages/count_tokens"]
   profile = "claude-code"            # the beta headers `claude -p` sends (see Agent CLI profiles)

   [tours.bindings.openai]
   upstream = "https://api.openai.com"
   auth = "bearer"
   key_file = "/etc/motel/llm-keys/openai.key"
   paths = ["/v1/chat/completions", "/v1/responses"]
   profile = "codex"                  # what `codex exec` sends (see Agent CLI profiles)
   ```

   Optional keys per binding: `methods` (default `["POST"]`, also `"GET"`), `format` (`"anthropic"` or `"openai"`,
   default from `auth`), `max_concurrent` (in flight to that host, default 16), `default_max_output_tokens`
   (`openai` format, default 32768), `profile`, `betas` and `allow` (see Request policy and Agent CLI profiles), `default_price` and
   `pricing."model-prefix"` (see Pricing).
   A path ending in `/*` allows everything below it (`/v1/models/*`). Matching is exact otherwise.
   `upstream` is an origin: `https://host[:port]`, no path. Plain `http://` is accepted for loopback addresses only.
3. `sudo motel install --upgrade` (this installs the user, the units and the directories), then
   `sudo systemctl status motel-concierge`. A missing or unreadable key shows up as `bindings: {"name": false}` in the
   health reply and as a `502 motel_key_unavailable` to a guest. The key file is re-read when it changes: rotate by
   replacing the file, no restart.

The service reads `/etc/motel/motel.toml`, so that file must be readable by `motel-concierge`. It holds no secrets.

## One listener per attempt

The concierge does not listen on one shared port. `issue` opens a listener for the attempt on its own loopback port, taken
from `[tours] concierge_port_range` (default `"17800-17999"`), before it returns the token. The port is written to the
ledger. The listener closes when the attempt is revoked (at once) or expires (at the next sweep, within about a second).

**A live attempt never changes port, and a start never binds its port again.** The only thing that keeps an attempt live across a
restart is its listening socket coming back from systemd's file-descriptor store:

- *How it works.* Each listener is handed to the store before `issue` returns (`FDSTORE=1` with the descriptor, named
  `att-<attempt>`, on `NOTIFY_SOCKET`; standard library only: `motel.concierge.fdstore`). The store outlives a service restart, so
  the port is never unbound, no other local process can take it, and connections queue in the backlog until the service is
  back. The unit sets `FileDescriptorStoreMax=512`, which equals the largest `concierge_port_range` (a larger range is a config
  error). The store is used only when systemd started the process (`LISTEN_PID` equals its pid; `INVOCATION_ID` is not accepted
  instead, because a child of the service inherits it), and `NOTIFY_SOCKET` is removed from the environment once read.
  (The key is `FileDescriptorStoreMax=`. An earlier version wrote `FileDescriptorStore=`, which does not exist: systemd ignored
  it, the store stayed at its default of 0, and nothing was ever stored.)
- *Support matrix.* systemd's `$FDSTORE` (the store size, set only when it is non-zero) and `FileDescriptorStorePreserve=` both
  arrive in systemd 254, and Debian 12 ships 252, so the service cannot rely on `$FDSTORE`. The installer therefore writes a
  marker next to the key, `Environment=MOTEL_FDSTORE_MAX=512`, and the service reads that.

  | systemd | unit lines | what survives |
  | --- | --- | --- |
  | 254 or newer (Ubuntu 24.04 ships 255) | `FileDescriptorStoreMax=512`, `FileDescriptorStorePreserve=yes`, `MOTEL_FDSTORE_MAX=512`, `MOTEL_FDSTORE_SIGNAL=1` | crash restarts, `systemctl restart` and an explicit `systemctl stop` then start (a reboot ends them) |
  | older than 254, or not detected | `FileDescriptorStoreMax=512`, `MOTEL_FDSTORE_MAX=512` | crash restarts and `systemctl restart`; an explicit `systemctl stop` releases the store, so the next start ends the attempts as `not_preserved` |

  **On systemd older than 254, a unit that ends `failed` releases the store.** A crash restart keeps it, but if the service hits its
  start limit (`Restart=on-failure` stops after the default 5 starts in 10 s) the unit goes to `failed`, systemd treats the service
  as stopped, and the stored descriptors are closed. The attempts then end as `not_preserved` at the next start. Fix the cause, run
  `systemctl reset-failed motel-concierge`, and start it. On 254 or newer `FileDescriptorStorePreserve=yes` keeps the store through
  this too. To drop the store on purpose (for example when removing the service), use `systemctl clean --what=fdstore motel-concierge`.

  This follows `systemd.service(5)`: stored descriptors are closed when the service is fully stopped and no job is queued or
  running for it, unless `FileDescriptorStorePreserve=` says otherwise. A restart is a job, so the store is kept. The default
  of `FileDescriptorStorePreserve=` on 254 or newer is `restart`, which also keeps it across restarts. The installer picks `yes`
  so that an explicit stop and start keeps it too. `install.detect_systemd_version()` feeds `install.unit_text(systemd_version=...)`.
- *Start-time check.* The service **refuses to start** (the message names the fix) when `MOTEL_FDSTORE_MAX` is missing (an old or
  hand-written unit, including the one that had the misspelled key) or smaller than the configured port range. On systemd 254 or
  newer `$FDSTORE` must also agree with the marker, and must be set when the unit says it was written for such a systemd
  (`MOTEL_FDSTORE_SIGNAL=1`): that catches a `FileDescriptorStoreMax=` systemd ignored. Below 512 but large enough it warns and
  lowers its own limit to match. `--dev-rebind` skips the check.
- *Upgrade and rollout.* The installer must rewrite the units on **upgrade**, not only first install: `install.unit_changes(read,
  systemd_version)` returns every unit that is missing or differs from what is installed, `install.after_unit_changes` the
  `daemon-reload` and service restart that follow, and `install.unit_problems(text)` flags an installed unit with no or too small
  a store, or no marker, for the verify step. `installer.py` (through `motel.tours.install.install_units`) calls these on every install and upgrade and
  fails if the verify step finds a problem. **The first upgrade to a working store restarts the service with an empty store, so every live attempt ends as
  `not_preserved` once.** Do it between tours.
- *Why never bind again.* A service that closes the socket and binds it again leaves a window. Another local process can take
  the port while the service is down, read the placeholder token from the cabin's reconnecting forward (the cabin keeps
  dialling the old port), and let go before the restart. The token would then work. Moving the attempt to a new port is worse.
- *So.* At start, a live attempt whose socket came back, on the port the ledger records, stays live. **Every other live attempt
  is revoked**, an error is logged, and `health()` lists it under `lost_listeners` as `{attempt: "not_preserved"}` (for this
  process's start). The engine sees `gone` from `endpoint` and treats the attempt as cut. That applies in every mode: a restart
  that loses the store (a reboot, or an explicit stop on a systemd older than 254) ends the attempts, which is cheap.
  The store keeps names that are not unique and removal drops every entry of a name, so an attempt that comes back with several
  sockets keeps the one on its recorded port, the others are closed and removed, and the kept one is stored again. A stored
  socket for an attempt that is not live is closed and removed.
- *No store, no attempt.* If the socket cannot be stored (no notify socket, the store full, the send failed), `issue` fails with
  kind `busy` and records nothing, rather than run the attempt unprotected. The service refuses to start outside systemd.
- *The one exception* is `motel-concierge --dev-rebind`: no store is needed, and a start binds each attempt's recorded port
  again (revoking it, `port_taken`, only if that fails). It is **unsafe** (the squatting window above) and for development
  only; `health()` shows `dev_rebind`. This path has been tested against a model of systemd's store, not a real systemd.

Why: before authentication a connection says nothing about who it is, so on a shared port one hostile cabin could keep
the whole pool busy with connections that never finish a request, and delay every other guest. Now a connection is
attributed to an attempt by the port it arrives on, before a byte is read:

- A token is accepted only on its own attempt's listener (401 on any other).
- Each listener has a pool of its own: `max_concurrent + 2` authenticated connections (429 `concurrency` beyond that) plus
  **2** that have not authenticated yet. A further connection waits up to 2 seconds for one of those two slots and is then
  dropped. What an attempt does on its port can affect only itself.
- After a reply sent before the body was read, the connection is given at most 1 second in total, however its bytes are
  paced, to let the client's body drain (so a large request that is refused gets the JSON error, not a connection reset). That
  holds for a connection that has not authenticated too: it costs only its own listener's 2 pending slots.

**For the engine.** The guest keeps one fixed port (`concierge_port`, default 7781). Ask the concierge where the attempt's
listener is, and aim the ssh reverse forward at it:

```python
port = ConciergeClient().endpoint(attempt="a-1")["port"]          # {"port": 17842}
# ssh ... -R 127.0.0.1:<concierge_port>:127.0.0.1:<port>
```

`issue` is unchanged and still returns the token. `endpoint` fails with `AdminRefused` of kind `gone` once the attempt is
revoked or expired, and with `AttemptNotFound` for an unknown attempt. After a concierge restart the port is the same, or the attempt is revoked: one whose socket
did not come back from the store is `gone` and listed in `health()["lost_listeners"]` (`not_preserved`), never moved. `issue` fails with kind `busy` when no port in the range is free or its socket cannot be stored. A port whose listener was closed in the last 10 minutes is not given to another attempt while the range has any other free port (a retry or forward that belonged to the old attempt must not meet the new one); when nothing else is free, the one freed longest ago is used. The quarantine is kept in memory, so a restart forgets it.
`motel.testing.FakeConcierge.endpoint` has the same signature and the same errors. There is no shared guest listener any more
(it is removed, not disabled), so `motel-concierge --port` is gone and `unit_text(port)` ignores its argument.

## What a guest sees

The guest talks to `http://127.0.0.1:<concierge_port>/u/<binding>/<upstream path>` with the placeholder token as its API key
(`x-api-key` or `Authorization: Bearer`). The runner sets `ANTHROPIC_BASE_URL=http://127.0.0.1:<port>/u/anthropic`
and `ANTHROPIC_API_KEY=<token>`, so the standard SDKs and CLIs work unchanged.

Refusals are JSON: `{"type": "error", "error": {"type": "motel_<reason>", "message": "..."}}`.

| Status | `motel_` reason | Meaning |
|---|---|---|
| 401 | `missing_token`, `invalid_token`, `token_expired`, `token_revoked` | The token is not usable. |
| 403 | `binding_denied`, `path_denied` | Wrong binding for this token, or a path that is not in the binding's list. |
| 403 | `feature_denied` | The request uses a feature the binding does not allow (see Request policy). |
| 403 | `budget_exhausted` | The worst case for this request does not fit in the remaining budget. Lower `max_tokens`. Not retried by SDKs. |
| 405 | `method_not_allowed` | The binding does not allow that method. |
| 429 | `concurrency`, `host_busy`, `busy` | Too many requests in flight, too many open connections for the attempt, the upstream host is full, or too many request bodies are in memory. `Retry-After: 1`. |
| 502 | `upstream_error`, `upstream_tls`, `upstream_encoding`, `key_unavailable` | The concierge could not complete the call. |
| 501 | `bad_request` | `CONNECT` and other methods the listener does not implement (a proxy tunnel is never offered). |
| 400, 411, 413 | `bad_request`, `bad_max_tokens`, `max_tokens_required`, `length_required`, `request_too_large` | The request is malformed or too large (cap 8 MiB, `concierge_max_request_mb`). |

Upstream error responses (for example the provider's own 429 or 529) pass through unchanged, with their body.
Chunked request bodies are not supported: the SDKs send `Content-Length`.

What is forwarded: the method, the path (as allowed), the query string, the body, and the guest's other headers
(`anthropic-version`, `anthropic-beta`, `content-type`, `user-agent`, ...). What is not: `authorization`, `x-api-key`,
`proxy-authorization`, `cookie`, `host`, `x-forwarded-*`, `forwarded`, `via`, hop-by-hop headers, `accept-encoding`,
and any header whose name contains `auth`, `api-key`, `secret`, `token`, `cookie`, `password` or `credential`.
The concierge asks the upstream for an unencoded reply (`Accept-Encoding: identity`) so it can scan and meter it; an
encoded reply is refused. Responses come back chunked. `Set-Cookie` is dropped.

## Budget semantics

Read this before trusting a number.

1. **Reserve.** Before dispatch the concierge computes a worst-case cost: `input_estimate x input price + max_tokens x
   output price` (times `n` for OpenAI). `input_estimate` is the body size in bytes divided by three, which is on the
   high side for text. The reservation is written to the ledger (and synced to disk) first. If
   `spent + reserved + this reservation` is over the attempt's budget, the request is refused with 403 and never sent.
2. **Settle.** When the response ends, the reservation is replaced by the actual cost, computed from the usage the
   upstream reported. The settle is written before the last bytes reach the guest, so a guest that has read a whole
   reply already sees its cost.
3. **When usage is missing** (an unparseable body, a stream that ended early, a connection lost, a response over the
   4 MiB parse buffer, a revoked attempt) the request is charged conservatively: input already seen plus the worst-case
   output, or the full reservation. Upstream 4xx/5xx and 3xx responses are not billed.

What this guarantees: concurrent requests cannot jointly overspend a budget that they have reserved against, and a
guest cannot dodge accounting by ending connections early.

What it does **not** guarantee: a hard cap.

- **The input side is an estimate, and the bound is stated, not exact.** A request the policy lets through is priced
  from its body: a text prompt has at most one token per byte, three times the reservation's one token per three bytes.
  Each image block is reserved at 6000 tokens, which covers the largest image the models take. Base64 PDFs are denied by
  default (`pdf`); when a binding allows them, the reservation adds 3000 input tokens **per page**, counting `/Type /Page`
  objects (or a larger declared `/Count`) in each file, and assumes 600 pages (the most a request can hold) for a file it
  cannot read: no `%PDF-` header, bad base64, no page objects, or objects packed in compressed streams. So one request's
  text input can exceed its reservation by at most about twice the reserved input cost, and the settle then records the
  real cost and the next request is refused. The overshoot is bounded by `max_concurrent x 2 x (reserved input cost)` plus
  the output cap, which the provider enforces. The 3000 tokens per page is an assumption, not a published figure.
- **Streaming exposure.** Usage arrives at the end of a stream (`message_delta` for Anthropic, the final chunk for
  OpenAI). Mid-stream the concierge cannot cut a response on cost. The bound is `max_tokens`, which the provider
  enforces. For `openai` bindings, a request without an output cap is reserved at `default_max_output_tokens`, but the
  model may legitimately produce more. Make your agents send an output cap.
- Streamed OpenAI chat requests get `stream_options.include_usage = true` added, so there is a usage chunk to meter.
- Fast mode (`allow = ["fast_mode"]`) is reserved and settled at 2x the standard price, also when only the response says it ran fast.
  That is the model reference's multiple for Claude Opus 5 and 5.5; it is assumed for others.
- **Allowed features can cost more than tokens, and that part is not in the reservation.** Web search is counted at USD 0.01
  per search when the response reports it. Everything else a feature bills for (code-execution time, file-search storage
  and calls, image generation, tokens the upstream reads from state it holds for you) is unbounded here: allowing it
  moves that risk to the provider-side spend limit.
- Long prompts are priced with the premium rules in "Pricing" (assumed, where the source is silent, to stay on the high side).
- Cache writes are priced at the 5-minute rate (1.25x) or 1-hour rate (2x) when the response splits them, and
  cache reads at the discounted rate for known models. Unknown models get no cache discount.
- Prices are list prices. Credits, batch discounts and negotiated rates are ignored, so real bills are lower or equal.
- The figure in `usage()` is `spent + reserved`: while a request is in flight it shows the worst case.
- The operator ceiling `max_usd_model_per_guest` bounds every `issue`, so a coordinating agent cannot ask for more.
  `max_eur_per_tour` and the cloud-side limits are separate and are not enforced here.

## Request policy

The reservation is computed from the request body, so anything that makes the upstream use tokens or money the body does not
show would slip past it. Each binding therefore **refuses by default** (403 `motel_feature_denied`, before anything is sent)
a request that uses such a feature, unless the operator names it in `allow`:

| `allow` value | Refused unless allowed |
|---|---|
| `server_state` | Context the upstream holds: `thread`, `container`, `previous_response_id`, `conversation`, a stored `prompt`, any `background` value other than `false`, a file named in a content block (`file_id` on a `document`, `image`, `input_file`, `input_image`, `file` or `container_upload` block), in a `source` object (`file_id`, or `type: "file"`), in an OpenAI file part (`file.file_id`) or in a code-execution container (`file_ids`), at any depth. A `file_id` that is only data is not checked, and only at fixed positions from the root of the body: a tool entry's `input_schema`, `parameters` or `schema` (also under its `function` or `custom`) in `tools[]` or codex's `input[].tools[]` (and one namespace level inside, `tools[].tools[]`), a response format's schema (`response_format`, `text.format`, `output_config.format`), and the `input` of a `tool_use` that is an entry of `messages[].content[]`. The same key anywhere else (metadata, a content block, a deeper level) is still checked. A content block, metadata or the request itself with a key of the same name is still checked. On the OpenAI Responses API (`/responses`, `/responses/compact` on an `openai`-format binding): an `input` item that only names a stored item (`item_reference`, or an item with nothing but `id` and `type`), and any body that does not say `"store": false` (a missing `store` means true: the response is kept upstream and can be read back). Codex sends `store: false`, so it is unaffected |
| `url_sources` | Documents or images the upstream fetches itself: `source.type: "url"`, `image_url` / `file_url` that is not a `data:` URL |
| `pdf` | A base64 `document` block (or any document of media type `application/pdf`), and an OpenAI `file_data` file part. Billed per page by the upstream; allowed, it is reserved at 3000 tokens per page (see Budget semantics). Documents given as `text` or `content` sources are tokens in the body and are not included. |
| `web_search`, `web_fetch`, `code_execution`, `mcp`, `file_search`, `image_generation` | Server-side tools of that kind (Anthropic `web_search_*`, `web_fetch_*`, `code_execution_*`, `mcp_servers`, `mcp_toolset`; OpenAI `web_search*`, `code_interpreter`, `mcp`, `file_search`, `image_generation`) |
| `fast_mode` | `speed` other than standard; OpenAI `service_tier` `priority` or `scale` |
| `fallbacks` | The `fallbacks` parameter (the request may be re-run on another model) |
| `tool:<type>` | Tools are an allowlist. A tool entry (in any `tools` list, at any depth) may have no `type`, or `custom`, `function` or `namespace`; those are the client-side tools your agent runs itself. (`namespace` groups function and custom tools under one name; codex 0.160.0 sends its tools that way, as captured in `tests/concierge/fixtures/codex_0_160_0_responses.json`. A namespace counts as a client tool only while it has no field beyond `type`, `name`, `description` and `tools`; with any other, such as a `server_url`, it is `tool:namespace` and needs that allow entry.) Any other `type` that is not one of the families above is refused unless `allow` names that exact type, for example `allow = ["tool:bash_20250124"]`. That covers tool search, the advisor, and the Anthropic-defined bash, text editor, computer and memory tools; an unknown future type is refused until someone names it. |

`betas` is a list of `anthropic-beta` / `openai-beta` header values a guest may send; any other value is refused. The
default is none, so an agent CLI that sends beta headers needs them listed:

```toml
[tours.bindings.anthropic]
betas = ["interleaved-thinking-2025-05-14"]
allow = ["web_search"]          # example: let guests search, and accept that searches are not capped here
```

### Agent CLI profiles

The two agent CLIs the tour runner starts send beta headers that a deny-by-default binding would refuse, so a real guest
would get a 403 on its first request. `profile = "claude-code"` or `profile = "codex"` adds the betas (and, if ever
needed, `allow` entries) that CLI sends for a plain print-mode run. Operator `betas` and `allow` are added on top of the
profile; nothing can be taken away from it. A binding with no profile behaves as before (nothing allowed).

The rule for a profile: **it never allows a beta that enables server tools, server-held state (threads, files, containers,
skills), MCP, fast mode, fallbacks or a second model (advisor)**. `allow` is empty in both built-in profiles.

The lists come from capture. Each CLI was run (`claude -p "say hi" --output-format json --max-turns 2` and
`codex exec --json "say hi"`) against a fake upstream on 127.0.0.1 with throwaway home directories, a fake key and no
network beyond loopback; the method, path, headers (authorisation values redacted) and the body were recorded. Versions:
**claude 2.1.319**, **codex-cli 0.160.0**. The sanitised requests are stored in `tests/concierge/fixtures/` and a test sends
them through a real concierge.

**`claude-code`** is the set `claude -p` sends **when `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` is in its environment**:
`claude-code-20250219`, `interleaved-thinking-2025-05-14`, `effort-2025-11-24`. Without that variable the CLI sends eight
betas, and one of them (`advanced-tool-use`) enables server tools, so the profile does not allow it. The guest adapter
must therefore set the variable (checked on five models: Opus 5.5, Sonnet 5.5, Haiku 5.5, Fable 5.1 and Opus 4.8, each ran
to completion against a fake upstream that answered 403 to the five betas below; with the default environment and only
`advanced-tool-use` refused, the CLI does not recover, it exits with the error). `ENABLE_TOOL_SEARCH=false` alone removes
only that one beta.

| Beta | Sent by default | In the profile | Why |
|---|---|---|---|
| `claude-code-20250219` | yes | yes | Identifies the client. No body field. Enables nothing. |
| `interleaved-thinking-2025-05-14` | yes | yes | Thinking between tool calls (`thinking: {type: "adaptive"}` in the body). Thinking tokens are output tokens, bounded by `max_tokens`, which the reservation uses. |
| `effort-2025-11-24` | yes | yes | `output_config.effort`. The model reference calls it GA, so the header is redundant. Bounded by `max_tokens`. |
| `advanced-tool-use-2025-11-20` | yes | **no** | The CLI's own label for it is `tool_search`. It is the bundle for tool search, programmatic tool calling (code execution) and tool-use examples: it enables server tools. |
| `context-management-2025-06-27` | yes | no | Clears old tool results or thinking blocks from the history *sent in the request* (body: `context_management.edits`). It matches none of the forbidden classes; it is left off because the CLI's own switch removes it and a smaller allowance is safer. |
| `per-turn-control-2026-07-01` | yes (not on Opus 4.8) | no | CLI label `per_message_effort`; body: a `role: "system"` message carrying `output_config.effort`. Matches none of the forbidden classes (name-to-feature link inferred). Left off as above. |
| `mid-conversation-tool-changes-2026-07-01` | yes | no | `tool_addition` / `tool_removal` references to tools in the same request. Matches none of the forbidden classes. Left off as above. |
| `thinking-token-count-2026-05-13` | yes | no | CLI label `thinking_token_count`; **not in the claude-api reference**, no body field. Effect inferred from the name. Left off: an undocumented beta is not allowed by default. |

An operator who wants the CLI's default behaviour back lists the extra betas under `betas` (not `advanced-tool-use`, unless
the binding is also meant to run server tools: then name `code_execution` in `allow` too).

**What a `claude-code` binding refuses**, with 403: every beta not listed. The ones the claude binary can send in other
modes (named in its own beta table, none was sent by a plain `-p` run) include `fast-mode-2026-02-01` (2x price),
`mcp-servers-2025-12-04`, `files-api-2025-04-14` (stored files), `web-search-2025-03-05`, `server-side-fallback-*` (re-run on
another model), `advisor-tool-2026-03-01` (a second model), `extended-cache-ttl-2025-04-11`, `task-budgets-2026-03-13`,
`environments-2025-11-01` and `ccr-byoc-2025-07-29`. The captured request has only client-side tools, no stored context, no
file, no URL source, no MCP and no fast mode.

**`codex`**, no betas and no `allow`. `codex exec` sends `POST /v1/responses` with **no `openai-beta` header**; its
`x-codex-beta-features: remote_compaction_v2` header is not an `openai-beta` value, is not checked, and is passed through.
The body has `store: false`, `include: ["reasoning.encrypted_content"]` and function/custom tools only. None of that is a
gated feature. The profile exists so the binding config says what it was checked against, and so a later CLI release that
needs an entry can add one in a single place. The codex binary also contains `OpenAI-Beta: responses_websockets=...`; that is
sent only on a WebSocket upgrade, which the concierge does not proxy, so it is deliberately not listed.

**Codex compaction.** codex 0.160.0 compacts through an ordinary `POST /v1/responses` whose last `input` item is
`{"type": "compaction_trigger"}` (`remote_compaction_v2`, used when the provider is named `OpenAI`; with any other name it
compacts inline with a normal summarising request). It has `store: false`, so the codex profile serves it; the reply carries one
`compaction` output item and the next request sends it back in `input`. Both requests are in
`tests/concierge/fixtures/codex_0_160_0_compaction_v2.json`, captured against a loopback fake with `model_auto_compact_token_limit`
forced low. This CLI version sent **no** `/v1/responses/compact` (it could not be made to), so the older shape is not captured;
the concierge treats that path as a Responses request like any other. A body without `"store": false` (a missing `store`
means true) gets `403 motel_feature_denied` whose message says so, names compaction, and says that sending the same request
again fails the same way. It carries no `Retry-After`. Measured: codex retries such a 403 twice itself, then ends the turn
with the message ("Error running remote compact task: unexpected status 403 Forbidden: ..."), so the guest sees the cause.

**Three facts for whoever writes the guest adapter** (none is a concierge setting):

- Set `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` for `claude` (see above); without it every request is refused.
- `claude` asks for `max_tokens: 128000` on Opus 5.5 by default, so one request reserves 128000 x USD 20 / 1M = **USD 2.56**,
  more than the default `max_usd_model_per_guest` of 2.0: the first request gets `403 budget_exhausted`. Setting the
  environment variable `CLAUDE_CODE_MAX_OUTPUT_TOKENS=8000` for the CLI made it send `max_tokens: 8000` (checked).
- `codex` defaults to model `gpt-6.1-sol`, which has no entry in the built-in table, so it is billed at the USD 15 / 75
  default until the operator adds `pricing."gpt-6.1-sol"`.

**One reading of the body.** On every path, `count_tokens` included, a non-empty body must be a strict JSON object, or the
request is refused (400 `motel_bad_request`) and nothing is forwarded. The concierge parses it once and refuses a body that
repeats a key at any depth (also when the repeat is only an escape: `"a"` and `"\u0061"`), that repeats a top-level
request field in a different letter case (`max_tokens` and `MAX_TOKENS`: a case-insensitive parser could read the other
one; nested objects such as tool schemas may hold `id` and `ID`), or that holds `NaN`, `Infinity` or an out-of-range
number. It forwards the *re-serialised parsed object*, not the guest's bytes. So the `max_tokens` and `model` it priced are
the ones the upstream reads, and a UTF-16 body reaches the upstream as UTF-8. `Content-Length` is set from the forwarded
bytes. Key order and whitespace may change. A request with no body (an empty `count_tokens`) is forwarded as it is.

Client-side tools with no type (or `custom` / `function`) are not affected. The
check is made on the parsed JSON body; a body that nests deeper than 200 levels is refused outright.

Use the upstream provider's own spend limit on the key as the real cap. The concierge makes a runaway swarm stop
early; the provider limit is what makes a bad day cost a bounded amount.

## Pricing

Prices are USD per million tokens and live in `motel.concierge.pricing`. The Claude entries come from the Claude API
model reference as of **2026-10-06** (`PRICING_AS_OF`): Fable 5.1 and 5, Mythos 5.1 and 5 (USD 10 / 50), Opus 5.5
(4 / 20), Opus 5, 4.8, 4.7 and 4.6 (5 / 25), Sonnet 5.5 and 5 (2 / 10), Sonnet 4.6 (3 / 15), Haiku 5.5 (0.10 / 0.50) and
Haiku 4.5 (1 / 5). Cache reads use the stated rate where there is one (Fable 5.1 and Mythos 5.1 0.25, Fable 5 and Mythos 5
1.00, Opus 5.5 0.20, Sonnet 5.5 0.20) and 0.1x input elsewhere; cache writes are 1.25x (5 minutes) and 2x (1 hour).
Models that reference does not price (Opus 4.5, Sonnet 4.5 and the retired models) are **not** in the table. The OpenAI
entries are unverified and not from that source.

A model id matches exactly or with a date, `-latest`, `[...]` or `@...` suffix. A future `claude-opus-5-6` does not match
`claude-opus-5`; it is unknown. An unknown model is billed at `DEFAULT_PRICE`: **USD 15 input, USD 75 output**, no cache
discount, long-context premium assumed. That is the highest price in the table, so a new model is assumed to be
expensive, and the cost is flagged `price_unknown` in the ledger. Expect a new model to be refused sooner than it should be
until you add it:

```toml
[tours.bindings.anthropic.pricing."claude-newmodel"]   # prefix match
input = 3.0
output = 15.0
cache_read = 0.3        # optional; default: same as input
cache_write = 3.75      # optional (5-minute); default 1.25 x input
cache_write_1h = 6.0    # optional; default 2 x input
```

**Long prompts.** Haiku 5.5 is billed at 0.50 / 2.50 (5x) for the whole request when the prompt is over 100K tokens;
Opus 4.8 has no premium. For every other Claude model the reference says nothing about a premium, so the table assumes
input x2 and output x1.5 above 200K tokens, which can only over-charge. Prices you set in config carry no premium rule:
put the long-prompt figures into the number if your contract has them. The prompt size counts input, cache reads and cache writes.

When the request model and the response model differ, the dearer one is charged.

## Ledger

`/var/lib/motel-concierge/ledger.jsonl` is append-only, one JSON object per line, mode 0600. Events: `start`, `issue`,
`revoke`, `reserve`, `settle`, `refuse`, `snapshot`. `issue` stores `token_sha256`, never the token. A `settle` carries
the cost, token counts, model, status, outcome (`ok`, `partial`, `unmetered`, `upstream_error`, `redirect`, `cut`,
`revoked`, `restart`, ...), `price_unknown`, `over_reservation` and `redactions` (the number of times a real key was
scrubbed from a reply: anything above zero means the upstream echoed it). Keys, tokens, prompts and replies are never
written. Requests with an unknown token are only counted (`unknown_token_refusals` in the health reply).

At start-up a ledger over 32 MiB is rotated to `ledger.jsonl.<epoch>` and the live state is carried into the new file
as `snapshot` events. Old files are kept; delete them when you no longer need the history.

## Restart behaviour

A restart does not revoke an attempt whose listening socket comes back from systemd's file-descriptor store (see One listener
per attempt); a live attempt whose socket did not come back is revoked at start (`lost_listeners`). The ledger is replayed: tokens keep their expiry, revocation and spend (only
hashes are stored, so a token is shown once, at `issue`). A request that was in flight when the process died is
settled at its full reserved cost, because nobody can know what the upstream billed. Attempts that ended more than 24
hours ago are forgotten. To drop every token on purpose, call `revoke(tour=...)` for each live tour, or stop the service and
move the ledger away.

## Admin socket and the system user

The admin socket is `/run/motel/concierge.sock`: JSON lines, one request per connection, requests capped at 64 KiB.
The Python client is `motel.concierge.admin.ConciergeClient` (`issue`, `revoke`, `usage`, `endpoint`, `health`).

It is root-only on two independent layers: the socket file is `root:root 0600` in a `0700` directory, and the server
checks `SO_PEERCRED` on every connection and serves uid 0 only. Because the service runs as `motel-concierge`, it must
not create the socket itself (it would own it and could re-mode it). `motel-concierge.socket` creates it as root and
systemd hands the open socket to the service. The service therefore has no `RuntimeDirectory=` either: that would
chown `/run/motel` to the service user and delete the socket on stop.

The service unit (`motel.concierge.install.unit_text()`) runs with `NoNewPrivileges`, an empty
`CapabilityBoundingSet`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, `PrivateDevices`, no address families but
`AF_UNIX`/`AF_INET`/`AF_INET6`, a system-call allowlist and write access to its state directory only.
`motel.concierge.install.layout()` lists the user, directories and modes the installer creates.

## Known limits

- One process, a thread per connection. Each attempt's listener allows `max_concurrent + 2` authenticated connections and 2
  that have not authenticated; extra connections are dropped (see One listener per attempt).
- **Slow and idle connections.** The request line and headers must arrive within 10 seconds in total, however they trickle.
  The body must arrive within 10 seconds plus its length at 64 KiB/s. After authentication an attempt may hold at most
  `max_concurrent + 2` connections (429 `concurrency` beyond that). All request bodies
  together may occupy `concierge_max_buffered_mb` (default 192) of memory; past that, 429 `busy`. A body is charged in
  steps by what it really costs: its raw bytes while it is read; then, before it is decoded, one more copy of the bytes
  (the re-encoded request) plus three copies of the text (decoded text, the strings of the parsed tree, the re-serialised
  text) at the size of the text's Python string kind, which is read from the raw bytes (1 byte per character for ASCII and
  Latin-1, 2 up to U+FFFF, 4 beyond: one emoji makes the whole text 4 bytes per character); then 200 bytes for each JSON key or
  value. A plain ASCII body therefore costs about 5x its size and the worst case 14x, plus the nodes. The setting must hold
  one request at its caps: at least 14x `concierge_max_request_mb` plus 200 bytes x `concierge_max_json_nodes` (150.1 MiB at the
  defaults), and one attempt may hold at most the larger of that and a quarter of the budget, so it cannot starve the others.
  A request whose own charge can never fit its attempt's share or the total gets 413 `request_too_large` (waiting would not help);
  one that only has to wait for other bodies to finish gets 429 `busy`. The charge is returned as soon as the body has been sent upstream, not when the
  reply ends. A body may hold at most `concierge_max_json_nodes` (default 200000) JSON keys plus values: that is counted
  with a linear scan *before* the parser runs, because a few megabytes of `[],[],...` would otherwise become hundreds of
  megabytes of Python objects. Beyond it: 413 `request_too_large`. The `100 Continue` for an `Expect` header is sent only
  after the request is authenticated and admitted.
  A hostile guest can fill its own listener's two pre-authentication slots and so delay its own new connections; it cannot
  affect any other attempt's.
- At most `concierge_port_range` attempts can be live at once (200 by default); `issue` is refused (`busy`) beyond that.
  A local user of the controller host who binds a port of the range first makes that port unusable, and sees only the
  placeholder token if a guest's forward reaches it; it never sees a key.
- No HTTP keep-alive and no upstream connection pooling: one upstream connection per request.
- Request bodies are limited to `Content-Length` framing and `concierge_max_request_mb`. Reply bodies over 128 MiB or
  longer than 30 minutes are cut.
- The upstream must answer without content encoding.
- Paths with `%` escapes, `..` or `//` are refused rather than normalised.
- Batch and file-upload endpoints are not in any default binding. Do not add them without thinking about how they are
  billed: their cost is not in the response that creates them.
