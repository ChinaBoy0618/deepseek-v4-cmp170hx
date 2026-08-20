#!/usr/bin/env python3
"""One-off c=32 decode point (same recipe as bench_concurrency.py DECODE arm)."""
import json
import random
import sys
import threading
import time
import urllib.request

URL = "http://127.0.0.1:5700/v1/completions"
WORDS = (
    "system kernel memory buffer thread process socket packet register cache "
    "pointer allocate schedule interrupt virtual physical address translate "
    "compile execute branch predict pipeline vector matrix tensor gradient "
    "cluster network storage device driver module segment offset boundary"
).split()


def main():
    c = int(sys.argv[1]) if len(sys.argv) > 1 else 32
    res = [None] * c

    def work(i):
        rng = random.Random(90000 + i)
        body = " ".join(rng.choice(WORDS) for _ in range(int(60 / 1.3)))
        prompt = f"[run {90000 + i}] Notes: {body}\nSummarize."
        payload = {"model": "dsv4s", "prompt": prompt,
                   "max_tokens": 300, "temperature": 0}
        req = urllib.request.Request(
            URL, json.dumps(payload).encode(), {"Content-Type": "application/json"})
        try:
            res[i] = json.load(urllib.request.urlopen(req, timeout=3600))
        except Exception as e:  # noqa: BLE001
            res[i] = {"error": str(e)}

    ts = [threading.Thread(target=work, args=(i,)) for i in range(c)]
    t0 = time.perf_counter()
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    wall = time.perf_counter() - t0
    ok = [r for r in res if r and "usage" in r]
    ptok = sum(r["usage"]["prompt_tokens"] for r in ok)
    gtok = sum(r["usage"]["completion_tokens"] for r in ok)
    print(f"c={c} wall={wall:.2f}s ok={len(ok)}/{c} prompt={ptok} gen={gtok} "
          f"decode={gtok / wall:.1f} t/s")


if __name__ == "__main__":
    main()
