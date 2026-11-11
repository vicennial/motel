# Omnigent on motel cabins

> **Experimental.** Tested against a fake `motel` and a fake `ssh` with Omnigent 0.16.0 and 0.21.0, and run by the live
> harness against a real Hetzner cabin with Omnigent 0.21.0 (server-managed sessions, no model call). Read [Cabins](cabins.md) first: the same warnings apply (dedicated project, low price cap,
> supervised first run).

An optional component of motel adds `motel` as an [Omnigent](https://github.com/omnigent-ai/omnigent) sandbox provider.
Omnigent starts a host (`omnigent host`) on a fresh cabin, the host dials back to your Omnigent server, and the cabin is
checked out when the session ends. The cabin's TTL, dead-man timer and price cap stay in force the whole time.

## Setup

1. Cabins working on this machine: `motel cabin run -- uname -a` ([Cabins](cabins.md)).
2. Install Omnigent with motel in the same environment. The provider needs motel 0.1.0 or newer:

   ```bash
   uv tool install omnigent --with motel                       # from PyPI
   uv tool install omnigent --with /path/to/motel-checkout     # or from a checkout
   ```

   Omnigent needs Python 3.12 or newer. motel's own code needs 3.11; the provider loads only inside Omnigent. Without
   Omnigent, `pip install motel` is unchanged: the provider files are inert and motel never imports them.

   **The provider files live in Omnigent's own package tree** (`omnigent/community/sandbox/motel/` in site-packages),
   because Omnigent loads community providers from there. Omnigent does not manage them: an Omnigent upgrade or
   uninstall does not touch them, and they import Omnigent internals (the launcher interface and its error types). After
   an Omnigent upgrade, reinstall motel in the same environment. The provider is tested with Omnigent 0.16.0 and 0.21.0.
3. Check that Omnigent sees it: `omnigent sandbox create --help` lists `motel` under `--provider`.

For server-managed sandboxes, add this to the Omnigent server config (restart the server after editing):

```yaml
sandbox:
  provider: motel
  server_url: http://127.0.0.1:6767       # the server's own URL; loopback is carried over the ssh session
  motel:                                  # every key is optional
    server_type: cpx32                    # default: the first type your cabin config allows
    ttl: 2h                               # lease length, and how far each keep-alive pushes the checkout time
    disk_gb: null
    env: [ANTHROPIC_API_KEY, OPENAI_API_KEY, GIT_TOKEN]   # NAMES of variables to pass to the host
    tunnel: auto                          # auto | relay | reverse-forward | none
    omnigent_version: auto                # auto = the version running the server; or pin, e.g. 0.21.0
    harness_packages:                     # npm packages for the coding-harness CLIs (these are the defaults)
      - "@anthropic-ai/claude-code"       # add @VERSION to each entry to pin it; [] installs none
      - "@openai/codex"
    bootstrap: pip                        # none = omnigent is already on the cabin's PATH
    wait_for_harnesses: false             # true = block the first launch until the harness CLIs are installed
    max_streams: 64                       # most connections the relay carries at once (1 to 1024)
    task: omnigent                        # label prefix on the cabin; a unique suffix is added
    motel_binary: motel                   # the motel command to run
    command_timeout_s: 1800               # longest one command on the cabin may run
    checkin_timeout_s: 1800               # longest to wait for a new cabin to be created
```

Without a `motel:` block everything takes the defaults above. The CLI path has no config block: set
`OMNIGENT_MOTEL_SANDBOX_ENV=ANTHROPIC_API_KEY,GIT_TOKEN` (names only) instead of `env`.

## Qualifying against a real cabin

The live test harness has an opt-in phase for this provider ([tests/live](https://github.com/vicennial/motel/blob/main/tests/live/README.md),
`run_live.py --omnigent`). On a throwaway test server it installs a pinned Omnigent and motel into a venv, starts a disposable Omnigent server
(loopback, throwaway home directory, no API key) with `sandbox.provider: motel`, and has a managed session
(`POST /v1/sessions` with `host_type: managed`) make the provider check in **one** cabin. It then checks that the host registers through the
relay, that a command returns its output, that `keep_alive` moves the checkout time, and that `terminate` (the session delete) checks the cabin out
until it is `released`. On every failure it saves scrubbed diagnostics, and it always deletes the session, stops the server and checks the cabin out.
No model is called. The phase costs one started cabin hour. It passes against a real Hetzner project with Omnigent 0.21.0.

## How it works

```
Omnigent server (your machine)                     cabin (disposable server)
   127.0.0.1:6767 <-- relay <-- ssh stdio --> relay listens on 127.0.0.1:6767 <-- omnigent host
```

| Omnigent calls | motel does |
|---|---|
| `provision(name)` | `motel cabin checkin --for TTL --task NAME`, then waits until the cabin accepts commands. The sandbox id is the cabin id. If the cabin never answers, it is checked out again. |
| `run`, `run_background`, `exec_foreground`, `stream_exec` | `motel cabin ssh ID -- sh -s`, with the script on stdin. Nothing sensitive is in argv. |
| `put` | `motel cabin ssh ID -- sh -c 'cat > FILE'`, then compares SHA-256 on both ends. |
| `keep_alive` | `motel cabin extend ID --for TTL`. It also checks the relay tunnel, but only one that already exists; it never starts a second relay. A refusal is logged and returns `False`; the cabin then ends at its current checkout time. |
| `is_running` | `motel cabin status`: `active` is true; `released`, `cleanup_pending` or absent is false; a provider error is unknown. |
| `terminate` | `motel cabin checkout ID`. It closes the relay tunnel first, then returns only when the cabin is `released` (or already gone). Otherwise it raises with `cleanup_pending`, so Omnigent keeps the sandbox pending and retries. A pending cabin still bills. |
| `resume(ID)` | The wake path after a server restart. It reads the cabin status. A cabin that is `released`, `cleanup_pending` or not found raises `SandboxGoneError`, and Omnigent starts a new cabin. Any other state that is not `active` raises an error. Otherwise it extends the lease and stops the host this provider started (its process group, recorded in `~/.local/state/motel-omnigent/host.pid` on the cabin, a place tmpfiles aging does not clean; without that record, the process whose command line is `.../python3 .../omnigent host ...`, which is how a pip-installed host appears). Omnigent then starts the host again, which re-opens the tunnel. |

The provider reports `resume_stopped`, so Omnigent treats a host whose cabin is still active as asleep and wakes it
instead of making a new cabin. A checked-out cabin is gone and cannot be resumed.

**The host needs omnigent installed.** A cabin is a plain Ubuntu server, not an Omnigent image. When a host is about to
start and no `omnigent` is on the cabin, the provider installs `git tmux curl python3-venv python3-pip python3-yaml`
with apt, creates a venv, runs `pip install omnigent==VERSION` and links `omnigent` into the cabin user's PATH.
`omnigent_version: auto` pins the version that runs the server, because host and server must match. It needs a release
version; for a development build set the version, or ship wheels (`omnigent sandbox create` does, from a source checkout,
and the provider installs them with their dependencies from PyPI). The cabin image needs Python 3.12 or newer
(`image = "ubuntu-24.04"`, the cabin default).

**First launch can be slow.** The first launch on a bare cabin runs apt and pip (a few minutes). Omnigent does not fail
the launch when that takes long. A message sent to the session waits up to 240 seconds for the launch to settle; if the
launch is still running then, that message gets "the session's managed sandbox is still provisioning; try again
shortly", and the launch carries on. Send the message again later. Separately, once the host process has started, it
has 120 seconds to register with the server (Omnigent's own limit; the install above runs before that clock starts).
To keep the first launch short, the provider installs Node and the harness CLIs (`harness_packages`, with `npm -g` into
the cabin user's prefix) in the background after omnigent is installed, and logs them to
`/tmp/omnigent-harness-install.log` on the cabin. So the host can start before the CLIs exist, and a very early
session may find a harness missing for a minute or two. If that install fails, it leaves
`/tmp/omnigent-harness-install.failed`. Then `keep_alive` (checked at most every ten minutes) and `resume` log a
warning (keep_alive once per cabin, resume on each wake) that the harness CLIs may be missing, a command that fails reports it, and the next launch or resume runs the
install again; the marker is cleared when it succeeds. Set `wait_for_harnesses: true` to block instead, at the cost of
a longer first launch. The fast path is a cabin
image with omnigent already installed, together with `bootstrap: none`; a baked image is planned with motel tours. On
a custom image with `bootstrap: none`, install `python3-yaml` too: the host-config writer needs it.

**Reaching your server.** The cabin firewall allows inbound ssh only from this machine, and outbound HTTP and HTTPS only
to the public internet, minus this machine's own address and private ranges. So a cabin cannot dial a server on
`127.0.0.1` or on this machine's public address. Two ways in:

- A loopback `server_url` (default `tunnel: auto`). The provider runs a small relay over the ssh session: the cabin side
  listens on `127.0.0.1:PORT` (the same port as your server) and every connection is copied to the server's loopback port
  here. No public exposure. One relay exists per cabin and server port in each process, shared by
  every launcher that Omnigent builds for that cabin. It restarts if its ssh session dies, and stops when the cabin is
  gone. After six failed restarts in a row it logs one error and gives up. The relay prints a version line first; the
  provider skips and logs any other output before it (a login banner) and refuses a different version. Each connection
  is flow controlled on its own, so one stalled connection does not block others, and the cabin refuses connections past
  `max_streams`. Use `127.0.0.1`, not `[::1]`, and a port above 1023.
- A public `server_url` such as `https://omnigent.example.com` served from a different address (a tunnel service, another
  host). The cabin dials it directly and `tunnel: auto` does nothing.

`tunnel: reverse-forward` uses `ssh -R` instead. **The stock cabin cannot do this**: its sshd has
`AllowTcpForwarding no` and the client key is `restrict`ed, so the cabin refuses and the provider reports it. It exists for
a cabin build that allows one loopback forward. The relay does the same job inside the rules a cabin already has.

## Credentials

Omnigent never copies `~/.claude` or any login. Like its Daytona and CoreWeave providers, this one passes variables **by
name**: config lists `env: [ANTHROPIC_API_KEY, ...]`, and the values are read from the environment of the process that
launches the host (the Omnigent server, or the shell running `omnigent sandbox connect`) at the moment the host starts.
A listed name that is not set fails the launch, before a cabin is created.

What the provider does:

- Values are never written to a file here and never put in argv, on this machine or on the cabin. They travel in a script
  on the stdin of `motel cabin ssh`, as `export NAME='value'` lines read by the cabin's shell.
- Error messages replace the values and the host launch token with `[redacted]`.
- Names that change how a shell or the host starts are refused: `PATH`, `HOME`, `LD_*`, `BASH_ENV`, `BASH_FUNC_*`,
  `NODE_OPTIONS`, `SHELLOPTS`, `BASHOPTS`, `PROMPT_COMMAND`, `PS4` and `OMNIGENT_*`.
- Error messages never contain the command text, and redaction covers the raw, shell-quoted and JSON-escaped forms of a
  value. Command output (stdout) that Omnigent parses is redacted only for values of 8
  characters or more, so a short setting such as `REGION=a` cannot rewrite a path; errors and stderr redact every value.

What it cannot do:

- **The key is on the cabin.** The host process and everything an agent runs there can read it (`env`, `/proc`). A key
  injected this way can be exfiltrated by any code the agent runs, over the cabin's open HTTP and HTTPS. Use a key with
  a low spending limit, scoped to this use, and revoke it afterwards. A hostile cabin user is already outside motel's
  threat model ([Cabins](cabins.md#honest-limits)).
- The launch token is in the cabin's process list while the host runs, as with every Omnigent exec provider.

The safer path is motel's concierge, an API-key proxy that is coming in motel tours: the cabin would hold a short-lived
token for a proxy on your side instead of the key, and the key would never leave your machine. Until it ships, prefer a
limited key or a subscription token (`CLAUDE_CODE_OAUTH_TOKEN`) over a full-power one.

## Limits

- A cabin that is busy is never reaped for being idle; an open tunnel counts as an admitted session. The TTL is the
  backstop. `keep_alive` pushes it forward only while the server's keep-alive loop runs, never past `max_lifetime` (at most
  14 days). After that the cabin is deleted and Omnigent starts a new sandbox on the next message. The host launch token
  lives as long as the longest cabin (`[cabins] max_lifetime` in `/etc/motel/motel.toml`, else 14 days, plus one hour).
  The provider reads this once when it loads, so restart the server after changing `max_lifetime`.
- A cabin that goes away takes its workspace with it. Push work to git from the session.
- If the Omnigent server stops, the relay stops with it. After a restart, Omnigent wakes the sandbox through `resume`
  when the next message arrives for that session, and the tunnel and host come back. If the cabin ran out in the meantime, Omnigent starts a new one. `resume` stops
  the recorded host first, so a host that was running there is ended. If the controller dies abruptly (a crash or `kill -9`),
  the old relay on the cabin can keep the port for longer than the 10 second retry; it ends when its ssh session does,
  and the next start succeeds once it has.
- Any process on the cabin can connect to the relay port, and so reach your server's loopback port. Do not run a server
  there that trusts every loopback client for something other than hosts.
- Cabin outbound is DNS, HTTP and HTTPS only: `git clone git@...` over ssh does not work, `https://` clones do.
- `omnigent sandbox create` builds wheels from an Omnigent source checkout (an Omnigent limit that applies to every
  provider). Server-managed sandboxes need no checkout.
- Tested with Omnigent 0.16.0 and 0.21.0 (Python 3.13). Earlier versions are not supported; the provider uses only the
  public launcher interface, so later ones should work.
- Whether the Omnigent web UI shows community providers in its picker was not checked in a browser. The server reports
  every configured provider that can launch (`sandbox_providers`), and `motel` is one.
