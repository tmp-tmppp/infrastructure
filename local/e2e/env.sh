# Shared settings of the end-to-end run; sourced by run.sh and thermal-watch.sh (never run on its own).
# Every path is relative to this folder; nothing names a machine or a user.
# shellcheck disable=SC2034  # the variables are read by the scripts that source this file
E2E_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_DIR="$(dirname "$E2E_DIR")"                 # infrastructure/local: compose.yaml of the dependencies
RSO_ROOT="$(cd "$LOCAL_DIR/../.." && pwd)"        # the folder that holds the service repositories side by side
LOGS="$E2E_DIR/logs"
PIDS_FILE="$E2E_DIR/pids.txt"
PROGRESS="$LOGS/progress.log"

# Optional, from the environment (README.md):
#   HEAVY_LOCK=<file>      run compose up and the service starts under flock on this file (one heavy job at a time)
#   THERMAL_WATCH=1        the thermal gate and watcher (one laptop's kernel log; README.md)
#   E2E_CPU_QUOTA=<n>%     CPU cap of each service's systemd scope (default 25%; empty: no scope, no cap)
#   PAYMENT_PROVIDER_IMAGE a prebuilt image of the mock provider (default: compose builds chance-payment-provider:local)
#   DOCKER_CONFIG, DOCKER_HOST are Docker's own settings and are passed through unchanged.
HEAVY_LOCK="${HEAVY_LOCK:-}"
THERMAL_WATCH="${THERMAL_WATCH:-0}"
E2E_CPU_QUOTA="${E2E_CPU_QUOTA-25%}"
COMPOSE=(docker compose -f "$LOCAL_DIR/compose.yaml" -f "$E2E_DIR/compose.override.yaml")

KC="http://localhost:8081"
ISSUER="$KC/realms/chance"
JWKS="$ISSUER/protocol/openid-connect/certs"
NATS="nats://localhost:4222"
AUTH_ENV=(AUTH_JWKS_URL="$JWKS" AUTH_ISSUER="$ISSUER" AUTH_AUDIENCE=chance)

say() { echo "[e2e $(date +%H:%M:%S)] $*"; }
note() { mkdir -p "$LOGS"; echo "- $(date +%H:%M:%S) $*" >> "$PROGRESS"; }

# A value of the keycloak or payment-provider block of compose.yaml (local development values only, marked there).
cval() { sed -n "s/^ *$1: *\([^ #]*\).*/\1/p" "$LOCAL_DIR/compose.yaml" | head -n 1; }

# Runs a command under the shared lock when HEAVY_LOCK is set, directly otherwise.
locked() { if [ -n "$HEAVY_LOCK" ]; then flock "$HEAVY_LOCK" "$@"; else "$@"; fi; }

# Thermal events of the last N minutes: asus_wmi key code 0x6d is the embedded controller's thermal notification of one
# ASUS laptop, which powers off under sustained load. Only with THERMAL_WATCH=1; elsewhere the gate always passes.
thermal_count() { journalctl -k --since "-${1:-5}min" --no-pager 2> /dev/null | grep -c 0x6d; }
thermal_gate() {
  [ "$THERMAL_WATCH" = 1 ] || return 0
  local n
  n="$(thermal_count 5)"
  say "thermal gate before $1: $n 0x6d lines in the last 5 min"
  note "thermal gate before $1: $n"
  if [ "$n" != 0 ]; then say "STOP: thermal events present"; return 1; fi
}
