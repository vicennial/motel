#!/usr/bin/env bash
# Runs run_workload.sh several times in a row on one machine, so the job fills most of a billed hour.
# One round (build and test of CPython) took about 7 minutes on 8 vCPU, which is shorter than the point where
# an hourly server beats a per-second sandbox. Six rounds is a sustained job of about 45 minutes.
#
# Prints one `MOTEL_BENCH_RESULT {json}` line per round (from run_workload.sh) and one
# `MOTEL_BENCH_ROUNDS {json}` summary line. Exit status: 0 when every round ended "ok", else 1.
set -u

rounds=6
server_type=""
skip_deps=0
dry_run=0
jobs=""
here=$(cd "$(dirname "$0")" && pwd)

usage() {
    cat <<'EOF'
usage: run_rounds.sh [--rounds N] [--type NAME] [--jobs N] [--skip-deps] [--dry-run]

  --rounds N    how many times to run the workload, one after the other (default 6, 1 to 50).
  --type NAME   label for the machine, passed to run_workload.sh.
  --jobs N      passed to run_workload.sh: parallel jobs, for a container that reports the host's cores.
  --skip-deps   never run apt-get. Without it, only the first round installs the dependencies.
  --dry-run     pass --dry-run to every round (no fetch, no build).
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --rounds) rounds="${2:-}"; shift 2 ;;
        --type) server_type="${2:-}"; shift 2 ;;
        --skip-deps) skip_deps=1; shift ;;
        --jobs) jobs="${2:-}"; shift 2 ;;
        --dry-run) dry_run=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "run_rounds.sh: unknown argument: $1" >&2; usage >&2; exit 64 ;;
    esac
done
case "$rounds" in ''|*[!0-9]*) echo "run_rounds.sh: --rounds needs a whole number" >&2; exit 64 ;; esac
if [ "$rounds" -lt 1 ] || [ "$rounds" -gt 50 ]; then
    echo "run_rounds.sh: --rounds must be between 1 and 50" >&2; exit 64
fi

t_start=$(date +%s)
started_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
ok=0
i=1
while [ "$i" -le "$rounds" ]; do
    args=(--workdir "bench-work" --out "result-$i.json")
    [ -n "$server_type" ] && args+=(--type "$server_type")
    if [ "$skip_deps" -eq 1 ] || [ "$i" -gt 1 ]; then args+=(--skip-deps); fi
    [ -n "$jobs" ] && args+=(--jobs "$jobs")
    [ "$dry_run" -eq 1 ] && args+=(--dry-run)
    echo "run_rounds.sh: round $i of $rounds" >&2
    if "$here/run_workload.sh" "${args[@]}"; then ok=$((ok + 1)); fi
    i=$((i + 1))
done
wall=$(( $(date +%s) - t_start ))
printf 'MOTEL_BENCH_ROUNDS {"schema":"motel-bench-rounds/1","rounds":%d,"rounds_ok":%d,"wall_seconds":%d,"started_utc":"%s","finished_utc":"%s"}\n' \
    "$rounds" "$ok" "$wall" "$started_utc" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
[ "$ok" -eq "$rounds" ]
