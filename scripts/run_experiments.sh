#!/usr/bin/env bash
# Assignment 3: run every experiment with the same workload and restore normal
# behaviour after each one. Run from the project folder with the stack up:
#   bash scripts/run_experiments.sh            # all runs (~15 min)
#   bash scripts/run_experiments.sh failure    # just one: a-only | round-robin | weighted |
#                                              #   rr-delay | lr-delay | failure
# Results: results/<run>.json (client) and results/experiments.log (timings).
set -euo pipefail
cd "$(dirname "$0")/.."

RATE=${RATE:-30}         # requests per second, per interface (HTTP and gRPC)
DURATION=${DURATION:-60} # seconds per run
DELAY=${DELAY:-0.2}      # seconds added to replica B in the least-request experiment
GAP=${GAP:-75}           # pause between runs so 1-minute dashboard windows don't overlap
LOG=results/experiments.log
mkdir -p results

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" | tee -a "$LOG"; }
envoy() { log "envoy config -> envoy-$1.yaml"; ENVOY_CONFIG="envoy-$1.yaml" docker compose up -d envoy >/dev/null 2>&1; sleep 8; }
delay_b() { docker compose run --rm tools python scripts/set_delay.py B "$1" >/dev/null 2>&1; log "replica B delay = ${1}s"; }
load() { log "run $1 start"; docker compose run --rm tools python scripts/loadtest.py --rate "$RATE" --duration "${2:-$DURATION}" --label "$1" | tee -a "$LOG"; log "run $1 end"; }
pause() { log "gap ${GAP}s"; sleep "$GAP"; }

run_a_only()      { envoy a-only; load a-only; }
run_round_robin() { envoy round-robin; load round-robin; }
run_weighted()    { envoy weighted; load weighted; }
run_rr_delay()    { envoy round-robin; delay_b "$DELAY"; load rr-delay; delay_b 0; }
run_lr_delay()    { envoy least-request; delay_b "$DELAY"; load lr-delay; delay_b 0; }
run_failure() {
  envoy round-robin
  python3 scripts/monitor_health.py homeshop-app-2 $((DURATION + 60)) > results/failure-health.log &
  local mon=$!
  load failure $((DURATION + 30)) &
  local lt=$!
  sleep 25; log "STOP B  epoch=$(date +%s)"; docker stop homeshop-app-2 >/dev/null
  sleep 30; log "START B epoch=$(date +%s)"; docker start homeshop-app-2 >/dev/null
  wait "$lt"; sleep 5; kill "$mon" 2>/dev/null || true
}

log "=== experiments: rate=${RATE}/s per interface, duration=${DURATION}s, B delay=${DELAY}s"
docker compose stop simulator >/dev/null 2>&1 && log "simulator stopped (keeps test traffic clean)"

if [[ $# -gt 0 ]]; then
  "run_${1//-/_}"
else
  run_a_only; pause; run_round_robin; pause; run_weighted; pause
  run_rr_delay; pause; run_lr_delay; pause; run_failure
fi

envoy round-robin   # restore normal behaviour
docker compose start simulator >/dev/null 2>&1 && log "simulator restarted; envoy back to round robin"
