#!/usr/bin/env bash
# One-command recovery for DSV4 @ 5700 (2026-08-20, patch 0026 era).
#
# Usage (on 760):  bash /mnt/nvme1/dsv4/deepseek-v4-cmp170hx/launch/restart-dsv4.sh
#
# What it does:
#   1. stop + rm the old dsv4-a100 container (graceful, 120s)
#   2. relaunch via run-pp-dspark.sh with the production config
#      (19-file bind mounts incl. 0020 rejection_sampler + 0026 NaN guard,
#       MODEL default /mnt/data/DeepSeek-V4-Flash-0731, maxlen 524288)
#   3. wait for /health = 200 (up to 60 min; cold boot ~45 min on /mnt/data SSD)
#
# Load-strategy note (2026-08-20, machine RAM upgraded 31G -> 125G):
#   default is --safetensors-load-strategy prefetch (8 threads x 16MB blocks
#   stream the shards into the page cache before the 4 PP workers mmap them,
#   turning random page-fault reads into large sequential reads).
#   Override with DSV4_LOAD_STRATEGY=eager|lazy to A/B at the next restart.
#
# Watch-only policy preserved: this script never runs on a timer. It is a
# manual one-shot. Rollback of 0026: remove rejection_sampler_utils.py from
# the mount list in run-pp-dspark.sh before running this, or DSV4_NO_MOUNT=1.
set -uo pipefail

cd "$(dirname "$0")"

docker stop -t 120 dsv4-a100 2>/dev/null || true
docker rm dsv4-a100 2>/dev/null || true

LOG=/tmp/dsv4-run-$(date +%Y%m%d-%H%M).log
export DSV4_PORT=5700
export DSV4_TYPEB_POLICY="${DSV4_TYPEB_POLICY:-commit}"
LOAD_STRATEGY="${DSV4_LOAD_STRATEGY:-prefetch}"
if [ "${DSV4_LOAD_STRATEGY:-none}" != "off" ]; then
  export DSV4_EXTRA_ARGS="--safetensors-load-strategy ${LOAD_STRATEGY} ${DSV4_EXTRA_ARGS:-}"
fi
echo "[restart-dsv4] launching (load strategy: ${LOAD_STRATEGY}), log: $LOG"
nohup bash run-pp-dspark.sh --maxlen 524288 > "$LOG" 2>&1 &

MISSES=0  # consecutive docker-ps misses (3 = 30s grace for docker-run registration)
for i in $(seq 1 360); do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 \
         http://localhost:5700/health 2>/dev/null || true)
  if [ "$code" = "200" ]; then
    echo "[restart-dsv4] HEALTHY after $((i * 10))s (log: $LOG)"
    exit 0
  fi
  if ! docker ps --format '{{.Names}}' | grep -q '^dsv4-a100$'; then
    MISSES=$((MISSES + 1))
    if [ "$MISSES" -lt 3 ]; then
      sleep 10
      continue
    fi
    echo "[restart-dsv4] container died during startup; tail of $LOG:"
    tail -20 "$LOG"
    exit 1
  fi
  MISSES=0
  sleep 10
done
echo "[restart-dsv4] NOT healthy after 60 min; check $LOG"
exit 1
