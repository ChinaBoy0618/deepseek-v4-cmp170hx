#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DSV4 depth-300k extension: single-turn matrix at 230k/300k + an accumulated
multi-turn auto loop pushed past 300k (the incident shape).

Arms:
  A  single-turn : {230k, 300k} x {auto, required} x N samples, small args
  E  accumulated : JSON-native, tool_choice=auto (unconstrained, incident path),
                   temp 1.0, thinking on; ~9.5k tok/turn growth, run to >300k.

Calibration (measured 08-20): ~911 prompt tokens per 30-line notes block.

Usage: DSV4_API_KEY=... python3 bench/depth300k.py [--n 5] [--max-turns 40]
"""
import argparse, json, os, random, sys, time
import urllib.request, urllib.error

AP = argparse.ArgumentParser()
AP.add_argument("--base-url", default="http://47.99.74.105:5700")
AP.add_argument("--model", default="dsv4s")
AP.add_argument("--api-key", default=os.environ.get("DSV4_API_KEY", ""))
AP.add_argument("-n", type=int, default=5)
AP.add_argument("--max-turns", type=int, default=40)
AP.add_argument("--skip-a", action="store_true",
                help="skip arm A (re-run the E accumulated arm only)")
AP.add_argument("--jsonl", default="tmp/reports/depth300k.jsonl")
A = AP.parse_args()
BASE = A.base_url.rstrip("/")

TOOLS = [
    {"type": "function", "function": {"name": "Bash",
        "description": "Execute a shell command and return its output",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "Read",
        "description": "Read a file", "parameters": {"type": "object",
        "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "Write",
        "description": "Write a file", "parameters": {"type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "Grep",
        "description": "Search file contents", "parameters": {"type": "object",
        "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}}},
]
PSEUDO = ["<bash>", "<bash ", "<Bash ", "<bash_command>", "<｜DSML｜_tool_calls>",
          "<thinking>", "<call "]
TASK = "Run the command 'uptime' now using the Bash tool."

def blocks(seed, n):
    rnd = random.Random(seed)
    out = []
    for d in range(n):
        lines = []
        for i in range(30):
            lines.append("Note %d.%d: the %s/%s window kept invariants; acceptance "
                         "%.2f, drafted %d, rejected %d." % (
                             d, i, rnd.choice(["scheduler", "grammar", "drafter"]),
                             rnd.choice(["bitmask", "allocator", "queue"]),
                             rnd.uniform(0.6, 0.95), rnd.randint(3, 6), rnd.randint(0, 2)))
        out.append("Investigation notes block %d:\n%s" % (d, "\n".join(lines)))
    return "\n\n".join(out)

def post(body, timeout=900):
    h = {"Content-Type": "application/json"}
    if A.api_key:
        h["Authorization"] = "Bearer " + A.api_key
    req = urllib.request.Request(BASE + "/v1/chat/completions",
                                 data=json.dumps(body).encode(), headers=h)
    t0 = time.time()
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(r.read().decode()), None, time.time() - t0
    except urllib.error.HTTPError as e:
        return None, "http%d: %s" % (e.code, e.read().decode(errors="replace")[:150]), time.time() - t0
    except Exception as e:
        return None, "conn:" + repr(e)[:100], time.time() - t0

def classify(d):
    ch = d["choices"][0]
    m = ch["message"]
    txt = m.get("content") or ""
    rsn = m.get("reasoning_content") or ""
    tcs = m.get("tool_calls") or []
    finish = ch.get("finish_reason")
    pseudo = [p for p in PSEUDO if p in txt]
    meta = dict(finish=finish, ctok=d.get("usage", {}).get("completion_tokens", 0),
                ptok=d.get("usage", {}).get("prompt_tokens", 0),
                reason_len=len(rsn), content_len=len(txt),
                head=repr((txt or rsn)[:150]))
    if tcs:
        okargs = True
        for tc in tcs:
            try:
                if not json.loads(tc["function"]["arguments"] or "{}"):
                    okargs = False
            except Exception:
                okargs = False
        names = [tc["function"]["name"] for tc in tcs]
        if not okargs:
            return "ARGS_BAD", "n=%d names=%s" % (len(tcs), names), meta
        cls = "TOOL_OK" if "Bash" in names else "TOOL_WRONGNAME"
        if len(tcs) > 1 and len(set(names)) < len(names):
            cls += "+DUP"
        return cls, "n=%d names=%s" % (len(tcs), names), meta
    if finish == "length":
        if pseudo:
            return "PSEUDO+LEN", "pseudo=%s" % pseudo, meta
        return "NO_TOOL_LENGTH", "", meta
    if pseudo:
        return "PSEUDO_TAG", "pseudo=%s" % pseudo, meta
    return "NO_TOOL_STOP", "", meta

def emit(row):
    print("  %-8s %-18s ptok=%6d ctok=%4d rlen=%4d clen=%4d %5.1fs %s %s"
          % (row.get("arm", ""), row["cls"], row["ptok"], row["ctok"],
             row["reason_len"], row["content_len"], row["lat"], row["detail"],
             row["head"][:60]), flush=True)
    with open(A.jsonl, "a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")

def arm_single():
    from collections import Counter
    for target in (230000, 300000):
        docs = max(1, int(target / 911))
        for choice in ("auto", "required"):
            c = Counter()
            for i in range(A.n):
                msgs = [{"role": "user", "content":
                         "You are a site-reliability agent. Context notes from the archive:\n"
                         + blocks(1000 + i, docs) + "\n\nFinal instruction: " + TASK}]
                body = {"model": A.model, "messages": msgs, "temperature": 1.0,
                        "top_p": 0.95, "max_tokens": 4096,
                        "chat_template_kwargs": {"thinking": True},
                        "tools": TOOLS, "tool_choice": choice}
                d, err, lat = post(body)
                if err:
                    row = dict(arm="A-%dk-%s" % (target // 1000, choice), i=i,
                               cls="NET_ERR", detail=err[:120], lat=round(lat, 1),
                               ptok=0, ctok=0, reason_len=0, content_len=0, finish="-",
                               head="")
                    c["NET_ERR"] += 1
                else:
                    cls, detail, meta = classify(d)
                    row = dict(arm="A-%dk-%s" % (target // 1000, choice), i=i, cls=cls,
                               detail=detail, lat=round(lat, 1), **meta)
                    c[cls.split("+")[0]] += 1
                emit(row)
            print("A %dk %s -> %s" % (target // 1000, choice, dict(c)), flush=True)

def exec_fake(name, args):
    return json.dumps({"stdout": "OK", "exit": 0})

def arm_accumulated():
    """Incident shape: auto + temp 1.0 + thinking on, grow to >300k."""
    from collections import Counter
    msgs = [{"role": "user", "content":
             "You are a site-reliability agent. Work the checklist with one tool call "
             "per turn (write/read/grep/bash cycle). Historical archive notes:\n"
             + blocks(5000, 6)}]
    c = Counter()
    depth_at_fail = []
    turn = 0
    t0 = time.time()
    while turn < A.max_turns:
        turn += 1
        if turn > 1:
            msgs.append({"role": "user", "content":
                         "Turn %d. Continue the checklist. New archive notes:\n%s"
                         % (turn, blocks(6000 + turn, 10))})  # ~9.1k tok/turn
        body = {"model": A.model, "messages": msgs, "temperature": 1.0,
                "top_p": 0.95, "max_tokens": 4096,
                "chat_template_kwargs": {"thinking": True},
                "tools": TOOLS, "tool_choice": "auto"}
        d, err, lat = post(body)
        if err:
            row = dict(arm="E-accum", i=turn, cls="NET_ERR", detail=err[:120],
                       lat=round(lat, 1), ptok=0, ctok=0, reason_len=0,
                       content_len=0, finish="-", head="")
            c["NET_ERR"] += 1
            emit(row)
            msgs.append({"role": "user", "content": "That turn failed; continue."})
            continue
        cls, detail, meta = classify(d)
        row = dict(arm="E-accum", i=turn, cls=cls, detail=detail,
                   lat=round(lat, 1), **meta)
        emit(row)
        c[cls.split("+")[0]] += 1
        if cls not in ("TOOL_OK",):
            depth_at_fail.append((turn, meta["ptok"], cls))
        tcs = (d["choices"][0]["message"].get("tool_calls") or [])
        if tcs:
            for k, tc in enumerate(tcs):
                try:
                    ar = json.loads(tc["function"]["arguments"] or "{}")
                except Exception:
                    ar = {}
                msgs.append({"role": "assistant", "content": None,
                             "tool_calls": [{"id": "t%d-%d" % (turn, k), "type": "function",
                                             "function": {"name": tc["function"]["name"],
                                                          "arguments": tc["function"]["arguments"]}}]})
                msgs.append({"role": "tool", "tool_call_id": "t%d-%d" % (turn, k),
                             "content": exec_fake(tc["function"]["name"], ar)[:600]})
        else:
            txt = d["choices"][0]["message"].get("content") or ""
            msgs.append({"role": "assistant", "content": txt[:400] or "(empty)"})
        if meta["ptok"] > 300000:
            print("E-accum reached %.0fk tokens at turn %d" % (meta["ptok"] / 1000, turn),
                  flush=True)
            break
    print("E-accum -> turns=%d %s fails@depth=%s (%.0fs)"
          % (turn, dict(c), depth_at_fail[:10], time.time() - t0), flush=True)

def main():
    os.makedirs(os.path.dirname(A.jsonl), exist_ok=True)
    open(A.jsonl, "w").close()
    if not A.skip_a:
        print("== A: single-turn 230k/300k ==", flush=True)
        arm_single()
    print("== E: accumulated auto loop -> 300k+ ==", flush=True)
    arm_accumulated()
    print("DONE", flush=True)

sys.exit(main())
