#!/usr/bin/env bash
# DSV4 canary watchdog v2 (S3 upgrade 2026-08-20, per docs/dsv4-para-optimization-2026-08-20.md).
# Changes vs v1:
#   - column '612' dropped (dead column: container never logs grammar_matcher.cc:612
#     post-0016) -> replaced by 'oov' = scheduler "Out-of-vocab" real signature
#   - new 'available_mb' column = host free available (memory-tier tracking)
#   - alert semantics split: dead/tb nonzero -> ALERT DEAD (immediate, log only);
#     else K consecutive degraded cycles (http!=200 OR available<MIN_MB OR oov>0)
#     -> ALERT HOLD <stats>. K = WATCHDOG_HOLD_K (default 6 = 30min at 300s interval).
#   - env222/tbfin columns kept (parallel-session 0022/0025 tripwires, 2026-08-20).
# Watch-only by policy: never restarts, never kills containers. Kill with
#   pkill -f watchdog_5700.sh
set -u
INTERVAL="${WATCHDOG_INTERVAL:-300}"
LOG="${WATCHDOG_LOG:-/tmp/dsv4-watchdog.log}"
URL="${WATCHDOG_URL:-http://127.0.0.1:5700/v1/models}"
HOLD_K="${WATCHDOG_HOLD_K:-6}"
MIN_MB="${WATCHDOG_MIN_AVAIL_MB:-8192}"
LAST=0  # docker logs --since anchor, seconds
hold=0  # consecutive degraded cycles

echo "ts,http,available_mb,reqs,oov,typeA,typeB,salvage,tripwire,dead,tb,env222,tbfin" >> "$LOG"

while true; do
    ts=$(date +%m-%d_%H:%M)
    code=$(curl -s -o /dev/null -w '%{http_code}' "$URL" --max-time 10)
    avail=$(LC_ALL=C free -m | awk '/^Mem:/{print $7}')
    tmp=$(mktemp)
    docker logs dsv4-a100 --since "${LAST}s" > "$tmp" 2>&1
    LAST=$((INTERVAL + 5))
    reqs=$(grep -c '"POST /v1/' "$tmp" 2>/dev/null || true)
    oov=$(grep -c 'Out-of-vocab' "$tmp" || true)
    typeA=$(grep -c 'Grammar completed mid-block' "$tmp" || true)
    typeB=$(grep -c 'TYPE-B' "$tmp" || true)
    salv=$(grep -c 'salvage-guard armed' "$tmp" || true)
    trip=$(grep -c 'soup-tripwire' "$tmp" || true)
    dead=$(grep -c 'EngineDeadError' "$tmp" || true)
    tb=$(grep -c 'Traceback' "$tmp" || true)
    env222=$(grep -c '0022 envelope-missing' "$tmp" || true)
    tbfin=$(grep -c 'typeb-finish' "$tmp" || true)
    echo "$ts,$code,$avail,$reqs,$oov,$typeA,$typeB,$salv,$trip,$dead,$tb,$env222,$tbfin" >> "$LOG"
    if [ "${dead:-0}" != "0" ] || [ "${tb:-0}" != "0" ]; then
        echo "$ts ALERT DEAD http=$code dead=$dead tb=$tb oov=$oov" >> "$LOG"
        hold=0
    elif [ "$code" != "200" ] || [ "${avail:-0}" -lt "$MIN_MB" ] || [ "${oov:-0}" != "0" ]; then
        hold=$((hold + 1))
        if [ "$hold" -ge "$HOLD_K" ]; then
            echo "$ts ALERT HOLD x${hold} http=$code avail=${avail}MB oov=$oov typeA=$typeA typeB=$typeB env222=$env222" >> "$LOG"
        fi
    else
        hold=0
    fi
    rm -f "$tmp"
    sleep "$INTERVAL"
done
