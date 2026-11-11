#!/usr/bin/env bash
# Driver for the motel GitHub Action (see ../action.yml and ../docs/github-action.md).
#
#   motel-action.sh init      make a private state directory and a unique task name
#   motel-action.sh prepare   validate inputs, install and set up motel if needed, check that it works
#   motel-action.sh run       run the command (on a cabin, or in a room) and write the outputs
#   motel-action.sh cleanup   check out whatever this invocation leased; remove the token file
#
# Inputs arrive only as environment variables, never spliced into this script. The Hetzner token is only ever
# in HETZNER_TOKEN (environment) and then in a root-owned 0600 file; it is never an argument to any command.
set -euo pipefail

SUDO=${MOTEL_ACTION_SUDO:-sudo}
DESK=${MOTEL_ACTION_DESK:-/usr/local/sbin/motel-desk}
REPO_URL=${MOTEL_ACTION_REPO_URL:-https://github.com/vicennial/motel.git}

MODE=${MODE:-cabin-run}
COMMAND=${COMMAND:-}
SERVER_TYPE=${SERVER_TYPE:-}
TTL=${TTL:-1h}
SIZE=${SIZE:-20}
SYNC_PATH=${SYNC_PATH:-}
MOTEL_VERSION=${MOTEL_VERSION:-}
KEEP_ROOM=${KEEP_ROOM:-false}
FAIL_ON_ERROR=${FAIL_ON_ERROR:-true}
STATE_DIR=${STATE_DIR:-}
TASK=${TASK:-}
STAGED=
ACTION_PATH=${ACTION_PATH:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
GITHUB_OUTPUT=${GITHUB_OUTPUT:-/dev/null}

die() {
  echo "::error::motel: $*" >&2
  exit 2
}

output() { printf '%s=%s\n' "$1" "$2" >>"$GITHUB_OUTPUT"; }

# JSON helper: python3 is needed by motel itself (3.11 or newer), so it is always there.
pick() { python3 -I -c "$1" "${@:2}"; }

provider() {
  if [ -n "${HETZNER_TOKEN:-}" ] || [ "$MODE" = cabin-run ]; then echo hetzner; else echo local; fi
}

# ---------------------------------------------------------------------------------------------------- init
cmd_init() {
  local base state task rand
  base=${RUNNER_TEMP:-${TMPDIR:-/tmp}}
  state=$(mktemp -d "$base/motel-action.XXXXXX")
  chmod 700 "$state"
  rand=$(od -An -N2 -tx1 /dev/urandom | tr -d ' \n')
  task="gha-${GITHUB_RUN_ID:-0}-${GITHUB_RUN_ATTEMPT:-1}-$rand"
  output state "$state"
  output task "$task"
}

# -------------------------------------------------------------------------------------------------- prepare
validate() {
  case "$MODE" in cabin-run | room) ;; *) die "mode must be cabin-run or room, not '$MODE'" ;; esac
  [[ $TTL =~ ^[1-9][0-9]{0,3}[mhd]$ ]] || die "ttl must be minutes, hours or days such as 90m, 2h or 1d"
  [[ $SIZE =~ ^[1-9][0-9]{0,5}[GT]?$ ]] || die "size must be a number of GB, or 200G or 1T"
  [[ -z $SERVER_TYPE || $SERVER_TYPE =~ ^[a-z0-9][a-z0-9-]{0,39}$ ]] || die "server-type looks wrong (for example cpx32)"
  [[ -z $MOTEL_VERSION || $MOTEL_VERSION =~ ^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$ ]] || die "motel-version must be a git tag or branch name"
  case "$KEEP_ROOM" in true | false) ;; *) die "keep-room must be true or false" ;; esac
  case "$FAIL_ON_ERROR" in true | false) ;; *) die "fail-on-error must be true or false" ;; esac
  [ -n "$STATE_DIR" ] && [ -d "$STATE_DIR" ] || die "internal: the state directory is missing (the init step did not run)"
  [[ $TASK =~ ^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$ ]] || die "internal: bad task name"
  if [ "$MODE" = cabin-run ]; then
    [ -n "$COMMAND" ] || die "command is required in cabin-run mode"
    git -C "${SYNC_PATH:-.}" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
      || die "sync-path must be a git checkout (cabin-run copies the tracked and the not-ignored untracked files)"
  else
    [ -n "$COMMAND" ] || [ "$KEEP_ROOM" = true ] || die "room mode needs a command, or keep-room: true"
    [ -z "$SYNC_PATH" ] || [ -d "$SYNC_PATH" ] || die "sync-path is not a directory"
  fi
}

# The installer copies code that root will later run, so it refuses a source anybody else can change (docs/install.md).
# The action's checkout and a fresh clone belong to the runner user, so stage a root-owned copy first: the whole parent
# chain root-owned, nothing group- or world-writable. The stage is replaced on every run (reruns are safe), but only a
# directory this script made: it carries a marker file, and without the marker nothing is removed.
STAGE_MARKER=.motel-action-stage

stage_path() { # print the stage directory, or stop: a root-run `rm -rf` must never aim at an arbitrary path
  local dest=${MOTEL_ACTION_STAGE:-/usr/local/src/motel-action}
  if [ "${MOTEL_ACTION_TEST_MODE:-}" != 1 ]; then # only the tests may aim the stage somewhere else
    case "$dest" in
      /*) ;;
      *) die "the stage path must be absolute: $dest" ;;
    esac
    case "$dest" in
      *//* | */./* | */../* | */. | */.. | */) die "the stage path must be normalised (no '.', '..', '//' or trailing '/'): $dest" ;;
    esac
    [ "$(dirname "$dest")" = /usr/local/src ] || die "the stage path must be a direct child of /usr/local/src: $dest"
    case "$(basename "$dest")" in
      motel-?*) ;;
      *) die "the stage directory name must start with 'motel-': $dest" ;;
    esac
  fi
  [ ! -L "$dest" ] || die "the stage path is a symbolic link: $dest"
  [ ! -L "$(dirname "$dest")" ] || die "the parent of the stage path is a symbolic link: $(dirname "$dest")"
  printf '%s' "$dest"
}

stage_source() {
  local from=$1 dest parent
  dest=$(stage_path)
  parent=$(dirname "$dest")
  if [ -e "$dest" ]; then
    [ -d "$dest" ] && [ -f "$dest/$STAGE_MARKER" ] && [ ! -L "$dest/$STAGE_MARKER" ] \
      || die "refusing to remove $dest: it has no $STAGE_MARKER marker, so this script did not make it"
    $SUDO rm -rf "$dest"
  fi
  [ -d "$parent" ] || $SUDO install -d -o root -g root -m 0755 "$parent" # an existing parent keeps its owner and mode
  $SUDO install -d -o root -g root -m 0755 "$dest"
  $SUDO install -o root -g root -m 0644 /dev/null "$dest/$STAGE_MARKER"
  $SUDO cp -R "$from/install.sh" "$from/src" "$dest/"
  [ ! -d "$from/skills" ] || $SUDO cp -R "$from/skills" "$dest/"
  $SUDO chown -R root:root "$dest"
  $SUDO chmod -R go-w "$dest"
  STAGED=$dest
}

# The installer also refuses to put root-run code (the desk, the CLI, /usr/local/lib/motel) under a directory that anybody
# else can write, because whoever can write there can replace what root runs. GitHub-hosted Ubuntu runners ship directories
# such as /usr/local/bin with mode 777, so on those the installer refuses with "writable by group or others". Its check is
# right and is not weakened. Instead, once the install is about to happen, this removes group and other write permission
# from the install directories and from every directory above them (only where root owns them and they are not links; an
# owner other than root is left for the installer to refuse). That is a deliberate change to the runner's own filesystem,
# acceptable because the runner is a throwaway machine and sudo is already needed to install. Steps that later write to one
# of these directories without sudo will no longer be able to (docs/github-action.md).
INSTALL_DIRS=(/usr/local/lib/motel /usr/local/sbin /usr/local/bin)

tighten_install_dirs() {
  local dir path top=/ owner=0
  local -a dirs=("${INSTALL_DIRS[@]}")
  if [ "${MOTEL_ACTION_TEST_MODE:-}" = 1 ]; then # only the tests may aim this somewhere else (there is no root there)
    dirs=()
    # shellcheck disable=SC2206  # a space-separated list of test paths
    [ -z "${MOTEL_ACTION_INSTALL_DIRS:-}" ] || dirs=($MOTEL_ACTION_INSTALL_DIRS)
    top=${MOTEL_ACTION_INSTALL_TOP:-/}
    owner=${MOTEL_ACTION_OWNER_UID:-0}
  fi
  [ "${#dirs[@]}" -gt 0 ] || return 0   # bash before 4.4 treats an empty "${dirs[@]}" as unbound under set -u
  for dir in "${dirs[@]}"; do
    path=$dir
    while [ "$path" != "$top" ] && [ "$path" != . ]; do
      # a directory that does not exist yet is created by the installer with mode 0755; a link is the installer's to judge
      if [ -d "$path" ] && [ ! -L "$path" ] && [ -n "$(find "$path" -maxdepth 0 -type d -user "$owner" \( -perm -020 -o -perm -002 \) -print 2>/dev/null)" ]; then
        echo "motel: removing group and other write permission from $path (the installer refuses root-run code under a writable directory)"
        $SUDO chmod go-w "$path"
      fi
      [ "$path" != / ] || break
      path=$(dirname "$path")
    done
  done
}

install_motel() {
  local need=false src=$ACTION_PATH
  command -v motel >/dev/null 2>&1 || need=true
  [ -z "$MOTEL_VERSION" ] || need=true
  [ "$need" = true ] || return 0
  if [ -n "$MOTEL_VERSION" ]; then
    src="$STATE_DIR/motel-src"
    echo "motel: fetching $MOTEL_VERSION from $REPO_URL"
    git clone --quiet --depth 1 --branch "$MOTEL_VERSION" "$REPO_URL" "$src"
  fi
  [ -f "$src/install.sh" ] || die "no install.sh in $src"
  [ -d "$src/src/motel" ] || die "no src/motel in $src"
  stage_source "$src"
  tighten_install_dirs
  echo "motel: installing the front desk (provider $(provider)) from $STAGED"
  $SUDO sh "$STAGED/install.sh" --users "$(id -un)" --provider "$(provider)"
  hash -r
  touch "$STATE_DIR/fresh-install"
}

write_token_file() {
  # root-owned, mode 0600, created before any byte of the token is written; the token comes in on stdin.
  local file=$STATE_DIR/hetzner.token
  $SUDO install -m 0600 -o root -g root /dev/null "$file"
  printf '%s' "$HETZNER_TOKEN" | $SUDO tee "$file" >/dev/null
  echo "$file"
}

setup_desk() {
  local token_file
  if [ -n "${HETZNER_TOKEN:-}" ]; then
    token_file=$(write_token_file)
    if [ "$MODE" = cabin-run ]; then
      $SUDO "$DESK" cabin setup hetzner --token-file "$token_file"
    else
      $SUDO "$DESK" setup hetzner --token-file "$token_file"
    fi
    unset HETZNER_TOKEN
  elif [ -e "$STATE_DIR/fresh-install" ] && [ "$MODE" = room ]; then
    $SUDO "$DESK" setup local
  fi
}

preflight() {
  local out rc=0
  if [ "$MODE" = cabin-run ]; then
    [ -x /usr/bin/rsync ] && [ -x /usr/bin/ssh ] && [ -x /usr/bin/ssh-keygen ] \
      || die "cabin-run needs rsync and an ssh client on the runner (apt-get install rsync openssh-client)"
    out=$(motel cabin ls --json 2>&1) || rc=$?
    # exit 9 only means the provider inventory is incomplete; the cabin verbs work.
    if [ "$rc" -ne 0 ] && [ "$rc" -ne 9 ]; then
      die "motel cabin ls failed (exit $rc): $out. Cabins need a Hetzner Cloud runner with [cabins] configured; see docs/github-action.md"
    fi
  else
    out=$(motel doctor --json 2>&1) || rc=$?
    [ "$rc" -eq 0 ] || die "motel doctor failed (exit $rc): $out"
  fi
}

cmd_prepare() {
  validate
  install_motel
  setup_desk
  preflight
  echo "motel: ready (mode $MODE, task $TASK)"
}

# ------------------------------------------------------------------------------------------------------ run
finish() { # finish RC: write the outputs, then fail the step when asked to
  local rc=$1
  output exit-code "$rc"
  if [ "$rc" -ne 0 ] && [ "$FAIL_ON_ERROR" = true ]; then
    echo "::error::motel: the command exited with status $rc"
    exit "$rc"
  fi
}

run_cabin() {
  local log=$STATE_DIR/stderr.log rcfile=$STATE_DIR/rc rc cabin cost
  local -a argv=(cabin run --for "$TTL" --task "$TASK")
  touch "$STATE_DIR/leasing" # from here on, cleanup must look for this task's lease
  [ -z "$SERVER_TYPE" ] || argv+=(--type "$SERVER_TYPE")
  argv+=(-- sh -c "$COMMAND")
  cd "${SYNC_PATH:-.}"
  set +e
  { motel "${argv[@]}" 2>&1 1>&3 3>&- | tee "$log" >&2; echo "${PIPESTATUS[0]}" >"$rcfile"; } 3>&1
  set -e
  rc=$(cat "$rcfile")
  cabin=$(sed -n 's/^motel: cabin \([a-z0-9-]*\) .*/\1/p' "$log" | head -n 1)
  cost=$(sed -n 's/.*est\. cost EUR \([0-9.]*\) net.*/\1/p' "$log" | tail -n 1)
  output cabin-id "$cabin"
  output cost-estimate "$cost"
  finish "$rc"
}

run_room() {
  local log=$STATE_DIR/stderr.log out rc=0 id path
  touch "$STATE_DIR/leasing"
  if ! out=$(motel checkin "$SIZE" --for "$TTL" --task "$TASK" --json 2>"$log"); then
    cat "$log" >&2
    die "motel checkin failed: $out"
  fi
  id=$(printf '%s' "$out" | pick 'import json,sys; print(json.load(sys.stdin)["room"]["id"])')
  path=$(printf '%s' "$out" | pick 'import json,sys; print(json.load(sys.stdin)["room"]["path"])')
  output room-id "$id"
  output room-path "$path"
  output cost-estimate ""
  if [ -n "$SYNC_PATH" ]; then cp -a -- "$SYNC_PATH"/. "$path"/; fi
  if [ -n "$COMMAND" ]; then
    set +e
    (cd "$path" && MOTEL_ROOM_ID=$id MOTEL_ROOM_PATH=$path bash -c "$COMMAND")
    rc=$?
    set -e
  fi
  if [ "$KEEP_ROOM" = true ]; then
    echo "motel: keeping room $id at $path. Check it out with: motel checkout $id --now"
  fi
  finish "$rc"
}

cmd_run() {
  if [ "$MODE" = cabin-run ]; then run_cabin; else run_room; fi
}

# --------------------------------------------------------------------------------------------------- cleanup
LEASED=()
leased() { # leased KIND: fills LEASED with the ids of this invocation's leases that are still live; 1 if unknown
  local kind=$1 doc ids rc=0
  LEASED=()
  if [ "$kind" = cabin ]; then
    doc=$(motel cabin ls --json 2>/dev/null) || rc=$?
    [ "$rc" -eq 0 ] || [ "$rc" -eq 9 ] || return 1 # exit 9: inventory incomplete, the records are still listed
    ids=$(printf '%s' "$doc" | pick '
import json, sys
task = sys.argv[1]
for c in json.load(sys.stdin).get("cabins", []):
    if c.get("task") == task and c.get("state") != "released":
        print(c["lease_id"])' "$TASK") || return 1
  else
    doc=$(motel ls --mine --json 2>/dev/null) || return 1
    ids=$(printf '%s' "$doc" | pick '
import json, sys
task = sys.argv[1]
for r in json.load(sys.stdin).get("rooms", []):
    if r.get("task") == task and r.get("state") in ("active", "grace"):
        print(r["id"])' "$TASK") || return 1
  fi
  # shellcheck disable=SC2206  # ids are validated lease ids, one per line
  [ -z "$ids" ] || LEASED=($ids)
}

checkout_room() { # checkout_room ID: files held open give exit 4, so retry a few times
  local attempt
  for attempt in 1 2 3 4 5; do
    motel checkout "$1" --now --json >/dev/null && return 0
    sleep 3
  done
  return 1
}

cmd_cleanup() {
  local status=0 id
  set +e
  if [ -n "$STATE_DIR" ] && [ -e "$STATE_DIR/leasing" ] && [ -n "$TASK" ] && command -v motel >/dev/null 2>&1; then
    if [ "$MODE" = cabin-run ]; then
      if leased cabin; then
        for id in "${LEASED[@]}"; do
          echo "motel: checking out cabin $id"
          motel cabin checkout "$id" --json >/dev/null \
            || { echo "::error::motel: cabin $id is NOT checked out and still bills. Try: motel cabin checkout $id"; status=1; }
        done
      else
        echo "::error::motel: cannot list cabins, so none was checked out. Try: motel cabin ls (task $TASK)"
        status=1
      fi
    elif [ "$KEEP_ROOM" != true ]; then
      if leased room; then
        for id in "${LEASED[@]}"; do
          echo "motel: checking out room $id"
          checkout_room "$id" || echo "::warning::motel: room $id is not checked out; it expires on its own. Try: motel checkout $id --now"
        done
      else
        echo "::warning::motel: cannot list rooms, so none was checked out. Try: motel ls --mine (task $TASK)"
      fi
    fi
  fi
  if [ -n "$STATE_DIR" ] && [ -d "$STATE_DIR" ]; then
    $SUDO rm -f -- "$STATE_DIR/hetzner.token"
    rm -rf -- "$STATE_DIR"
  fi
  exit "$status"
}

case "${1:-}" in
  init) cmd_init ;;
  prepare) cmd_prepare ;;
  run) cmd_run ;;
  cleanup) cmd_cleanup ;;
  *) die "usage: motel-action.sh init|prepare|run|cleanup" ;;
esac
