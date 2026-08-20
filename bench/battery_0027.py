#!/usr/bin/env python3
"""battery_0027: fixed concurrent json_schema battery for 0027 A/B.

3 waves x 8 concurrent response_format requests; reports per-wave valid
rate and aggregate. Deterministic prompts (no randomness dependence).
Run on the server against localhost:5700:
    python3 battery_0027.py <tag>
"""
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
import urllib.request

BASE = "http://127.0.0.1:5700"
TAG = sys.argv[1] if len(sys.argv) > 1 else "run"

SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "age": {"type": "integer"},
        "city": {"type": "string"},
    },
    "required": ["name", "age", "city"],
    "additionalProperties": False,
}


def req(i):
    body = json.dumps({
        "model": "dsv4s",
        "messages": [{"role": "user",
                      "content": f"Make up person #{i} and return strict JSON "
                                 f"with name, age, city."}],
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "profile", "schema": SCHEMA}},
        "max_tokens": 300,
    }).encode()
    r = urllib.request.Request(BASE + "/v1/chat/completions", body,
                               {"Content-Type": "application/json"})
    t0 = time.time()
    resp = json.load(urllib.request.urlopen(r, timeout=300))
    dt = time.time() - t0
    ch = resp["choices"][0]
    c = ch["message"].get("content") or ""
    try:
        d = json.loads(c)
        ok = set(d.keys()) <= {"name", "age", "city"} and all(
            k in d for k in ("name", "age", "city"))
    except Exception:
        ok = False
    return ok, ch["finish_reason"], resp.get("usage", {}).get("completion_tokens"), c[:90], dt


total_ok = total = 0
for wave in range(3):
    with ThreadPoolExecutor(max_workers=8) as ex:
        rs = list(ex.map(req, range(wave * 8, wave * 8 + 8)))
    ok = sum(r[0] for r in rs)
    total_ok += ok
    total += len(rs)
    print(f"[{TAG}] wave{wave}: {ok}/8 valid", flush=True)
    for r in rs:
        if not r[0]:
            print(f"    BAD fin={r[1]} ct={r[2]} content={r[3]!r}", flush=True)
print(f"[{TAG}] TOTAL {total_ok}/{total} valid")
