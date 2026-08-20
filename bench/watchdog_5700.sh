#!/usr/bin/env bash
# DSV4 canary watchdog (0017 deployable asset).
# Every CHECK_INTERVAL seconds: probe :5700/v1/models, count new server
# log signature lines since the previous check, append one CSV line.
# Watch-only by policy: no restarts, no container control. Kill with
#   pkill -f watchdog_5700.sh
set -u
INTERVAL="${WATCHDOG_INTERVAL:-300}"
LOG="${WATCHDOG_LOG:-/tmp/dsv4-watchdog.log}"
LAST=0  # docker logs --since anchor, seconds

echo "ts,http,reqs,612,typeA,typeB,salvage,tripwire,dead,tb,env0022,tbfin" >> "$LOG"

while true; do
    ts=$(date +%m-%d_%H:%M)
    code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5700/v1/models --max-time 10)
    tmp=$(mktemp)
    docker logs dsv4-a100 --since "${LAST}s" > "$tmp" 2>&1
    LAST=$((INTERVAL + 5))
    c() { grep -c "$1" "$tmp" 2>/dev/null || true; }
    reqs=$(grep -c '"POST /v1/' "$tmp" 2>/dev/null || true)
    sig612=$(grep -c 'grammar_matcher.cc:612' "$tmp" || true)
    typeA=$(grep -c 'Grammar completed mid-block' "$tmp" || true)
    typeB=$(grep -c 'TYPE-B' "$tmp" || true)
    salv=$(grep -c 'salvage-guard armed' "$tmp" || true)
    trip=$(grep -c 'soup-tripwire' "$tmp" || true)
    dead=$(grep -c 'EngineDeadError' "$tmp" || true)
    tb=$(grep -c 'Traceback' "$tmp" || true)
    env222=$(grep -c '0022 envelope-missing' "$tmp" || true)
    tbfin=$(grep -c 'typeb-finish' "$tmp" || true)
    echo "$ts,$code,$reqs,$sig612,$typeA,$typeB,$salv,$trip,$dead,$tb,$env222,$tbfin" >> "$LOG"
    # alert line when something nonzero-scary appears (log only)
    if [ "${dead:-0}" != "0" ] || [ "${tb:-0}" != "0" ] || [ "$code" != "200" ]; then
        echo "$ts ALERT http=$code dead=$dead tb=$tb" >> "$LOG"
    fi
    rm -f "$tmp"
    sleep "$INTERVAL"
done
