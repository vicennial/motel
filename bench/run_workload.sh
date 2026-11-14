#!/usr/bin/env bash
# Benchmark workload: build CPython from a pinned tag with its profile-guided optimisation, then run its
# full test suite. CPU bound, deterministic input, no network after the fetch step, no secrets.
# Records wall time, CPU count, RAM, CPU model and exit status to a JSON file (result.json by default) and
# prints the same JSON on one stdout line that starts with MOTEL_BENCH_RESULT.
#
#   ./run_workload.sh --type cpx42
#   ./run_workload.sh --dry-run            # a tiny stand-in command: tests the recording path, takes a second
#
# See bench/README.md for why this workload was chosen and how to run it on a cabin.
set -u

TAG="v3.13.9"
COMMIT="8183fa5e3f78ca6ab862de7fb8b14f3d929421e0"
REPO_URL="https://github.com/python/cpython.git"
SCHEMA="motel-bench-result/1"
MARKER="MOTEL_BENCH_RESULT"
TEST_TIMEOUT=900

server_type=""
workdir="./bench-work"
out="result.json"
dry_run=0
skip_deps=0

usage() {
    cat <<'EOF'
usage: run_workload.sh [--type NAME] [--workdir DIR] [--out FILE] [--skip-deps] [--dry-run]

  --type NAME     label for the machine, for example cpx42. It is recorded as server_type in the result.
  --workdir DIR   where the clone, logs and build go (default ./bench-work). It is deleted first.
  --out FILE      where to write the result JSON (default result.json).
  --skip-deps     do not run apt-get; the build tools and -dev libraries must already be installed.
  --dry-run       do not fetch or build anything. Run a one-second stand-in command and record the result.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --type) server_type="${2:-}"; shift 2 ;;
        --workdir) workdir="${2:-}"; shift 2 ;;
        --out) out="${2:-}"; shift 2 ;;
        --skip-deps) skip_deps=1; shift ;;
        --dry-run) dry_run=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "run_workload.sh: unknown argument: $1" >&2; usage >&2; exit 64 ;;
    esac
done

if [[ ! "$server_type" =~ ^[A-Za-z0-9._-]{0,64}$ ]]; then
    echo "run_workload.sh: --type may contain only letters, digits, dot, underscore and hyphen" >&2
    exit 64
fi
if [ -z "$workdir" ] || [ "$workdir" = "/" ] || [ "$workdir" = "." ]; then
    echo "run_workload.sh: refusing to use '$workdir' as the work directory" >&2
    exit 64
fi

# ---- helpers ---------------------------------------------------------------------------------------------

now() { date +%s; }
utc() { date -u +%Y-%m-%dT%H:%M:%SZ; }

json_str() {  # print a JSON string literal for $1
    local s=${1//\\/\\\\}
    s=${s//\"/\\\"}
    s=$(printf '%s' "$s" | tr -d '\000-\037')
    printf '"%s"' "$s"
}

cpu_count() {
    nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 1
}

mem_kib() {
    if [ -r /proc/meminfo ]; then
        awk '/^MemTotal:/ {print $2; exit}' /proc/meminfo
    else
        local bytes
        bytes=$(sysctl -n hw.memsize 2>/dev/null || echo 0)
        echo $((bytes / 1024))
    fi
}

cpu_model() {
    local m=""
    if [ -r /proc/cpuinfo ]; then
        m=$(awk -F': ' '/^model name/ {print $2; exit}' /proc/cpuinfo)
    fi
    [ -z "$m" ] && m=$(sysctl -n machdep.cpu.brand_string 2>/dev/null || true)
    [ -z "$m" ] && m=unknown
    printf '%s' "$m"
}

os_name() {
    local n=""
    if [ -r /etc/os-release ]; then
        # shellcheck source=/dev/null
        n=$(. /etc/os-release && printf '%s' "${PRETTY_NAME:-}")
    fi
    [ -z "$n" ] && n=$(uname -s)
    printf '%s' "$n"
}

compiler_version() {
    (cc --version 2>/dev/null || true) | head -n 1
}

as_root() {
    if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo -n "$@"; fi
}

# ---- state -----------------------------------------------------------------------------------------------

cpus=$(cpu_count)
mem=$(mem_kib)
mem=${mem:-0}
model=$(cpu_model)
kernel=$(uname -sr)
os=$(os_name)
compiler=""
started_utc=$(utc)
t_start=$(now)
fetch_s=0
build_s=0
test_s=0
build_rc=0
test_rc=0
tests_result=""
tests_run=""
status="ok"
commit_seen=""

write_result() {
    local wall=$(( $(now) - t_start ))
    local exit_status=0
    case "$status" in
        ok|dry_run) exit_status=0 ;;
        tests_failed) exit_status=2 ;;
        build_failed) exit_status=1 ;;
        *) exit_status=1 ;;
    esac
    local doc
    doc=$(printf '{"schema":"%s","workload":"cpython-pgo-build-and-test","tag":"%s","commit":%s,"dry_run":%s,"status":"%s","exit_status":%d,"build_exit_status":%d,"test_exit_status":%d,"server_type":%s,"cpus":%d,"mem_total_kib":%d,"cpu_model":%s,"kernel":%s,"os":%s,"compiler":%s,"started_utc":"%s","finished_utc":"%s","fetch_seconds":%d,"build_seconds":%d,"test_seconds":%d,"wall_seconds":%d,"tests_result":%s,"tests_run":%s}' \
        "$SCHEMA" "$TAG" "$(json_str "$commit_seen")" "$([ "$dry_run" -eq 1 ] && echo true || echo false)" "$status" "$exit_status" \
        "$build_rc" "$test_rc" "$([ -n "$server_type" ] && json_str "$server_type" || echo null)" "$cpus" "$mem" \
        "$(json_str "$model")" "$(json_str "$kernel")" "$(json_str "$os")" \
        "$([ -n "$compiler" ] && json_str "$compiler" || echo null)" "$started_utc" "$(utc)" "$fetch_s" "$build_s" "$test_s" "$wall" \
        "$([ -n "$tests_result" ] && json_str "$tests_result" || echo null)" "$([ -n "$tests_run" ] && echo "$tests_run" || echo null)")
    printf '%s\n' "$doc" > "$out"
    printf '%s %s\n' "$MARKER" "$doc"
    echo "run_workload.sh: wrote $out (status $status, wall ${wall}s, ${cpus} CPUs)" >&2
    return "$exit_status"
}

# ---- dry run: exercises the recording path only ----------------------------------------------------------

if [ "$dry_run" -eq 1 ]; then
    status="dry_run"
    t0=$(now)
    sleep 1
    build_s=$(( $(now) - t0 ))
    commit_seen="dry-run"
    write_result
    exit $?
fi

# ---- fetch step: needs the network, is timed separately --------------------------------------------------

# Start from an empty work directory. Delete only a directory this script made, never an arbitrary one.
if [ -e "$workdir" ]; then
    if [ -f "$workdir/.motel-bench-workdir" ]; then
        rm -rf "$workdir"
    elif [ -d "$workdir" ] && [ -z "$(ls -A "$workdir")" ]; then
        :
    else
        echo "run_workload.sh: $workdir exists and was not made by this script; pick another --workdir" >&2
        exit 64
    fi
fi
mkdir -p "$workdir" || exit 1
touch "$workdir/.motel-bench-workdir"
workdir=$(cd "$workdir" && pwd)
orig_dir=$(pwd)
t0=$(now)

if [ "$skip_deps" -eq 0 ]; then
    echo "run_workload.sh: installing build dependencies (apt-get)" >&2
    export DEBIAN_FRONTEND=noninteractive
    if ! { as_root apt-get update -qq && as_root apt-get install -y -qq --no-install-recommends \
        build-essential git ca-certificates pkg-config libssl-dev zlib1g-dev libbz2-dev libreadline-dev \
        libsqlite3-dev libncurses-dev libffi-dev liblzma-dev uuid-dev libgdbm-dev libgdbm-compat-dev libzstd-dev; } \
        > "$workdir/deps.log" 2>&1; then
        echo "run_workload.sh: dependency install failed; see $workdir/deps.log (use --skip-deps if the tools are already installed)" >&2
        status="build_failed"; build_rc=1
        fetch_s=$(( $(now) - t0 ))
        write_result; exit $?
    fi
fi

echo "run_workload.sh: cloning $TAG" >&2
if ! git clone --quiet --depth 1 --branch "$TAG" "$REPO_URL" "$workdir/cpython" > "$workdir/clone.log" 2>&1; then
    echo "run_workload.sh: clone failed; see $workdir/clone.log" >&2
    status="build_failed"; build_rc=1
    fetch_s=$(( $(now) - t0 ))
    write_result; exit $?
fi
commit_seen=$(git -C "$workdir/cpython" rev-parse HEAD)
fetch_s=$(( $(now) - t0 ))
if [ "$commit_seen" != "$COMMIT" ]; then
    echo "run_workload.sh: tag $TAG points at $commit_seen, expected $COMMIT. Refusing to run a different source tree." >&2
    status="build_failed"; build_rc=3
    write_result; exit $?
fi

# ---- timed step: no network from here on -----------------------------------------------------------------

export LC_ALL=C.UTF-8
compiler=$(compiler_version)
cd "$workdir/cpython" || exit 1

echo "run_workload.sh: configure and build with -j$cpus (profile-guided: includes a serial profile run)" >&2
t0=$(now)
if ./configure --enable-optimizations > "$workdir/configure.log" 2>&1 && make -j"$cpus" > "$workdir/build.log" 2>&1; then
    build_rc=0
else
    build_rc=$?
    [ "$build_rc" -eq 0 ] && build_rc=1
fi
build_s=$(( $(now) - t0 ))

if [ "$build_rc" -ne 0 ]; then
    echo "run_workload.sh: build failed (exit $build_rc); see $workdir/configure.log and $workdir/build.log" >&2
    status="build_failed"
    cd "$orig_dir" || true
    write_result; exit $?
fi

echo "run_workload.sh: running the full test suite with -j$cpus" >&2
t0=$(now)
./python -m test -j"$cpus" --timeout="$TEST_TIMEOUT" > "$workdir/tests.log" 2>&1
test_rc=$?
test_s=$(( $(now) - t0 ))
tests_result=$(awk '/^Result: / {sub(/^Result: /, ""); print; exit}' "$workdir/tests.log")
tests_run=$(sed -n 's/^Total tests: run=\([0-9][0-9]*\).*/\1/p' "$workdir/tests.log" | head -n 1)

if [ "$test_rc" -ne 0 ]; then
    status="tests_failed"
    echo "run_workload.sh: the test run exited $test_rc; see $workdir/tests.log. The time is still recorded." >&2
fi

cd "$orig_dir" || true
write_result
exit $?
