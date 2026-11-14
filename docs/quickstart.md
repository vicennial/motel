# Quickstart

Three ways in. Pick one; they build on each other.

| Path | Time | Needs |
|---|---|---|
| [A. Try it](#a-try-it-in-30-seconds) | 30 seconds | Python 3.11+ (or `uv`). No root, no cloud, no config. |
| [B. Your own Hetzner box](#b-your-own-hetzner-box-in-5-minutes) | 5 minutes | A Linux server in a Hetzner Cloud project, root on it, a Read & Write API token. |
| [C. Wire up your agent](#c-wire-up-your-agent) | 2 minutes | Path B done, plus Claude Code, Codex or Cursor. |

## A. Try it in 30 seconds

```bash
uvx motel try
```

`motel try` plays one whole stay: check in, write a file, late checkout, forget the room, watch it pass its
checkout time, grace and lost & found, restore it, hit "no vacancy", check out. It runs the **real** rooms engine
and the real `motel` CLI in your terminal process. Only the cloud is fake (an in-memory provider), the clock is
simulated, and nothing is mounted. All state lives in a temporary folder that is deleted when the tour ends.

```bash
motel try --fast        # no pauses
motel try --json        # one machine-readable transcript (schema motel/try/v1)
motel try --no-color    # plain text; NO_COLOR works too
```

It never uses `sudo`, never reads `/etc/motel` or `/var/lib/motel`, and never mounts anything.

## B. Your own Hetzner box in 5 minutes

motel runs **on the server that needs the disk**. It attaches temporary Hetzner volumes to that server.

1. **Use a dedicated project.** In the Hetzner Cloud console, create a project that holds this server and little
   else. A Hetzner API token can see the whole project and cannot be narrowed. motel's allowlist guard keeps it
   off everything it did not create (see [what motel will never touch](#what-motel-will-never-touch)), but a
   small project is the better second wall.
2. **Create a token** in that project: Security, API tokens, permission **Read & Write**. Keep the page open; you
   paste the token once, at a hidden prompt.
3. **Install the front desk** on the server, as root (this writes a narrow sudo rule and a housekeeping timer;
   use `--dry-run --prefix /tmp/stage` first to see the plan):

   ```bash
   sudo git clone https://github.com/vicennial/motel /usr/local/src/motel && sudo /usr/local/src/motel/install.sh --users $USER --provider hetzner
   # or, from PyPI:
   sudo uvx --from motel motel install --users $USER --provider hetzner
   ```

   The installer copies code that root will run, so it refuses a source that anyone else can change: clone into
   `/usr/local/src` **as root**, not into your home directory. Never run a user-owned binary under sudo: if `sudo uvx`
   is not found, either install uv for root (a root-owned uv in `/usr/local/bin`; the one-line command is in
   [installing and upgrading](install.md)) or use the root-owned clone above.

4. **Set up** (reads this server's id and location from the metadata service, asks for the token with echo off,
   and records every volume that already exists in the project as never-touch):

   ```bash
   sudo motel-desk setup hetzner
   ```

   For automation, `--token-file PATH` takes a root-owned, non-symlink, `0600` regular file. A token is never
   accepted from an argument or the environment.
5. **Check** (read-only):

   ```bash
   motel doctor
   ```

   Every line should say `ok`. A `WARN` or `FAIL` says what is wrong and what to run.
6. **Take a room:**

   ```bash
   D=$(motel checkin 50G --for 6h --task first-room)   # stdout is only the path
   echo hello > "$D/hello.txt"
   motel ls
   motel checkout first-room-XXXX --now                # use the id shown by `motel ls`
   ```

   The first checkin creates a 50 GB volume (a **floor**), attaches, formats and mounts it, and makes your
   private room on it. After the last room is gone, housekeeping deletes the empty floor 15 minutes later.

Cost: you pay Hetzner for volumes while they exist, roughly EUR 0.05 per GB-month. Caps are checked before any
create call (defaults: 3 floors, 500 GB per floor, 1000 GB total, EUR 30 per month projected).

No cloud account? On a Linux machine where you have root, `sudo uvx --from motel motel install --provider local
&& sudo motel-desk setup local` uses sparse loop files instead of cloud volumes. Everything else behaves the same. See
[providers](providers.md).

## C. Wire up your agent

Two things help an agent: the **skill** (when and how to check in) and, optionally, the **MCP server** (typed tools
instead of shell commands). Neither holds a cloud token; the root-owned front desk does.

```bash
npx skills add vicennial/motel          # the skill: installs skills/motel/SKILL.md for the agents it finds
```

`motel mcp` runs a stdio MCP server as the same user as the agent. It runs `motel ... --json`, so it adds no
privilege. Install motel for that user first (`uv tool install motel` or `pipx install motel`) and keep
`motel doctor` green.

**Claude Code**

```bash
claude mcp add motel -- motel mcp
```

or in `.mcp.json`:

```json
{ "mcpServers": { "motel": { "command": "motel", "args": ["mcp"] } } }
```

**Codex**, in `~/.codex/config.toml`:

```toml
[mcp_servers.motel]
command = "motel"
args = ["mcp"]
```

**Cursor**, in `.cursor/mcp.json` (project) or `~/.cursor/mcp.json` (global):

```json
{ "mcpServers": { "motel": { "command": "motel", "args": ["mcp"] } } }
```

Without a local install, use `"command": "uvx"` and `"args": ["motel", "mcp"]`.

Tools: `motel_checkin`, `motel_list`, `motel_status`, `motel_extend`, `motel_checkout`, `motel_restore`,
`motel_doctor`. Add `--with-cabins` to `motel mcp` to expose the experimental cabin tools. Details and the rules
agents should follow: [agents.md](agents.md).

Some agent sandboxes block `sudo`, which the CLI needs to reach the front desk. An MCP server is started by the
agent host, outside the command sandbox, so the tools usually work where shell commands do not.

## When something fails

Every error is one line that starts with `motel:` and ends with `Try: <command>`. Run that command first. The
exit code tells you the kind of failure:

| Exit | Meaning | What to do |
|---|---|---|
| 0 | OK | |
| 2 | Usage: a bad size, duration, room id or option | Read the message; sizes are `200G`, `1T` or a number of GB, durations `90m`, `6h`, `3d`. `motel --help`. |
| 3 | **No vacancy** or another policy refusal: a cap would be exceeded, or the room is in the wrong state | Free a finished room (`motel checkout ROOM`), ask for less, or raise the caps in `/etc/motel/motel.toml`. **Never retry in a loop.** For a room in the wrong state, the message names the command (`extend` or `restore`). |
| 4 | Busy: a process has files open in the room (or a mount sits inside it) | `cd` out of the room, stop the processes that use it, retry. motel never kills processes. |
| 5 | Provider or system error: the cloud API failed, a mount or resize failed, or something internal broke | `motel doctor`. Check the token, the network and the cloud status page. A lost response is reconciled by housekeeping, so check `motel ls` before you retry a checkin. |
| 6 | A **safety guard** refused: the volume or room did not pass the ownership checks | Stop. Do not work around it. Read the message, run `motel doctor`, and report it if it persists. There is no force flag, on purpose. |
| 7 | Not found: no such room | `motel ls --mine`. Rooms in lost & found are listed too; a purged room is gone. |
| 8 | Not set up: the front desk, the config or the setup receipt is missing | `sudo uvx --from motel motel install` (or the root-owned clone, step 3) and then `sudo motel-desk setup hetzner` (or `local`), then `motel doctor`. |
| 9 | Inventory incomplete: the provider returned a partial list of volumes, so motel refuses to act on a guess | Retry in a minute. If it persists, check the token's permissions and `motel doctor`. |

With `--json`, every failure is one object: `{"ok": false, "error": "...", "hint": "...", "code": N}`. See
[cli-json.md](cli-json.md).

## What motel will never touch

motel runs as root on your server and holds a cloud token that can see a whole project, so the rule is
"only what motel made, proven twice".

- **The never-touch set.** `setup` records **every volume that already exists** in the project. The set can grow
  (by config or a later setup) and never shrinks. Those volumes are refused for every action, whatever their
  labels say.
- **Ownership labels.** motel labels each volume it creates `managed-by=motel`, `role=floor` and
  `host=<this server's id>`, and names it `motel-...`. Before every provider write the desk re-reads the volume
  and requires the labels, the name, an attachment to this server, delete protection off, and an id outside the
  never-touch set.
- **Intent records.** Before motel asks the cloud to create anything, it writes a root-owned record on this host.
  After a crash or a lost response it finds its own volume by that record's unique label. A volume that carries
  motel labels but has **no** local record is an *orphan*: reported by `motel doctor` and housekeeping, **never
  adopted, never deleted**.
- **Mounts.** A room is moved or deleted only while its floor's mount point is a mount of the floor's own
  verified device. motel never edits `/etc/fstab`.
- **Processes.** motel never kills a process. Open files block cleanup (exit 4).
- **Caps** are checked and reserved under a lock before any create or resize call.

What the sudo rule allows is narrower than root, but it is not nothing: [Architecture](architecture.md) lists the
seven user verbs, and the README says plainly what a user in `allow_users` can do.
