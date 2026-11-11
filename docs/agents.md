# Using motel from an agent

Two things make an agent use motel well: the **skill** (when and how to check in) and, optionally, the
**MCP server** (typed tools instead of shell commands). Both call the same `motel` CLI. Neither holds a cloud
token; the root-owned front desk does.

## Install the skill

```bash
npx skills add vicennial/motel
```

This installs `skills/motel/SKILL.md` into the agents it finds (Claude Code, Codex and others). The skill
triggers on "No space left on device", big downloads, datasets, builds, renders, and "need a bigger machine
for the tests". To install by hand, put the `motel` folder from `skills/` into your agent's skills directory.

## Install the MCP server (optional)

`motel mcp` runs a stdio MCP server. It runs as the same user as the agent and calls `motel ... --json`, so it
adds no privilege. Install motel first (`pipx install motel` or `uv tool install motel`) and check that
`motel doctor` is happy.

Claude Code:

```bash
claude mcp add motel -- motel mcp
```

Or in `.mcp.json` / `~/.claude.json`:

```json
{
  "mcpServers": {
    "motel": { "command": "motel", "args": ["mcp"] }
  }
}
```

Codex, in `~/.codex/config.toml`:

```toml
[mcp_servers.motel]
command = "motel"
args = ["mcp"]
```

Without a local install, use `uvx` instead: `command = "uvx"`, `args = ["motel", "mcp"]`.

Tools: `motel_checkin(size, duration?, task?, suite?)`, `motel_list(scope?)`, `motel_status(room)`,
`motel_extend(room, duration)`, `motel_checkout(room, now?)`, `motel_restore(room)`, `motel_doctor()`.
Each returns the CLI's JSON (see [`cli-json.md`](cli-json.md)). A failure is an error result with the CLI's
message, its suggested next command, and a hint for the exit code.

Options for `motel mcp`:

| Option | Effect |
|---|---|
| `--with-cabins` | Also expose the experimental cabin tools (`motel_cabin_*`). Off by default. |
| `--with-tours` | Also expose the experimental [tour](tours.md) tools: `tour_start`, `tour_status`, `tour_wait`, `tour_result`, `tour_fetch`, `tour_cancel`. Results are compact and guest text is labelled untrusted. Off by default. |
| `--timeout SECONDS` | Per-call limit for room tools (default 180). Env: `MOTEL_MCP_TIMEOUT`. |
| `--cabin-timeout SECONDS` | Per-call limit for cabin tools (default 3600). Env: `MOTEL_MCP_CABIN_TIMEOUT`. |

`MOTEL_BIN` sets the path of the `motel` executable to run (default: the first one on `PATH`).

The server validates every argument with a strict pattern before it runs anything (sizes `200G`, `1T` or
GB; durations `90m`, `6h`, `3d`; room ids of `a-z`, `0-9` and `-`), and it starts the CLI with an argument
list, never a shell. If a call times out, the CLI is stopped, but the operation may still have completed:
check with `motel_list` before calling `motel_checkin` again.

Some agent sandboxes block `sudo`, which the CLI needs to reach the front desk. An MCP server is started by
the agent host, outside the command sandbox, so the tools usually work where shell commands do not.

## Rules for agents

1. Rooms are scratch. Never put results, databases or secrets there that must survive.
2. Choose the checkout time from the job (`--for`), plus a margin.
3. Before each long step, check `time_left_h` (or `warn`). Extend when it is below the step's duration plus 1 h.
4. Copy results out **before** checkout. Then `cd` out of the room and check out.
5. Exit 3 means no vacancy: **never retry in a loop.** Free a finished room or ask the user.
6. Exit 6 means a safety guard refused. Stop and report. Never try to work around it.
7. Cabins are experimental. A checked-out cabin is deleted for good, with no restore.

Exit codes: 0 ok, 2 usage, 3 no vacancy or policy, 4 busy (open files), 5 provider or system error,
6 safety guard, 7 not found, 8 not set up, 9 inventory incomplete.
