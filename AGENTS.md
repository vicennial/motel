# AGENTS.md: working on the motel repository

motel is "disk and servers by the hour, for agents": TTL-bound scratch rooms on cloud volumes attached to
your own host, plus experimental disposable servers (cabins). Apache-2.0. Python 3.11+, standard library
only at runtime (no third-party dependencies). This file is for agents changing this repo. Agents *using*
motel should read `skills/motel/SKILL.md` and `docs/agents.md`.

## Layout

| Path | What |
|---|---|
| `src/motel/cli.py` | The unprivileged `motel` CLI. Talks to the front desk through a narrow sudo rule. |
| `src/motel/desk.py`, `rooms.py`, `guard.py`, `state.py`, `sysops.py` | The root-owned front desk: lifecycle, ownership guard, state, system operations. |
| `src/motel/providers/` | Volume providers (Hetzner, local loop files, in-memory fake). |
| `src/motel/tours/` | The tours engine: spec, state, pipeline, supervisor, desk verbs, CLI. `tours/guest/` and `tours/bake.py` are the guest side (`docs/tours-guest.md`); `src/motel/concierge/` is the credential proxy (`docs/concierge.md`). |
| `src/motel/mcp_server.py` | stdio MCP server; a thin bridge that runs the CLI with `--json`. No privilege. Tour tools (`tour_start`, `tour_status`, `tour_wait`, `tour_result`, `tour_fetch`, `tour_cancel`) with `--with-tours`. |
| `src/omnigent/community/sandbox/motel/` | Optional Omnigent sandbox provider (only Omnigent imports it; no `__init__.py` above it, Omnigent owns those). Tests in `tests/omnigent_provider/`. |
| `skills/motel/SKILL.md` | The agent skill (installed with `npx skills add vicennial/motel`). |
| `docs/` | `cli-json.md` (JSON contract), `lifecycle.md`, `architecture.md`, `agents.md`, ... |
| `tests/` | `tests/mcp/` for the MCP server, `tests/tours/` and `tests/concierge/` for tours; the rest cover the core. |
| `server.json` | MCP registry manifest. |

Some of these paths land with the core branch; check what exists before assuming.

## Run the checks

```bash
uvx --with pytest python -m pytest tests -q     # whole suite
uvx --with pytest python -m pytest tests/mcp -q # MCP server only (uses a fake `motel` on PATH)
uvx ruff check .
```

CI also runs the suite on macOS, where only the client side (CLI, MCP bridge, `motel try`, rooms and cabins client code) is meant to
work. Tests of the Linux-only parts (front desk, installer, Action driver, concierge, tour guest) carry `@linux_only` from
`tests/platform_marks.py`; never use it to hide a portability bug in code that is meant to run on a Mac.

Tests must not need root, a network, a cloud token or a real `motel` install. Use the fake provider, the
local loop provider with a temp directory, or a fake executable on `PATH`.

## Safety rules for the root helper

The front desk runs as root. A wrong unmount or delete is the worst outcome in this project.

- Act only on resources motel created: a root-owned creation record **and** matching labels. Never adopt,
  relabel or delete anything else. The never-touch set (volumes that existed at setup) can grow, never shrink.
- Validate every argument at the privilege boundary with strict patterns (room ids, sizes, durations).
  Never accept raw paths, provider ids or shell fragments from the caller. Never build shell strings.
- Caller identity comes from `SUDO_UID`, never from an argument.
- Write intent before every provider create so a crash can be reconciled. Check caps under the lock
  *before* the provider call.
- Never kill processes. Open files block cleanup (exit 4).
- Fail closed: when unsure, refuse with a clear message that ends `Try: <command>`.
- Do not add a force flag that bypasses the guard. Do not weaken tests of the guard to make a change pass.
- Do not add dependencies, daemons or telemetry.

## CLI contract

Stable exit codes: 0 ok, 2 usage, 3 no vacancy or policy refusal, 4 busy, 5 provider or system error,
6 safety guard, 7 not found, 8 not set up, 9 inventory incomplete. With `--json` every verb prints one JSON
object (`docs/cli-json.md`). Changing a key or an exit code is a breaking change: update the contract test,
the skill, and `src/motel/mcp_server.py` in the same change.

## Privacy rule for the tree

This is a public project. Nothing in the tree, tests, docs, examples or commit messages may contain real
hostnames or IPs, home directory paths of real machines, real account or user names, handles, tokens, or
details of a private deployment. Use synthetic values: user `alice`, task `video-render`, paths under
`/mnt/motel/...` or a temp directory, server ids like `1000001`. The pre-commit hook rejects some of these;
a passing hook is not proof the change is clean. Do not bypass it with `--no-verify`.

## Style

Small, plain Python. Type hints on public functions. Error messages start with `motel:` and end with
`Try: <command>`. Docs are short and imperative. Keep user-facing vocabulary consistent: room, cabin,
checkin, checkout, extend, restore, grace, lost & found, housekeeping.

## Tours rules

- Tour state, results and the concierge's keys are root-only; the desk answers "not found" to anyone but the owner.
- Guest output is untrusted: label it in every output, never execute or `eval` it, never check out or merge it.
- `src/motel/tours/` never reads `~/.claude`, a credential store or a keychain, and never runs `setup-token`. The
  own-login token travels on ssh stdin only and must not appear in argv, env, a file on the guest, a log or a result.
- Test basenames must be unique across `tests/` (`pythonpath` includes `tests`): tour tests start with `test_tours_`
  when a core test has the same topic. Do not add `sys.path.insert` for `tests/`.
