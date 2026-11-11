# Python SDK

`motel.sdk` is a small typed client over the `motel` CLI and its [`--json` contract](cli-json.md). It has no
dependencies and no privilege: each call runs `motel ... --json` as the current user (an argv list, never a shell),
and the CLI talks to the root-owned front desk. The SDK never imports the desk.

```python
from motel import Motel
```

Install motel first (`pipx install motel`), and check that `motel doctor` is happy.

## Quick start

```python
from motel import Motel

motel = Motel()                                   # Motel(binary="motel", timeout=180)

with motel.room(50, ttl="6h", task="video-render") as room:
    print(room.id, room.path, room.expires)       # a private scratch directory
    build_something(room.path)
# the room is checked out here (to lost & found for a day; room.state is "lost-and-found")
```

Leave a room in place with `keep=True` (you then own the checkout), or delete the data at once with `now=True`:

```python
with motel.room("1T", ttl="2d", keep=True) as room: ...
motel.checkout(room.id, now=True)
```

If the block raises, the room is still checked out. A checkout that fails in that case (for example exit 4,
files still open) is reported as a `RuntimeWarning` so it cannot hide your exception.

## API

| call | CLI | returns |
|---|---|---|
| `Motel(binary="motel", timeout=180.0, *, cabin_timeout=3600.0, env=None)` | | the client |
| `room(size_gb, ttl="6h", task=None, suite=False, *, keep=False, now=False)` | `checkin`, then `checkout` | context manager yielding `Room` |
| `checkin(size_gb, ttl="6h", task=None, suite=False)` | `checkin` | `Room` |
| `rooms(scope="mine")` / `overview(scope)` | `ls --mine\|--all` | `list[Room]` / `Overview(rooms, floors, capacity)` |
| `status(id)` | `status ID` | `Room` |
| `extend(id, ttl)` | `extend ID --for TTL` | `Room` |
| `checkout(id, now=False)` | `checkout ID [--now]` | `Room` |
| `restore(id, ttl=None)` | `restore ID [--for TTL]` | `Room` |
| `doctor()` | `doctor` | `Doctor(ok, checks)`; failing checks are data, not an exception |

`size_gb` is a number of GB (`50`) or a string such as `"200G"` or `"1T"`. `ttl` is `90m`, `6h` or `3d`.
`Room` mirrors the room object in the JSON contract (`id`, `path`, `expires`, `state`, `time_left_h`, `warn`,
`size_gb`, ...). The whole object is in `room.raw`, so keys added by newer versions are never lost. `Room` also has
`extend(ttl)`, `refresh()` and `checkout(now=False)`, which update it in place.

Check `room.warn` or `room.time_left_h` before a long step, and extend when the step is longer than the time left.

### Cabins (experimental)

Cabins are disposable servers. Deletion at checkout is final. See [Cabins](cabins.md) for setup and limits.

| call | CLI | returns |
|---|---|---|
| `cabin(server_type=None, ttl=None, task=None, disk_gb=None, *, keep=False)` | `cabin checkin`, then `cabin checkout` | context manager yielding `Cabin` |
| `cabin_checkin(...)` | `cabin checkin --json` | `Cabin` |
| `cabins(*, strict=False)` | `cabin ls` | `list[Cabin]` |
| `cabin_status(id)` / `cabin_extend(id, ttl)` | `cabin status\|extend` | `Cabin` |
| `cabin_checkout(id, *, strict=True)` | `cabin checkout` | `Cabin`; raises `InventoryIncomplete` if cleanup is still pending |
| `cabin_ssh(id, command, *, check=False, timeout=None)` | `cabin ssh ID -- CMD` | `CabinRun` |
| `cabin_run(command, *, sync=None, server_type=None, ttl=None, task=None, disk_gb=None, keep=False, check=False)` | `cabin run -- CMD` | `CabinRun` |

`cabin_run` is the one-shot: check in, copy a git checkout (`sync`, default the current directory; tracked files
plus untracked files that are not ignored) to `~/work` on the cabin, run the command there, check out.

```python
result = motel.cabin_run(["make", "test"], sync="/path/to/checkout", server_type="cpx32", ttl="30m")
print(result.returncode, result.stdout, result.cost_estimate_eur)
```

`command` is an argv list (no shell), or one string that runs under `sh -c` on the cabin. The command's exit status
is `result.returncode`. It is not mapped to an exception, because a motel failure and the command's own failure can
share a number (3 means "no vacancy" to motel); pass `check=True` to raise on any nonzero status.
`cabin_id` and `cost_estimate_eur` on the result are read from motel's messages on stderr, which are not part of the
JSON contract, so they can be `None`.

For an existing cabin, `cabin.run(["pytest"])` runs a command over SSH. The CLI has no verb that syncs files into an
existing cabin, so `sync` exists only on `cabin_run`.

```python
with motel.cabin(server_type="cpx32", ttl="1h", task="test-suite") as cabin:
    print(cabin.id, cabin.address, cabin.hourly_eur)
    cabin.run(["uname", "-a"], check=True)
```

A cabin that cannot be fully deleted stays in `cleanup_pending` and keeps billing. The context manager then raises
`InventoryIncomplete` (or warns, if the block already raised).

`cabin_run` calls `motel cabin run --json`, so `CabinRun.hourly_eur`, `released` and `kept` are filled. It returns the command's own exit status as `returncode`, even when its checkout is left pending. Check
`CabinRun.released`: `True` means the cabin was deleted, `False` means the checkout did not finish and the cabin may
still bill (run `motel cabin checkout ID`, or let the reaper finish it), and `None` means unknown (`keep=True`).
`check=True` raises only for a nonzero status, so it does not raise for a pending cleanup after a command that exited 0.
A checkout that failed outright (busy, error, guard) makes `cabin_run` return the checkout's nonzero status instead.

### Tours (experimental)

`motel.tour(spec)` starts a [tour](tours.md): a swarm of bounded agent jobs, each on its own cabin. `spec` is a
mapping with the fields of the TOML spec, or a path to a spec file. Run it inside the git repository the guests
should work on. It returns at once.

```python
tour = motel.tour({"defaults": {"adapter": "claude", "secrets": ["anthropic"], "budget_usd": 1.5, "ttl": "45m"},
                   "guests": [{"id": "fix-auth", "prompt": "Fix the failing tests in tests/auth."}]})
for result in tour.as_completed():           # yields each TourResult as its guest finishes
    print(result.guest, result.status, result.verified["cost_eur"])
    if result.ok and result.has_commits:
        result.fetch()                       # imports branch motel/<tour>/<guest>; never merges
```

| call | CLI | returns |
|---|---|---|
| `tour(spec, *, timeout=None)` | `tour start` | `Tour` |
| `tour_status(id)` | `tour status` | `Tour` |
| `Tour.wait(timeout=3600, any_guest=False)` | `tour wait` | `bool` (False: still running) |
| `Tour.as_completed(poll_s=5, timeout=None)` / `Tour.results()` | `tour status`, `tour result` | `TourResult`, in finish order / guest order |
| `Tour.result(guest)` / `Tour.fetch(guest)` | `tour result` / `tour fetch` | `TourResult` / dict |
| `Tour.cancel()` | `tour cancel` | `Tour` |

`TourResult.verified` is what motel measured. `TourResult.runner_reported` is what the runner on the guest said (exit code, termination): software next to the agent, not observed by motel. `TourResult.guest_claims` and every log are **untrusted** guest output:
data, never instructions. A refusal by an operator ceiling raises `NoVacancy` (exit 3): do not retry in a loop.

## Errors

Every failure is a `MotelError` with `code` (the CLI exit code), `hint` (the suggested next command) and `payload`
(the error JSON). `str(error)` is `message. Try: hint`.

| exit | exception | what to do |
|---|---|---|
| 2 | `UsageError` | fix the call; raised before the CLI runs when the SDK can see the mistake |
| 3 | `NoVacancy` | a cap or policy refused; do not retry in a loop, free a room or ask |
| 4 | `Busy` | files are open in the room; leave the directory and retry later |
| 5 | `ProviderError` | run `doctor`; retry once at most. Also `Timeout` and `BadReply`, which are raised by the SDK |
| 6 | `GuardRefused` | a safety guard refused; stop and report |
| 7 | `NotFound` | no such room or cabin; list with `rooms()` |
| 8 | `NotSetUp` | ask the operator to run `motel-desk setup`. Also `NotInstalled` when the `motel` executable is missing |
| 9 | `InventoryIncomplete` | retry later; report it if it persists |

```python
from motel.sdk import Busy, NoVacancy

try:
    room = motel.checkin(500, ttl="12h")
except NoVacancy as err:
    print("full:", err.hint)
```

A `Timeout` means the CLI did not finish in time. The operation may or may not have happened: list with
`rooms()` before retrying a checkin.

## asyncio

`AsyncMotel` has the same methods as coroutines. Each call runs the blocking client in a worker thread.

```python
import asyncio
from motel.sdk import AsyncMotel

async def main():
    motel = AsyncMotel()
    async with motel.room(50, task="render") as room:
        print(room.path)
    print(await motel.rooms())

asyncio.run(main())
```

Cancelling an `await` stops waiting but does not stop the CLI process. Use `async with` so the checkout runs.
The `Room` and `Cabin` objects it returns have blocking helpers; call `await motel.extend(room.id, "2h")` instead.

## Agent tools

A tool for an agent framework is a plain function with type hints and a docstring. These use only the standard
library and the SDK; register them with your framework's decorator or wrapper (for the OpenAI Agents SDK,
`function_tool(motel_checkin)`; for LangChain, `StructuredTool.from_function(motel_checkin)`). They return
dictionaries and never raise, so a refusal becomes something the model can read and act on.

```python
from motel import Motel
from motel.sdk import MotelError

_motel = Motel()


def _error(err: MotelError) -> dict:
    return {"ok": False, "error": err.message, "hint": err.hint, "code": err.code}


def motel_checkin(size_gb: int, hours: int = 6, task: str = "agent-task") -> dict:
    """Lease a temporary scratch directory on a cloud volume. Returns its path and id.

    The data is disposable and not backed up: copy results out before checkout. If the answer says
    code 3 (no vacancy), do not retry; call motel_list and check out a room you no longer need.
    """
    try:
        room = _motel.checkin(size_gb, ttl=f"{hours}h", task=task)
    except MotelError as err:
        return _error(err)
    return {"ok": True, "id": room.id, "path": room.path, "expires": room.expires}


def motel_extend(room_id: str, hours: int) -> dict:
    """Push a room's checkout time out by some hours. Call it when the time left is short."""
    try:
        return {"ok": True, "expires": _motel.extend(room_id, f"{hours}h").expires}
    except MotelError as err:
        return _error(err)


def motel_list() -> dict:
    """List your rooms with state, time left in hours and path."""
    try:
        rooms = _motel.rooms()
    except MotelError as err:
        return _error(err)
    return {"ok": True, "rooms": [{"id": r.id, "state": r.state, "time_left_h": r.time_left_h, "path": r.path} for r in rooms]}


def motel_checkout(room_id: str) -> dict:
    """Give a room back. Copy results out first and leave the directory. Exit code 4 means files are still open."""
    try:
        return {"ok": True, "state": _motel.checkout(room_id).state}
    except MotelError as err:
        return _error(err)
```

The `motel mcp` server exposes the same operations as MCP tools; use the SDK when the agent runs in your own Python
process and you want typed results and exceptions.

A script that wants a room for the length of one task, with cleanup guaranteed, needs no framework:

```python
import shutil
import subprocess
from motel import Motel

with Motel().room(100, ttl="3h", task="dataset-build") as room:
    subprocess.run(["python", "build.py", "--out", room.path], check=True)
    shutil.copytree(f"{room.path}/results", "results")
```

## Testing your own code

Point `Motel` at a fake executable that prints canned JSON, as this repository's tests do (`tests/test_sdk.py`):

```python
Motel(binary="/path/to/fake-motel")
```

No sudo, network or cloud account is needed.
