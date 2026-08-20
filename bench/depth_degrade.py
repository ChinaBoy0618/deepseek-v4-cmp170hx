#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DSV4 depth-degradation discriminators (docs/issues 20260819/20260820 RCA).

Hypotheses under test, one arm each:
  H1 depth x tool_choice : emission decay only in the unconstrained (auto) tail?
  H2 required-path TYPE-B: grammar abandoned mid-request leaves unconstrained tail
                           (correlate client-side symptoms; server logs separately)
  H3 budget burn         : failures are max_tokens exhaustion, not emission decay
  H4 context pollution   : <bash> pseudo-tags in history teach the model pseudo-format

Arms:
  A  depth matrix  : {60k, 90k, 120k} x {auto, required} x N samples
  B  xml channel   : prompt-protocol (unconstrained by definition) @ 90k
  C  pollution     : tool-result text containing <bash> blocks vs clean control @ ~30k
  D  budget        : max_tokens 2048 vs 8192 @ 90k auto

Per-call classification: TOOL_OK / NO_TOOL_STOP / NO_TOOL_LENGTH / PSEUDO_TAG /
ARGS_BAD / DUP(+). Latency, prompt_tokens, reasoning vs content split recorded.

Usage: DSV4_API_KEY=... python3 bench/depth_degrade.py [--n 5]
"""
import argparse, json, os, random, re, sys, time
import urllib.request, urllib.error

AP = argparse.ArgumentParser()
AP.add_argument("--base-url", default="http://47.99.74.105:5700")
AP.add_argument("--model", default="dsv4s")
AP.add_argument("--api-key", default=os.environ.get("DSV4_API_KEY", ""))
AP.add_argument("-n", type=int, default=5, help="samples per matrix cell")
AP.add_argument("--jsonl", default="tmp/reports/depth_degrade.jsonl")
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
    {"type": "function", "function": {"name": "Grep",
        "description": "Search file contents", "parameters": {"type": "object",
        "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}}},
]
PSEUDO = ["<bash>", "<bash ", "<Bash ", "<bash_command>", "<｜DSML｜_tool_calls>",
          "<thinking>", "<call "]
TASK = "Run the command 'uptime' now using the Bash tool."

def filler(seed, docs):
    rnd = random.Random(seed)
    out = []
    for d in range(docs):
        lines = []
        for i in range(30):
            lines.append("Note %d.%d: the %s/%s window kept invariants; acceptance "
                         "%.2f, drafted %d, rejected %d." % (
                             d, i, rnd.choice(["scheduler", "grammar", "drafter"]),
                             rnd.choice(["bitmask", "allocator", "queue"]),
                             rnd.uniform(0.6, 0.95), rnd.randint(3, 6), rnd.randint(0, 2)))
        out.append("Investigation notes block %d:\n%s" % (d, "\n".join(lines)))
    return "\n\n".join(out)

def post(body, timeout=600):
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
    """Classify one response. Returns (cls, detail, meta)."""
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
                a = json.loads(tc["function"]["arguments"] or "{}")
                if not a:
                    okargs = False
            except Exception:
                okargs = False
        names = [tc["function"]["name"] for tc in tcs]
        dup = len(names) - len(set(names)) > 0 and len(tcs) > 1
        if not okargs:
            return "ARGS_BAD", "names=%s" % names, meta
        cls = "TOOL_OK" if "Bash" in names else "TOOL_WRONGNAME"
        if dup:
            cls += "+DUP"
        return cls, "n=%d names=%s" % (len(tcs), names), meta
    if finish == "length":
        if pseudo:
            return "PSEUDO+LEN", "pseudo=%s" % pseudo, meta
        return "NO_TOOL_LENGTH", "", meta
    if pseudo:
        return "PSEUDO_TAG", "pseudo=%s" % pseudo, meta
    return "NO_TOOL_STOP", "", meta

def run(arm, depth_tok, choice, mt, n, pollute=None, xml=False, tag=""):
    """Fire n samples of one cell; returns list of result dicts."""
    docs = max(1, depth_tok // 700)   # ~700 tok per notes block (calibrated)
    if pollute is not None:
        docs = pollute // 700
    out = []
    for i in range(n):
        msgs = [{"role": "user", "content":
                 "You are a site-reliability agent. Context notes from the archive:\n"
                 + filler(1000 + i, docs)
                 + "\n\nFinal instruction: " + TASK}]
        if pollute is not None:
            msgs = [
                {"role": "user", "content":
                 "You are a site-reliability agent. Earlier steps (context notes):\n"
                 + filler(2000 + i, docs)},
                {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "p1", "type": "function",
                     "function": {"name": "Bash",
                                  "arguments": json.dumps({"command": "ls dist/"})}}]},
                {"role": "tool", "tool_call_id": "p1",
                 "content": "<bash>ls dist/</bash>\napp.js\nvendor.js\n--- prior session log excerpt ---\n<bash>python3 -c \"import sys; import docx; d=docx.Document('培训规程.docx')\"</bash>\nextract ok"},
                {"role": "user", "content": "Final instruction: " + TASK},
            ]
        body = {"model": A.model, "messages": msgs, "temperature": 1.0,
                "top_p": 0.95, "max_tokens": mt,
                "chat_template_kwargs": {"thinking": True}}
        if xml:
            body["messages"] = [{"role": "system", "content":
                "Tools: Bash(command), Read(path), Grep(pattern). Respond with exactly "
                "one block <tool_call><name>NAME</name><arguments>{\"command\": "
                "\"uptime\"}</arguments></tool_call> and nothing else."}] + body["messages"]
        else:
            body["tools"] = TOOLS
            body["tool_choice"] = choice
        d, err, lat = post(body)
        if err:
            out.append(dict(arm=arm, i=i, cls="NET_ERR", detail=err[:120],
                            lat=round(lat, 1), ptok=0, tag=tag))
            print("  [%s %d] NET_ERR %s" % (arm, i, err[:80]), flush=True)
            continue
        cls, detail, meta = classify(d)
        row = dict(arm=arm, i=i, cls=cls, detail=detail, lat=round(lat, 1),
                   ptok=meta["ptok"], ctok=meta["ctok"], finish=meta["finish"],
                   reason_len=meta["reason_len"], content_len=meta["content_len"],
                   head=meta["head"], tag=tag)
        out.append(row)
        print("  [%s %d] %-18s ptok=%d ctok=%d rlen=%d clen=%d %.1fs %s %s"
              % (arm, i, cls, meta["ptok"], meta["ctok"], meta["reason_len"],
                 meta["content_len"], lat, detail, meta["head"][:70]), flush=True)
        with open(A.jsonl, "a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return out

def summarize(rows, label):
    from collections import Counter
    c = Counter(r["cls"] for r in rows)
    ok = c.get("TOOL_OK", 0)
    n = len(rows)
    print("%-28s n=%d  ok=%d (%.0f%%)  %s" % (label, n, ok, 100.0 * ok / max(1, n),
          dict(c)), flush=True)

def main():
    os.makedirs(os.path.dirname(A.jsonl), exist_ok=True)
    open(A.jsonl, "w").close()
    n = A.n
    allrows = []
    t0 = time.time()
    print("== A: depth x tool_choice ==", flush=True)
    for depth in (60000, 90000, 120000):
        for choice in ("auto", "required"):
            rows = run("A-%dk-%s" % (depth // 1000, choice), depth, choice, 4096, n)
            allrows += rows
            summarize(rows, "A %dk %s" % (depth // 1000, choice))
    print("== B: xml channel @90k ==", flush=True)
    rows = run("B-xml-90k", 90000, "auto", 4096, n, xml=True)
    allrows += rows
    summarize(rows, "B xml 90k")
    print("== C: pollution @30k ==", flush=True)
    rows = run("C-polluted", 30000, "auto", 4096, n, pollute=30000)
    allrows += rows
    summarize(rows, "C polluted")
    rows = run("C-clean", 30000, "auto", 4096, n)
    allrows += rows
    summarize(rows, "C clean")
    print("== D: budget @90k auto ==", flush=True)
    for mt in (2048, 8192):
        rows = run("D-mt%d" % mt, 90000, "auto", mt, n)
        allrows += rows
        summarize(rows, "D max_tokens=%d" % mt)
    print("\nDONE %.0fs, %d calls" % (time.time() - t0, len(allrows)), flush=True)

sys.exit(main())
