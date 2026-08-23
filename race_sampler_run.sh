#!/bin/bash
# ezloan 실시간 배너 competitor-timing sampler, kmong customer 5136338 (더원대부 / advertiser 585).
#
# Runs on unicorn@external-2 (KR). READ-ONLY, anonymous GETs, never logs in, never writes.
# Idempotent: flock -n means the cron watchdog is a no-op while a sampler is already up.
#
#   install   scp race_sampler.py race_sampler_run.sh unicorn@external-2:~/ezloan-sampler/
#   start     ~/ezloan-sampler/race_sampler_run.sh          (or just wait for the watchdog)
#   stop      crontab -r  (or drop the two sampler lines) && pkill -f race_sampler.py
#   report    python3 ~/ezloan-sampler/race_sampler.py --report-only
set -u
DIR="$HOME/ezloan-sampler"
LOG="$DIR/sampler.log"
mkdir -p "$DIR"

# keep the log bounded; external-2 only has ~1.4G free
if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG")" -gt 33554432 ]; then
  mv -f "$LOG" "$LOG.1"
fi

exec /usr/bin/flock -n "$DIR/sampler.lock" \
  /usr/bin/python3 -u "$DIR/race_sampler.py" "$@" >>"$LOG" 2>&1
