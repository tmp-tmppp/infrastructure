#!/usr/bin/env bash
# The local end-to-end run of Chance with a real Keycloak (re-runnable; README.md).
#
#   bash run.sh all           watch + up + services + upstream + flows + down (the whole run, from zero)
#   bash run.sh watch         start the thermal watcher (only with THERMAL_WATCH=1; transient user unit rso-e2e-thermal)
#   bash run.sh up            the dependencies (../compose.yaml + compose.override.yaml)
#   bash run.sh services      the seven services from their dist/, each natively on its own ports
#   bash run.sh upstream      step k's stand-in for Google: realm e2e-upstream and provider e2e-upstream in chance (kcadm)
#   bash run.sh flows [a..k]  the flows (flows.py), all steps or the ones named
#   bash run.sh status        what of this run is up
#   bash run.sh down          stop every recorded pid, compose down -v, stop the watcher, check that nothing is left
set -u
# shellcheck source=env.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/env.sh"
mkdir -p "$LOGS"

SERVICES=(tenants trading payments audit markets market-data market-maker)

alive() { local s; s="$(awk '{print $3}' "/proc/$1/stat" 2> /dev/null)"; [ -n "$s" ] && [ "$s" != Z ]; }

cmd_watch() {
  if [ "$THERMAL_WATCH" != 1 ]; then say "thermal watcher off (THERMAL_WATCH is not 1)"; return 0; fi
  if systemctl --user is-active -q rso-e2e-thermal.service; then say "thermal watcher already active"; return 0; fi
  systemctl --user reset-failed rso-e2e-thermal.service 2> /dev/null
  # A transient unit does not inherit this shell's environment: hand over Docker's settings (its compose down needs them).
  local name pass=()
  for name in DOCKER_CONFIG DOCKER_HOST PAYMENT_PROVIDER_IMAGE; do [ -n "${!name:-}" ] && pass+=("--setenv=$name=${!name}"); done
  systemd-run --user --unit=rso-e2e-thermal -q "${pass[@]}" bash "$E2E_DIR/thermal-watch.sh" || return 1
  sleep 1
  systemctl --user is-active rso-e2e-thermal.service
  note "thermal watcher rso-e2e-thermal started"
}

cmd_up() {
  thermal_gate "compose up" || return 3
  say "compose up${HEAVY_LOCK:+ (waiting for the lock $HEAVY_LOCK)}"
  note "compose up started"
  local t0 rc; t0=$(date +%s)
  locked timeout 900 "${COMPOSE[@]}" up -d --wait --wait-timeout 840 > "$LOGS/compose-up.log" 2>&1
  rc=$?
  say "compose up exit $rc after $(( $(date +%s) - t0 )) s"
  note "compose up exit $rc after $(( $(date +%s) - t0 )) s"
  "${COMPOSE[@]}" ps --format '{{.Service}} {{.Status}}' | tee -a "$LOGS/compose-up.log"
  return $rc
}

start_service() { # name VAR=value...
  local name="$1"; shift
  local wrap=()
  if [ -n "$E2E_CPU_QUOTA" ] && command -v systemd-run > /dev/null; then
    systemctl --user reset-failed "rso-e2e-$name.scope" 2> /dev/null
    wrap=(systemd-run --user --scope -q -p "CPUQuota=$E2E_CPU_QUOTA" --unit="rso-e2e-$name")
  fi
  # setsid and exec keep the recorded pid the pid of node itself; fd 9 (the lock) is not inherited.
  ( cd "$RSO_ROOT/$name" && exec setsid "${wrap[@]}" env APP_ENV=local LOG_LEVEL=info "$@" node dist/main.js ) \
    > "$LOGS/$name.log" 2>&1 < /dev/null 9>&- &
  local pid=$!
  echo "$name $pid" >> "$PIDS_FILE"
  sleep 1
  say "$name started, pid $pid ($(cat "/proc/$pid/comm" 2> /dev/null || echo gone))"
}

wait_ready() { # name port
  local i
  for i in $(seq 1 120); do
    if curl -fsS -m 2 "http://localhost:$2/health/ready" > /dev/null 2>&1; then
      say "$1 ready on $2 after ${i} s"
      note "$1 ready on $2 (pid $(awk -v n="$1" '$1==n{print $2}' "$PIDS_FILE" | tail -n 1))"
      return 0
    fi
    sleep 1
  done
  say "$1 did not become ready; last log lines:"; tail -n 20 "$LOGS/$1.log"
  note "$1 NOT ready on $2"
  return 1
}

services_locked() {
  : > "$PIDS_FILE"
  local tsec ppkey whsec psec msec mmsec tmsec
  tsec="$(cval TENANTS_CLIENT_SECRET)"; tmsec="$(cval TENANTS_MARKETS_CLIENT_SECRET)"; psec="$(cval PAYMENTS_CLIENT_SECRET)"
  msec="$(cval MARKETS_CLIENT_SECRET)"; mmsec="$(cval MARKET_MAKER_CLIENT_SECRET)"
  ppkey="$(cval PROVIDER_API_KEY)"; whsec="$(cval WEBHOOK_SECRET)"
  db() { echo "postgres://$1:$1@localhost:5432/$1"; }
  start_service tenants PORT=8101 METRICS_PORT=9101 DATABASE_URL="$(db tenants)" NATS_URL="$NATS" "${AUTH_ENV[@]}" \
    KEYCLOAK_URL="$KC" KEYCLOAK_REALM=chance KEYCLOAK_CLIENT_ID=tenants KEYCLOAK_CLIENT_SECRET="$tsec" \
    MARKETS_BASE_URL=http://localhost:8104 MARKETS_ADMISSION_CLIENT_ID=tenants-markets MARKETS_ADMISSION_CLIENT_SECRET="$tmsec"
  wait_ready tenants 8101 || return 1
  start_service trading PORT=8105 METRICS_PORT=9105 INTERNAL_PORT=8115 GRPC_PORT=8125 DATABASE_URL="$(db trading)" NATS_URL="$NATS" "${AUTH_ENV[@]}"
  wait_ready trading 8105 || return 1
  start_service payments PORT=8102 METRICS_PORT=9102 DATABASE_URL="$(db payments)" NATS_URL="$NATS" "${AUTH_ENV[@]}" \
    PROVIDER_BASE_URL=http://localhost:8082 PROVIDER_API_KEY="$ppkey" WEBHOOK_SECRET="$whsec" \
    DEPOSIT_SUCCESS_URL=http://localhost:5173/deposits/success DEPOSIT_CANCEL_URL=http://localhost:5173/deposits/cancelled \
    TRADING_DEBIT_TRANSPORT=grpc TRADING_GRPC_URL=localhost:8125 \
    KEYCLOAK_TOKEN_URL="$ISSUER/protocol/openid-connect/token" KEYCLOAK_CLIENT_ID=payments KEYCLOAK_CLIENT_SECRET="$psec"
  wait_ready payments 8102 || return 1
  start_service audit PORT=8103 METRICS_PORT=9103 DATABASE_URL="$(db audit)" NATS_URL="$NATS" "${AUTH_ENV[@]}"
  wait_ready audit 8103 || return 1
  start_service markets PORT=8104 METRICS_PORT=9104 DATABASE_URL="$(db markets)" NATS_URL="$NATS" "${AUTH_ENV[@]}" \
    TRADING_URL=http://localhost:8115 KEYCLOAK_URL="$KC" KEYCLOAK_REALM=chance MARKETS_CLIENT_SECRET="$msec"
  wait_ready markets 8104 || return 1
  start_service market-data PORT=8106 METRICS_PORT=9106 DATABASE_URL="$(db market_data)" NATS_URL="$NATS" "${AUTH_ENV[@]}"
  wait_ready market-data 8106 || return 1
  start_service market-maker PORT=8107 METRICS_PORT=9107 NATS_URL="$NATS" TRADING_URL=http://localhost:8115 \
    KEYCLOAK_URL="$KC" KEYCLOAK_REALM=chance MARKET_MAKER_CLIENT_SECRET="$mmsec" RECHECK_INTERVAL_MS=5000
  wait_ready market-maker 8107 || return 1
}

cmd_services() {
  local name missing=""
  for name in "${SERVICES[@]}"; do [ -f "$RSO_ROOT/$name/dist/main.js" ] || missing="$missing $name"; done
  if [ -n "$missing" ]; then
    say "no dist/main.js in:$missing (run npm ci and npm run build in each)"
    return 2
  fi
  thermal_gate "service starts" || return 3
  say "starting the services${HEAVY_LOCK:+ (waiting for the lock $HEAVY_LOCK)}"
  note "service starts"
  if [ -n "$HEAVY_LOCK" ]; then exec 9> "$HEAVY_LOCK"; flock 9; fi
  services_locked
  local rc=$?
  if [ -n "$HEAVY_LOCK" ]; then flock -u 9; exec 9>&-; fi
  note "service starts finished, exit $rc; pids: $(tr '\n' ' ' < "$PIDS_FILE")"
  return $rc
}

# Step k's stand-in for Google, only in this local Keycloak and only through its own kcadm (never the realm file): a realm
# e2e-upstream with a confidential client, and in chance a provider e2e-upstream set up like google (trustEmail, syncMode
# IMPORT, the first-login flow "chance first broker login"). The browser reaches the provider's login page on
# localhost:8081; Keycloak calls its token, userinfo and keys endpoints on its own port inside the container.
cmd_upstream() {
  local kc=("${COMPOSE[@]}" exec -T keycloak /opt/keycloak/bin/kcadm.sh) secret back=http://localhost:8080/realms/e2e-upstream log="$LOGS/upstream.log"
  secret="$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  : > "$log"
  if ! "${kc[@]}" config credentials --server http://localhost:8080 --realm master --user "$(cval KC_BOOTSTRAP_ADMIN_USERNAME)" --password "$(cval KC_BOOTSTRAP_ADMIN_PASSWORD)" >> "$log" 2>&1; then
    say "kcadm login failed (see $log)"; return 1
  fi
  # A repeated run starts from a fresh provider realm.
  "${kc[@]}" delete identity-provider/instances/e2e-upstream -r chance >> "$log" 2>&1
  "${kc[@]}" delete realms/e2e-upstream >> "$log" 2>&1
  "${kc[@]}" create realms -s realm=e2e-upstream -s enabled=true -s verifyEmail=false -s registrationAllowed=false >> "$log" 2>&1 \
    && "${kc[@]}" update authentication/required-actions/VERIFY_PROFILE -r e2e-upstream -s enabled=false >> "$log" 2>&1 \
    && "${kc[@]}" create clients -r e2e-upstream -s clientId=e2e-broker -s publicClient=false -s "secret=$secret" -s standardFlowEnabled=true \
      -s directAccessGrantsEnabled=false -s 'redirectUris=["http://localhost:8081/realms/chance/broker/e2e-upstream/endpoint"]' >> "$log" 2>&1 \
    && "${kc[@]}" create identity-provider/instances -r chance -s alias=e2e-upstream -s providerId=oidc -s enabled=true -s trustEmail=true \
      -s 'firstBrokerLoginFlowAlias=chance first broker login' -s config.syncMode=IMPORT -s config.clientId=e2e-broker -s "config.clientSecret=$secret" \
      -s config.clientAuthMethod=client_secret_post -s 'config.defaultScope=openid email profile' \
      -s "config.authorizationUrl=$KC/realms/e2e-upstream/protocol/openid-connect/auth" -s "config.issuer=$KC/realms/e2e-upstream" \
      -s "config.tokenUrl=$back/protocol/openid-connect/token" -s "config.userInfoUrl=$back/protocol/openid-connect/userinfo" \
      -s "config.jwksUrl=$back/protocol/openid-connect/certs" -s config.useJwksUrl=true -s config.validateSignature=true >> "$log" 2>&1 \
    && "${kc[@]}" create identity-provider/instances/e2e-upstream/mappers -r chance -s name=e2e-upstream-username -s identityProviderAlias=e2e-upstream \
      -s identityProviderMapper=oidc-username-idp-mapper -s "config.template=e2e-upstream.\${CLAIM.sub}" -s config.target=LOCAL -s config.syncMode=IMPORT >> "$log" 2>&1
  # The last step gives the stand-in the user name mapper of google (google.<sub> there): a provider-created user is
  # named after the provider's subject, never after a name a stranger could take first.
  local rc=$?
  say "stand-in provider e2e-upstream: exit $rc"
  note "upstream (realm e2e-upstream, provider e2e-upstream in chance) exit $rc"
  return $rc
}

cmd_flows() {
  thermal_gate "flows" || return 3
  python3 "$E2E_DIR/flows.py" "$@" 2>&1 | tee "$LOGS/flows.out"
  return "${PIPESTATUS[0]}"
}

cmd_status() {
  if [ -f "$PIDS_FILE" ]; then
    while read -r name pid; do echo "$name $pid $(alive "$pid" && echo alive || echo gone)"; done < "$PIDS_FILE"
  fi
  "${COMPOSE[@]}" ps --format '{{.Service}} {{.Status}}'
  if [ "$THERMAL_WATCH" = 1 ]; then
    echo "thermal watcher: $(systemctl --user is-active rso-e2e-thermal.service)"
    echo "0x6d in the last 5 min: $(thermal_count 5)"
  fi
}

cmd_down() {
  if [ -f "$PIDS_FILE" ]; then
    while read -r name pid; do
      [ -n "${pid:-}" ] || continue
      if alive "$pid"; then
        kill -TERM "$pid" 2> /dev/null
        timeout 20 tail --pid="$pid" -f /dev/null
        if alive "$pid"; then kill -KILL "$pid" 2> /dev/null; echo "$name $pid KILLED after 20 s"; else echo "$name $pid stopped"; fi
      else
        echo "$name $pid was not running"
      fi
    done < "$PIDS_FILE" | tee "$LOGS/shutdown.txt"
  fi
  # Keycloak's own log (a failed realm import, the errors behind a refused page) is gone once the stack is down.
  "${COMPOSE[@]}" logs --no-color --tail 2000 keycloak > "$LOGS/keycloak.log" 2>&1
  "${COMPOSE[@]}" down -v > "$LOGS/compose-down.log" 2>&1
  echo "compose down -v exit $?"
  systemctl --user stop rso-e2e-thermal.service 2> /dev/null
  echo "left over: containers of chance-local: [$(docker ps -aq --filter label=com.docker.compose.project=chance-local | tr '\n' ' ')]"
  echo "left over: volumes of chance-local: [$(docker volume ls -q --filter label=com.docker.compose.project=chance-local | tr '\n' ' ')]"
  if [ -f "$PIDS_FILE" ]; then
    local left=""
    while read -r name pid; do alive "$pid" && left="$left $name:$pid"; done < "$PIDS_FILE"
    echo "left over: service pids alive: [${left}]"
  fi
  [ "$THERMAL_WATCH" = 1 ] && echo "thermal watcher: $(systemctl --user is-active rso-e2e-thermal.service)"
  note "down finished"
}

case "${1:-}" in
  watch) cmd_watch ;;
  up) cmd_up ;;
  services) cmd_services ;;
  upstream) cmd_upstream ;;
  flows) shift; cmd_flows "$@" ;;
  status) cmd_status ;;
  down) cmd_down ;;
  all)
    rc=1
    cleaned=0
    # The services run under setsid and the compose stack under Docker, so an interrupt of this script would leave both
    # running: cleanup runs once, on the normal end and on INT, TERM and HUP (exit 130). Bash runs a trap only after the
    # step in progress has ended (Ctrl-C reaches that step too; a signal sent to this script alone waits for it).
    cleanup_once() {
      [ "$cleaned" = 1 ] && return 0
      cleaned=1
      [ "${E2E_KEEP_UP:-0}" = 1 ] || cmd_down
    }
    # shellcheck disable=SC2329 # runs through the trap below
    on_interrupt() {
      trap '' INT TERM HUP # a second signal must not cut the cleanup short
      say "interrupted: cleaning up (bash run.sh down repeats it)"
      cleanup_once
      exit 130
    }
    trap 'on_interrupt' INT TERM HUP
    if cmd_watch && cmd_up && cmd_services && cmd_upstream; then cmd_flows; rc=$?; echo "flows exit $rc"; fi
    cleanup_once
    exit "$rc"
    ;;
  *) sed -n '2,11p' "$0"; exit 2 ;;
esac
