#!/usr/bin/env bash
# Restarts the customer 5136338 server run after a crash or a host reboot, from the saved
# ezloan session only (no credentials are ever held here). Armed by the AUTORESTART marker
# that server_run.py writes once a session is acquired; `server_run.py stop`, a Naver
# verification screen, an egress violation, or a dead session remove it, and then this
# script does nothing. Cron (bfdev@main): */2 * * * * <this script>
set -u
PROJ="$(cd "$(dirname "$0")/.." && pwd)"
RUN_DIR="${EZLOAN_SERVER_DIR:-$HOME/.ezloan-server/5136338}"
LOG="$RUN_DIR/watchdog.log"
[ -f "$RUN_DIR/AUTORESTART" ] || exit 0
[ -f "$RUN_DIR/STOP" ] && exit 0
[ -f "$RUN_DIR/session.json" ] || exit 0
pid="$(cat "$RUN_DIR/run.pid" 2>/dev/null || true)"
if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then exit 0; fi

# Never double-run the single-session account: if the customer's own desktop copy posted in
# the last 10 minutes, stand down and disarm.
recent="$(docker exec neoworks-postgres psql -U neoworks -d neoworks -At -c \
  "select count(*) from \"IngestedLog\" where \"customerKey\"='5136338' and source like 'ezloan-desktop-%' and \"createdAt\" > now() - interval '10 minutes'" 2>/dev/null || echo err)"
if [ "$recent" != "0" ]; then
  echo "$(date -u +%FT%TZ) customer copy active or db unreadable ($recent), disarming" >> "$LOG"
  rm -f "$RUN_DIR/AUTORESTART"
  exit 0
fi

echo "$(date -u +%FT%TZ) run not alive, resuming from the saved session" >> "$LOG"
# An orphaned loop from the dead run would double-register on the same session.
ssh -o BatchMode=yes -o ConnectTimeout=20 unicorn@external-8 \
  "pkill -f 'python3 -u remote_[l]oop.py'; sleep 2; pgrep -fc 'python3 -u remote_[l]oop.py' || echo 0" >> "$LOG" 2>&1
cd "$PROJ" && python3 server_run.py start --resume \
  --ssh-host unicorn@external-8 --expect-ip 49.247.139.101 \
  --loop-host unicorn@external-8 --loop-expect-ip 49.247.139.101 \
  --loop-tick 0.15 >> "$LOG" 2>&1
