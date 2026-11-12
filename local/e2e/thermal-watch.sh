#!/usr/bin/env bash
# Thermal watcher of the run, only with THERMAL_WATCH=1 (README.md: it fits one ASUS laptop that powers off under
# sustained load). Every 20 s it reads the kernel log since its own start; on any asus_wmi 0x6d line it stops every
# service process and container of this run and writes "STOPPED: thermal" to logs/progress.log.
# Started by run.sh as the transient user unit rso-e2e-thermal (systemd-run --user), stopped by run.sh down.
set -u
# shellcheck source=env.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/env.sh"
mkdir -p "$LOGS"
START="$(date +%s)"
echo "thermal watcher started at $(date -d "@$START" +%H:%M:%S)" >> "$LOGS/thermal-watch.log"
while true; do
  n="$(journalctl -k --since "@$START" --no-pager 2> /dev/null | grep -c 0x6d)"
  echo "$(date +%H:%M:%S) 0x6d since start: $n" >> "$LOGS/thermal-watch.log"
  if [ "$n" != 0 ]; then
    echo "STOPPED: thermal ($n asus_wmi 0x6d lines since $(date -d "@$START" +%H:%M:%S), detected $(date +%H:%M:%S))" >> "$PROGRESS"
    if [ -f "$PIDS_FILE" ]; then
      while read -r name pid; do
        [ -n "${pid:-}" ] && kill -TERM "$pid" 2> /dev/null && echo "TERM $name $pid" >> "$LOGS/thermal-watch.log"
      done < "$PIDS_FILE"
    fi
    "${COMPOSE[@]}" down -v >> "$LOGS/thermal-watch.log" 2>&1
    echo "$(date +%H:%M:%S) stopped everything" >> "$LOGS/thermal-watch.log"
    exit 0
  fi
  sleep 20
done
