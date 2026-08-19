#!/usr/bin/env python3
"""TDD-0019 LIVE — issue-2 announce-loop induction + no-false-kill probes.

    python3 /tmp/tdd_0019_live.py [rep|probe|all]

rep:   poisoned "Lets reload." context continuation at temp 1.0. Pre-0019:
       flows to max_tokens with dozens of repeated lines. Post-0019:
       early FINISHED_STOPPED + "DSV4 0019 rep-tripwire" log lines.
probe: legit repeated-shape outputs (markdown tables with --- rows,
       short list items) must finish normally (green before AND after).
"""
import json
import subprocess
import sys
import time
import urllib.request

BASE, KEY, MODEL = "http://127.0.0.1:5700", "wzg123$%^", "dsv4s"


def post(body, timeout=300):
    req = urllib.request.Request(
        BASE + "/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + KEY,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def rep_count():
    out = subprocess.run(["docker", "logs", "dsv4-a100", "--since", "60m"],
                         capture_output=True, text=True)
    blob = (out.stdout or "") + (out.stderr or "")
    return sum(1 for l in blob.splitlines() if "rep-tripwire" in l)


MODE = sys.argv[1] if len(sys.argv) > 1 else "all"

if MODE in ("rep", "all"):
    poison = "Lets reload.\n" * 6
    t0 = rep_count()
    full_flow, early_stop = 0, 0
    for i in range(20):
        try:
            r = post({"model": MODEL, "max_tokens": 400, "temperature": 1.0,
                      "messages": [
                          {"role": "system", "content": "你是浏览器测试助手，按上文节奏继续"},
                          {"role": "user", "content": "继续按上面格式输出"},
                          {"role": "assistant", "content": poison},
                          {"role": "user", "content": "继续"},
                      ]})
            c = r["choices"][0]
            txt = c["message"]["content"] or ""
            lines = [l.strip() for l in txt.splitlines() if l.strip()]
            dup = max((lines.count(l) for l in set(lines)), default=0)
            early_stop += (dup < 5)
            full_flow += (dup >= 5)
            print(f"rep{i}: max_dup_line={dup} fin={c['finish_reason']} len={len(txt)}")
        except Exception as e:
            print(f"rep{i}: EXC {e}")
        time.sleep(1)
    t1 = rep_count()
    print(f"REP: dup-heavy={full_flow}/20 clean={early_stop}/20 "
          f"rep-tripwire delta={t1 - t0}")
    print("RED  (pre-0019):  delta=0, dup-heavy high")
    print("GREEN(post-0019): delta>0, tails cut (dup-heavy low)")

if MODE in ("probe", "all"):
    probes = [
        "生成一个 markdown 表格：三列五行，内容随意，表头分隔行用 ---",
        "输出一到十的中文数字列表，每行一个词",
        "写一段 Python 代码，用 for 循环打印 hello 三次，注意要正常完整",
    ]
    ok = 0
    for i, p in enumerate(probes):
        try:
            r = post({"model": MODEL, "max_tokens": 400, "temperature": 0.3,
                      "messages": [{"role": "user", "content": p}]})
            c = r["choices"][0]
            txt = c["message"]["content"] or ""
            # shape-based (length probes misfire on legitimately short answers)
            shape = [("|" in txt and "---" in txt),
                     ("一" in txt and "十" in txt
                      and len([l for l in txt.splitlines() if l.strip()]) >= 8),
                     ("for" in txt and "hello" in txt and "print" in txt)][i]
            good = c["finish_reason"] in ("stop", "length") and shape
            ok += good
            print(f"probe{i}: {'OK ' if good else 'BAD'} fin={c['finish_reason']} len={len(txt)}")
        except Exception as e:
            print(f"probe{i}: EXC {e}")
    print(f"PROBE: {ok}/{len(probes)} (must be 3/3 before AND after)")
