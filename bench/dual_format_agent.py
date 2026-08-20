#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DSV4 dual-format tool-calling + sustained-agent test (5700 / dsv4s).

Adapted from the《助手双格式工具调用测试方案 v3》plan:
  - XML group   (P2-P4):  prompt-protocol <tool_call> blocks, single/serial/concurrent
  - JSON group  (P5-P7):  native OpenAI `tools` param, single/serial/concurrent
  - Mixed group (P8-P11): formats alternated within a round
  - P12 context continuity, P13 sustained agent loops (user-added: 3 loops x 32 turns),
    P14 report generation (tmp/reports/tool_test_report.md)

Every model tool call is validated: name registered, arguments parse, required
fields present + typed, enums honored. Leak signatures from issue
01-shuzhipost-dsf-xml-leak are counted separately from hard failures. Tools are
executed against a simulated in-memory filesystem; results are fed back so
serial/agent rounds exercise real multi-turn chaining.

Usage:
  python3 dual_format_agent.py --api-key KEY [--base-url http://47.99.74.105:5700]
                               [--model dsv4s] [--quick]
"""
import argparse, json, os, random, re, sys, threading, time
import urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor

AP = argparse.ArgumentParser()
AP.add_argument("--base-url", default="http://47.99.74.105:5700")
AP.add_argument("--model", default="dsv4s")
AP.add_argument("--api-key", default=os.environ.get("DSV4_API_KEY", ""))
AP.add_argument("--quick", action="store_true", help="smoke scale (1 round/phase)")
AP.add_argument("--agent-turns", type=int, default=32)
AP.add_argument("--out", default="tmp/reports/tool_test_report.md")
AP.add_argument("--jsonl", default="tmp/reports/dual_format_calls.jsonl")
A = AP.parse_args()
BASE = A.base_url.rstrip("/")
SCALED = 0.5 if A.quick else 1.0

# ---------------------------------------------------------------- registry
REG = {
    "write_file": {"desc": "Write content to a file path",
        "params": {"path": ("string", True), "content": ("string", True)}},
    "read_file": {"desc": "Read a file by path",
        "params": {"path": ("string", True), "offset": ("integer", False)}},
    "edit_file": {"desc": "Replace old_string with new_string in a file",
        "params": {"path": ("string", True), "old_string": ("string", True),
                   "new_string": ("string", True)}},
    "move_file": {"desc": "Move/rename a file",
        "params": {"src": ("string", True), "dst": ("string", True)}},
    "ls": {"desc": "List entries under a directory",
        "params": {"path": ("string", True)}},
    "glob": {"desc": "Find files matching a pattern",
        "params": {"pattern": ("string", True)}},
    "grep": {"desc": "Search file contents for a regex",
        "params": {"pattern": ("string", True), "path": ("string", False),
                   "mode": ("enum:content|files|count", False)}},
    "bash": {"desc": "Run a shell command in the sandbox",
        "params": {"command": ("string", True)}},
}
JSON_TOOLS = []
for n, s in REG.items():
    props, req = {}, []
    for p, (t, r) in s["params"].items():
        if t.startswith("enum:"):
            opts = t[5:].split("|")
            props[p] = {"type": "string", "enum": opts}
        else:
            props[p] = {"type": t}
        if r:
            req.append(p)
    JSON_TOOLS.append({"type": "function", "function": {
        "name": n, "description": s["desc"],
        "parameters": {"type": "object", "properties": props, "required": req}}})

def xml_tool_doc():
    parts = []
    for n, s in REG.items():
        pl = []
        for p, (t, r) in s["params"].items():
            pl.append("%s: %s%s" % (p, t, " (required)" if r else " (optional)"))
        parts.append("<tool>\n<name>%s</name>\n<description>%s</description>\n"
                     "<parameters>%s</parameters>\n</tool>" % (n, s["desc"], "; ".join(pl)))
    return "\n".join(parts)

XML_SYSTEM = (
    "You can call tools that operate on a sandbox filesystem.\n"
    "Available tools:\n" + xml_tool_doc() + "\n"
    "To call a tool, respond with EXACTLY one block and nothing else:\n"
    "<tool_call>\n<name>tool_name</name>\n"
    '<arguments>{"key": "value"}</arguments>\n</tool_call>\n'
    "<arguments> must be valid JSON. No prose, no markdown fences, no other tags.")

# leak signatures (issue 01) — anything tool-protocol-ish outside the proper channel
LEAK_SIGS = ["<bash_command>", "<Bash command", "<call ", "<Write ", "<answer>",
             "<analyze>", "</Bash>", "PYEOF", "<tool_calls", "<tool-call-name",
             "<thinking>", "<dies_cmd_wrapper"]
TOOLCALL_RE = re.compile(
    r"<tool_call>\s*<name>(.*?)</name>\s*<arguments>(.*?)</arguments>\s*</tool_call>",
    re.S)
OPEN_ONLY_RE = re.compile(r"<tool_call>.*?(?:<name>|<arguments>)", re.S)

# ------------------------------------------------------------ virtual sandbox
FS = {}
FS_LOCK = threading.Lock()

def fs_reset(seed=0):
    rnd = random.Random(seed)
    with FS_LOCK:
        FS.clear()
        FS["/tmp/notes_%d.txt" % seed] = "alpha beta gamma\n" * 3
        FS["/data/log.txt"] = "\n".join("line %d: status ok %d" % (i, rnd.randint(1, 9))
                                        for i in range(20))

def exec_tool(name, args):
    """Execute a registered tool against the simulated FS; returns a result string."""
    try:
        with FS_LOCK:
            if name == "write_file":
                FS[str(args["path"])] = str(args["content"])
                return json.dumps({"bytes_written": len(str(args["content"]))})
            if name == "read_file":
                p = str(args["path"])
                if p not in FS:
                    return json.dumps({"error": "ENOENT: " + p})
                body = FS[p]
                off = args.get("offset")
                if isinstance(off, int) and off > 0:
                    body = "\n".join(body.split("\n")[off:])
                return json.dumps({"content": body[:2000]})
            if name == "edit_file":
                p = str(args["path"])
                if p not in FS:
                    return json.dumps({"error": "ENOENT: " + p})
                old, new = str(args["old_string"]), str(args["new_string"])
                if old not in FS[p]:
                    return json.dumps({"error": "old_string not found"})
                FS[p] = FS[p].replace(old, new, 1)
                return json.dumps({"bytes_written": len(FS[p])})
            if name == "move_file":
                s, d = str(args["src"]), str(args["dst"])
                if s not in FS:
                    return json.dumps({"error": "ENOENT: " + s})
                FS[d] = FS.pop(s)
                return json.dumps({"moved": s, "to": d})
            if name == "ls":
                p = str(args["path"]).rstrip("/") + "/"
                ents = sorted({k for k in FS if k.startswith(p)})
                return json.dumps({"entries": [e[len(p):].split("/")[0] for e in ents]})
            if name == "glob":
                pat = str(args["pattern"]).lstrip("./")
                rx = re.compile("^" + re.escape(pat).replace(r"\*", "[^/]*") + "$")
                return json.dumps({"matches": sorted(k for k in FS if rx.match(k))[:50]})
            if name == "grep":
                pat = str(args["pattern"])
                rx = re.compile(pat)
                out = []
                base = str(args.get("path") or "/")
                for k, v in sorted(FS.items()):
                    if not k.startswith(base):
                        continue
                    for i, ln in enumerate(v.split("\n")):
                        if rx.search(ln):
                            out.append("%s:%d:%s" % (k, i + 1, ln[:80]))
                return json.dumps({"matches": out[:50]})
            if name == "bash":
                return json.dumps({"stdout": "OK: " + str(args["command"])[:80]
                                   + "\nexit 0", "exit": 0})
        return json.dumps({"error": "unknown tool"})
    except Exception as e:
        return json.dumps({"error": repr(e)[:120]})

def validate(name, args):
    """Schema-validate one parsed call. Returns error string or None."""
    if name not in REG:
        return "UNKNOWN_TOOL name=%s" % name
    for p, (t, req) in REG[name]["params"].items():
        if req and (p not in args or args[p] in (None, "")):
            return "MISSING_ARG %s.%s" % (name, p)
        if p in args and args[p] is not None:
            v = args[p]
            if t.startswith("enum:"):
                if str(v) not in t[5:].split("|"):
                    return "BAD_ENUM %s.%s=%r" % (name, p, v)
            elif t == "string" and not isinstance(v, str):
                return "BAD_TYPE %s.%s want str got %s" % (name, p, type(v).__name__)
            elif t == "integer" and not isinstance(v, int):
                return "BAD_TYPE %s.%s want int got %s" % (name, p, type(v).__name__)
    return None

# ------------------------------------------------------------------ transport
def post(body, timeout=300):
    hdrs = {"Content-Type": "application/json"}
    if A.api_key:
        hdrs["Authorization"] = "Bearer " + A.api_key
    req = urllib.request.Request(BASE + "/v1/chat/completions",
                                 data=json.dumps(body).encode(), headers=hdrs)
    t0 = time.time()
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(r.read().decode()), None, time.time() - t0
    except urllib.error.HTTPError as e:
        return None, "http%d: %s" % (e.code, e.read().decode(errors="replace")[:200]), time.time() - t0
    except Exception as e:
        return None, "conn:" + repr(e)[:100], time.time() - t0

# ------------------------------------------------------------------ recording
REC = []          # one dict per model call attempt
REC_LOCK = threading.Lock()
ROUNDS = [0]
JSONL_F = None

def record(phase, rnd, pattern, fmt, calls, err=None, leak=None, dup=0,
           lat=0.0, ptok=0, ctok=0, recovered=False, note="", soft=None):
    row = dict(phase=phase, round=rnd, pattern=pattern, fmt=fmt,
               calls=calls, err=err, leak=leak, dup=dup, lat=round(lat, 2),
               ptok=ptok, ctok=ctok, recovered=recovered, note=note, soft=soft)
    with REC_LOCK:
        REC.append(row)
        if JSONL_F is not None:
            JSONL_F.write(json.dumps(row, ensure_ascii=False) + "\n")
            JSONL_F.flush()

def parse_fmt(fmt, body, d):
    """Extract validated tool calls from a response. Returns (calls, err, leak, dup).

    calls: list of (name, args). err: hard failure reason. leak: leaked-signature
    string. dup: duplicate consecutive calls with identical (name,args).
    """
    m = d["choices"][0]["message"]
    ch = d["choices"][0]
    txt = m.get("content") or ""
    leak_hit = [s for s in LEAK_SIGS if s in txt]
    leak = ",".join(leak_hit[:3]) if leak_hit else None
    calls = []
    if fmt == "json":
        tcs = m.get("tool_calls") or []
        if "<tool_call>" in txt:
            leak = (leak + "," if leak else "") + "<tool_call>in-text"
        for tc in tcs:
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
            except Exception as e:
                return calls, "ARG_PARSE " + repr(e)[:60], leak, 0
            calls.append((tc["function"]["name"], args))
        if not calls:
            return calls, "NOTOOL finish=%s" % ch.get("finish_reason"), leak, 0
    else:  # xml
        blocks = TOOLCALL_RE.findall(txt)
        if not blocks:
            if OPEN_ONLY_RE.search(txt):
                return calls, "XML_TRUNCATED", leak, 0
            return calls, "NO_XML_BLOCK finish=%s" % ch.get("finish_reason"), leak, 0
        extra = TOOLCALL_RE.sub("", txt).strip()
        if len(extra) > 120:
            leak = (leak + "," if leak else "") + "prose-around-block"
        for nm, ar in blocks[:3]:
            try:
                args = json.loads(ar.strip())
            except Exception as e:
                return calls, "XML_ARG_PARSE " + repr(e)[:60], leak, 0
            calls.append((nm.strip(), args))
    dup = 0
    if len(calls) > 1:
        uniq = {(n, json.dumps(a, sort_keys=True)) for n, a in calls}
        dup = len(calls) - len(uniq)
    for n, a in calls:
        verr = validate(n, a)
        if verr:
            return calls, verr, leak, dup
    return calls, None, leak, dup

def ask(fmt, messages, temp, max_tokens, require_tool=True, tool_hint=None):
    """One model round-trip in the given format. Returns (calls, err, leak, dup, meta)."""
    body = {"model": A.model, "messages": messages, "temperature": temp,
            "top_p": 0.95, "max_tokens": max_tokens}
    if fmt == "json":
        body["tools"] = JSON_TOOLS
        body["tool_choice"] = "required" if require_tool else "auto"
        body["chat_template_kwargs"] = {"thinking": True}
    else:
        msgs = [dict(messages[0])]
        msgs[0] = dict(msgs[0]); msgs[0]["content"] = XML_SYSTEM
        msgs.extend(messages[1:])
        body["messages"] = msgs
    d, err, lat = post(body)
    if err:
        return [], err, None, 0, dict(lat=lat, ptok=0, ctok=0)
    u = d.get("usage", {})
    calls, verr, leak, dup = parse_fmt(fmt, body, d)
    meta = dict(lat=lat, ptok=u.get("prompt_tokens", 0), ctok=u.get("completion_tokens", 0))
    if verr and tool_hint:
        # expected-tool mismatch counts only when call itself was otherwise valid
        if calls and all(n == tool_hint for n, _ in calls) is False:
            meta["soft_mismatch"] = "%s!=%s" % (calls[0][0], tool_hint)
    return calls, verr, leak, dup, meta

def one_call(phase, rnd, pattern, fmt, instruction, temp, expected=None, max_tokens=512):
    """Single-request round with validation, leak/dup capture, one recovery retry."""
    user = instruction if expected is None else instruction
    messages = [{"role": "system", "content": "placeholder"} if fmt == "xml"
                else {"role": "system", "content": "You are a precise agent."},
                {"role": "user", "content": user}]
    calls, err, leak, dup, meta = ask(fmt, messages, temp, max_tokens)
    recovered = False
    if err:
        time.sleep(1)
        retry = [{"role": "system", "content": "placeholder"},
                 {"role": "user", "content": "Respond with exactly one valid tool "
                  "call. " + user}]
        if fmt != "xml":
            retry[0] = {"role": "system", "content": "You are a precise agent. "
                        "Call the appropriate tool."}
        c2, e2, l2, d2, m2 = ask(fmt, retry, min(temp, 0.3), max_tokens)
        if e2 is None:
            calls, err, leak, dup, meta, recovered = c2, None, (leak or l2), dup or d2, m2, True
    ok = err is None
    record(phase, rnd, pattern, fmt,
           [c[0] for c in calls], err=err, leak=leak, dup=dup, lat=meta["lat"],
           ptok=meta["ptok"], ctok=meta["ctok"], recovered=recovered,
           note=("exp=" + expected) if expected else "")
    return ok, calls, meta

# ------------------------------------------------------------------- phases
def n_rounds(base):
    return max(1, int(base * SCALED))

def phase_single(fmt, phase, temps=(0.0, 0.7, 1.0)):
    """P2/P5: single calls, simple/normal/complex params, repeats."""
    specs = [
        ("write_file", "Create /tmp/%s_hello.txt with the content: hello 双格式 test 🚀 line2" % fmt, None),
        ("read_file", "Read the file /tmp/notes_0.txt using the read_file tool.", None),
        ("ls", "List the directory /tmp using the ls tool.", None),
        ("glob", "Find files matching pattern /tmp/*.txt with the glob tool.", None),
        ("grep", "Search for pattern 'status ok' with grep in content mode.", None),
        ("bash", "Run the command 'df -h' with the bash tool.", None),
        ("edit_file", "In file /tmp/notes_0.txt replace the string 'alpha' with 'ALPHA' using edit_file.", None),
        ("write_file", "Call write_file to create /tmp/%s_cfg.json with content {\"a\": 1, \"b\": [2, 3], \"u\": \"中文üñ\"}." % fmt, None),
        ("move_file", "Move /data/log.txt to /data/log_old.txt using move_file.", None),
        ("bash", "Call the bash tool with command 'echo \"a;b|c\" && grep -c ok /data/log.txt'.", None),
    ]
    rnd = 0
    for i, (tool, instr, _) in enumerate(specs):
        rnd = i // 2 + 1
        temp = temps[i % len(temps)]
        one_call(phase, rnd, "single", fmt, instr, temp, expected=tool)

def serial_chain(phase, fmt, rnd, tag):
    """One serial round: 6-10 chained steps, step k's output feeds step k+1."""
    fs_reset(seed=rnd)
    chain = [
        ("write_file", "Step 1: create /w/round%d.txt with content 'seed-A\nseed-B\nseed-C' using the write_file tool." % rnd),
        ("read_file", "Step 2: read back /w/round%d.txt using the read_file tool." % rnd),
        ("edit_file", "Step 3: in /w/round%d.txt replace 'seed-B' with 'SEED-B' using the edit_file tool." % rnd),
        ("read_file", "Step 4: read /w/round%d.txt again using the read_file tool." % rnd),
        ("grep", "Step 5: grep for 'SEED-B' under /w using the grep tool." ),
        ("ls", "Step 6: list /w using the ls tool."),
        ("glob", "Step 7: glob pattern /w/*.txt using the glob tool."),
        ("move_file", "Step 8: move /w/round%d.txt to /w/final%d.txt using the move_file tool." % (rnd, rnd)),
        ("bash", "Step 9: run 'wc -c /w/final%d.txt' using the bash tool." % rnd),
        ("read_file", "Step 10: read /w/final%d.txt using the read_file tool." % rnd),
    ]
    history = []
    chain_ok = 0
    for k, (tool, instr) in enumerate(chain, 1):
        msgs = ([{"role": "system", "content": ""},
                 {"role": "user", "content": "Filesystem task, follow steps strictly. " + instr}]
                if not history else
                [{"role": "system", "content": ""}] + history +
                [{"role": "user", "content": instr}])
        msgs[0] = dict(msgs[0])
        calls, err, leak, dup, meta = ask(fmt, msgs, 0.7, 768)
        recovered = False
        if err:
            c2, e2, l2, d2, m2 = ask(fmt, [{"role": "system", "content": ""},
                {"role": "user", "content": "Respond with exactly one valid tool call. " + instr}],
                0.2, 768)
            if e2 is None:
                calls, err, leak, meta, recovered = c2, None, leak or l2, m2, True
                dup = dup or d2
        ok = err is None and bool(calls)
        soft = None
        if ok and calls[0][0] != tool:
            soft = "got=%s want=%s" % (calls[0][0], tool)
        if soft is None:
            chain_ok += 1
        record(phase, rnd, "serial", fmt, [c[0] for c in calls], err=err, leak=leak,
               dup=dup, lat=meta["lat"], ptok=meta["ptok"], ctok=meta["ctok"],
               recovered=recovered, note="step%d exp=%s" % (k, tool), soft=soft)
        # feed result back in the step's native format
        if calls:
            nm, ar = calls[0]
            res = exec_tool(nm, ar)
            if fmt == "json":
                history.append({"role": "assistant", "content": None,
                                "tool_calls": [{"id": "s%d" % k, "type": "function",
                                                "function": {"name": nm,
                                                             "arguments": json.dumps(ar, ensure_ascii=False)}}]})
                history.append({"role": "tool", "tool_call_id": "s%d" % k,
                                "content": res[:600]})
            else:
                history.append({"role": "assistant",
                                "content": "<tool_call><name>%s</name><arguments>%s</arguments></tool_call>"
                                           % (nm, json.dumps(ar, ensure_ascii=False))})
                history.append({"role": "user", "content": "tool result: " + res[:600]})
        else:
            history.append({"role": "user", "content": "That call failed; continue."})
    return chain_ok, len(chain)

def concurrent_round(phase, rnd, fmt_mix, tag):
    """Fire 3-5 independent single calls concurrently (max 5)."""
    tasks = [
        ("write_file", "Create /c/%s_%d.txt with content 'concurrent-%d'." % (tag, rnd, i))
        for i in range(2)
    ] + [
        ("ls", "List /c with the ls tool."),
        ("glob", "Glob /c/*.txt."),
        ("grep", "Grep pattern 'concurrent' under /c in content mode."),
    ][:len(fmt_mix)]
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = []
        for (tool, instr), fmt in zip(tasks, fmt_mix):
            futs.append(ex.submit(one_call, phase, rnd, "concurrent", fmt, instr,
                                  0.7, expected=tool))
        return [f.result()[0] for f in futs]

def mixed_serial(phase, rnd):
    """XML/JSON alternating serial chain (uses serial_chain mechanics per step)."""
    fs_reset(seed=100 + rnd)
    steps = [
        ("json", "write_file", "Step 1: create /m/round%d.md with 'title: mix' using the write_file tool." % rnd),
        ("xml", "read_file", "Step 2: read /m/round%d.md using the read_file tool." % rnd),
        ("json", "edit_file", "Step 3: replace 'mix' with 'MIX' in /m/round%d.md using the edit_file tool." % rnd),
        ("xml", "read_file", "Step 4: verify /m/round%d.md using the read_file tool." % rnd),
        ("json", "grep", "Step 5: grep 'MIX' under /m using the grep tool."),
        ("xml", "bash", "Step 6: run 'ls /m' using the bash tool."),
    ]
    history = []
    okc = 0
    for k, (fmt, tool, instr) in enumerate(steps, 1):
        msgs = ([{"role": "system", "content": ""}, {"role": "user", "content": instr}]
                if not history else
                [{"role": "system", "content": ""}] + history +
                [{"role": "user", "content": instr}])
        msgs[0] = dict(msgs[0])
        calls, err, leak, dup, meta = ask(fmt, msgs, 0.6, 640)
        recovered = False
        if err:
            c2, e2, l2, d2, m2 = ask(fmt, [{"role": "system", "content": ""},
                {"role": "user", "content": "Respond with exactly one valid tool call. " + instr}],
                0.2, 640)
            if e2 is None:
                calls, err, leak, meta, recovered = c2, None, leak or l2, m2, True
                dup = dup or d2
        ok = err is None and bool(calls)
        soft = None
        if ok and calls[0][0] != tool:
            soft = "got=%s want=%s" % (calls[0][0], tool)
        if soft is None:
            okc += 1
        record(phase, rnd, "serial-mixed", fmt, [c[0] for c in calls], err=err, leak=leak,
               dup=dup, lat=meta["lat"], ptok=meta["ptok"], ctok=meta["ctok"],
               recovered=recovered, note="step%d exp=%s" % (k, tool), soft=soft)
        if calls:
            nm, ar = calls[0]
            res = exec_tool(nm, ar)
            if fmt == "json":
                history.append({"role": "assistant", "content": None,
                                "tool_calls": [{"id": "m%d" % k, "type": "function",
                                                "function": {"name": nm,
                                                             "arguments": json.dumps(ar, ensure_ascii=False)}}]})
                history.append({"role": "tool", "tool_call_id": "m%d" % k,
                                "content": res[:600]})
            else:
                history.append({"role": "assistant",
                                "content": "<tool_call><name>%s</name><arguments>%s</arguments></tool_call>"
                                           % (nm, json.dumps(ar, ensure_ascii=False))})
                history.append({"role": "user", "content": "tool result: " + res[:600]})
    return okc, len(steps)

def stress_round(phase, rnd):
    """P11: one round mixing single+concurrent+serial shapes and formats."""
    ok = 0
    ok1, _, _ = one_call(phase, rnd, "stress-single", "xml" if rnd % 2 else "json",
                         "Create /s/stress%d.txt with content 'stress-%d'." % (rnd, rnd),
                         1.0, expected="write_file")
    ok += 1 if ok1 else 0
    cres = concurrent_round(phase, rnd, ("xml", "json", "xml", "json")[:3 + rnd % 3], "stress")
    ok += sum(1 for c in cres if c)
    cok, ctot = serial_chain(phase, "json" if rnd % 2 else "xml", rnd, "stress")
    ok += 1 if cok == ctot else 0
    return ok

def agent_loop(phase, fmt, turns, tag):
    """P13 sustained agent work: `turns` rounds of tool use, growing context,
    filler notes each turn (agent_loop.py shape). Model must keep calling tools."""
    rnd_all_ok = True
    fails = []
    t0 = time.time()
    msgs = [{"role": "system", "content": ""},
            {"role": "user", "content":
             "You are a site-reliability agent. Work the checklist with one tool "
             "call per turn: 1) write a report file 2) read it 3) edit it 4) grep "
             "logs 5) list dirs 6) run bash checks — repeat the cycle with deeper "
             "detail until told to stop. Answer every turn with exactly one tool "
             "call%s, no prose. Previously collected notes:\n%s"
             % (" block" if fmt == "xml" else "", filler_doc(900 + hash(tag) % 50))}]
    for t in range(1, turns + 1):
        if t > 1:
            msgs.append({"role": "user", "content":
                         "Turn %d. Continue the checklist. New log-archive notes to weigh:\n%s"
                         % (t, filler_doc(1000 + t))})
        body_msgs = msgs
        calls, err, leak, dup, meta = ask(fmt, body_msgs, 1.0, 2048)
        ok = err is None and bool(calls)
        record(phase, t, "agent", fmt, [c[0] for c in calls], err=err, leak=leak,
               dup=dup, lat=meta["lat"], ptok=meta["ptok"], ctok=meta["ctok"],
               note="loop=%s turn=%d" % (tag, t))
        if not ok:
            rnd_all_ok = False
            fails.append((t, err))
            msgs.append({"role": "user", "content": "That turn failed (%s). Continue." % err})
            continue
        nm, ar = calls[0]
        res = exec_tool(nm, ar)
        if fmt == "json":
            msgs.append({"role": "assistant", "content": None,
                         "tool_calls": [{"id": "c%d" % t, "type": "function",
                                         "function": {"name": nm, "arguments": json.dumps(ar, ensure_ascii=False)}}]})
            msgs.append({"role": "tool", "tool_call_id": "c%d" % t, "content": res[:600]})
        else:
            msgs.append({"role": "assistant",
                         "content": "<tool_call><name>%s</name><arguments>%s</arguments></tool_call>"
                                    % (nm, json.dumps(ar, ensure_ascii=False))})
            msgs.append({"role": "user", "content": "tool result: " + res[:600]})
    return rnd_all_ok, fails, time.time() - t0

FILL_PKG = ["scheduler", "grammar", "drafter", "kv-cache", "indexer", "allocator"]
def filler_doc(seed):
    rnd = random.Random(seed)
    lines = []
    for i in range(40):
        lines.append(
            "Note %d: the %s/%s interaction was re-audited; acceptance window %d "
            "kept invariants under rollback order %d. Measured %.2f, drafted %d, "
            "rejected %d." % (i, rnd.choice(FILL_PKG), rnd.choice(FILL_PKG),
                              i, rnd.randint(1, 3), rnd.uniform(0.6, 0.95),
                              rnd.randint(3, 6), rnd.randint(0, 2)))
    return "\n".join(lines)

def context_check(phase, rnd, path, content_id, fmt, marker):
    """P12: reference something created phases ago, cross-format."""
    ok1, _, _ = one_call(phase, rnd, "context", fmt,
                         "Earlier in this session a file %s containing '%s' was "
                         "created. Read it back with read_file and confirm the marker."
                         % (path, marker), 0.3, expected="read_file")
    return ok1

# --------------------------------------------------------------------- main
def main():
    global JSONL_F
    os.makedirs(os.path.dirname(A.jsonl), exist_ok=True)
    JSONL_F = open(A.jsonl, "w")
    t_start = time.time()
    print("== P1 init ==", flush=True)
    d, err, lat = post({"model": A.model, "messages": [{"role": "user", "content": "ping"}],
                        "max_tokens": 8, "temperature": 0})
    assert err is None, "service unreachable: " + err
    print("service ok, ping %.2fs" % lat, flush=True)

    print("== P2 XML single ==", flush=True)
    phase_single("xml", "P2")
    print("== P3 XML serial ==", flush=True)
    for r in range(1, n_rounds(5) + 1):
        ok, tot = serial_chain("P3", "xml", r, "xml")
        print("  round %d chain %d/%d" % (r, ok, tot), flush=True)
    print("== P4 XML concurrent ==", flush=True)
    for r in range(1, n_rounds(5) + 1):
        k = 3 + (r % 3)  # 3,4,5,3,4
        res = concurrent_round("P4", r, ("xml",) * k, "xml")
        print("  round %d conc=%d ok=%d" % (r, k, sum(res)), flush=True)

    print("== P5 JSON single ==", flush=True)
    phase_single("json", "P5")
    print("== P6 JSON serial ==", flush=True)
    for r in range(1, n_rounds(5) + 1):
        ok, tot = serial_chain("P6", "json", r, "json")
        print("  round %d chain %d/%d" % (r, ok, tot), flush=True)
    print("== P7 JSON concurrent ==", flush=True)
    for r in range(1, n_rounds(5) + 1):
        k = 3 + (r % 3)
        res = concurrent_round("P7", r, ("json",) * k, "json")
        print("  round %d conc=%d ok=%d" % (r, k, sum(res)), flush=True)

    print("== P8 mixed single ==", flush=True)
    for r in range(1, n_rounds(3) + 1):
        one_call("P8", r, "single-mixed", "xml" if r % 2 else "json",
                 "Create /x/mix%d.txt with content 'mix-single-%d'." % (r, r), 0.5,
                 expected="write_file")
        one_call("P8", r, "single-mixed", "json" if r % 2 else "xml",
                 "Read /x/mix%d.txt back." % r, 0.5, expected="read_file")
    print("== P9 mixed serial ==", flush=True)
    for r in range(1, n_rounds(3) + 1):
        ok, tot = mixed_serial("P9", r)
        print("  round %d chain %d/%d" % (r, ok, tot), flush=True)
    print("== P10 mixed concurrent ==", flush=True)
    for r in range(1, n_rounds(3) + 1):
        mixes = [("xml", "json", "xml"), ("json", "xml", "json", "xml"),
                 ("xml", "xml", "json", "json", "json")][r - 1]
        res = concurrent_round("P10", r, mixes, "mix")
        print("  round %d conc=%d ok=%d" % (r, len(mixes), sum(res)), flush=True)

    print("== P11 stress ==", flush=True)
    for r in range(1, n_rounds(4) + 1):
        ok = stress_round("P11", r)
        print("  round %d ok-parts=%d" % (r, ok), flush=True)

    print("== P12 context continuity ==", flush=True)
    for r in range(1, n_rounds(3) + 1):
        fmt = "xml" if r % 2 else "json"
        fs_reset(0)
        with FS_LOCK:
            FS["/tmp/ctx_ref_%d.txt" % r] = "marker=CTX-%d ok" % r
        context_check("P12", r, "/tmp/ctx_ref_%d.txt" % r, r, fmt, "CTX-%d" % r)

    print("== P13 sustained agent loops ==", flush=True)
    turns = max(4, int(A.agent_turns * SCALED))
    for tag, fmt in (("A-json", "json"), ("B-xml", "xml"), ("C-json", "json")):
        all_ok, fails, dur = agent_loop("P13", fmt, turns, tag)
        print("  loop %s (%s): %d turns, %d fails, %.0fs"
              % (tag, fmt, turns, len(fails), dur), flush=True)
        for t, e in fails[:5]:
            print("    turn %d: %s" % (t, e), flush=True)

    total = time.time() - t_start
    print("\nTOTAL %.0fs, %d recorded calls" % (total, len(REC)), flush=True)
    JSONL_F.close()
    write_report(total)
    bad = [r for r in REC if r["err"] and not r["recovered"]]
    print("VERDICT:", "ALL GOOD" if not bad else "%d UNRECOVERED FAILURES" % len(bad))
    return 0 if not bad else 1

def write_report(total):
    def sel(**kw):
        return [r for r in REC if all(r.get(k) == v for k, v in kw.items())]
    def stat(rs):
        n = len(rs)
        ok = sum(1 for r in rs if r["err"] is None)
        rec = sum(1 for r in rs if r["err"] is None and r["recovered"])
        lats = [r["lat"] for r in rs] or [0]
        return n, ok, n - ok, (100.0 * ok / n if n else 0), rec, \
               round(sum(lats) / len(lats), 2), round(max(lats), 2), round(min(lats), 2)
    rounds = len({(r["phase"], r["round"]) for r in REC})
    lines = []
    W = lines.append
    W("# 助手双格式工具调用测试报告 — dsv4s @ %s" % BASE)
    W("")
    W("生成时间: %s | harness: bench/dual_format_agent.py | raw: %s"
      % (time.strftime("%Y-%m-%d %H:%M:%S"), A.jsonl))
    W("")
    W("## 1. 环境信息")
    W("")
    W("| 项目 | 值 |")
    W("|------|-----|")
    W("| 服务 | %s (model=%s, max_model_len=524288) |" % (BASE, A.model))
    W("| 总轮次 | %d |" % rounds)
    W("| 总调用 | %d |" % len(REC))
    n, ok, f, pct, rec, la, lx, ln = stat(REC)
    W("| 成功/失败/成功率 | %d / %d / %.1f%% (恢复重试成功 %d) |" % (ok, f, pct, rec))
    W("| 平均/最大/最小延迟 | %.2fs / %.2fs / %.2fs |" % (la, lx, ln))
    W("| 总耗时 | %.0f 分钟 |" % (total / 60))
    W("")
    W("## 2. 分格式汇总")
    W("")
    W("| 格式 | 调用 | 成功 | 失败 | 成功率 | 恢复成功 |")
    W("|------|------|------|------|--------|----------|")
    for fmt in ("xml", "json"):
        n, ok, f, pct, rec, la, lx, ln = stat(sel(fmt=fmt))
        W("| %s | %d | %d | %d | %.1f%% | %d |" % (fmt.upper(), n, ok, f, pct, rec))
    W("")
    W("## 3. 分模式汇总（单次/串行/并发/混合/agent）")
    W("")
    W("| 模式 | 调用 | 成功 | 失败 | 成功率 |")
    W("|------|------|------|------|--------|")
    for pat in ("single", "serial", "concurrent", "single-mixed", "serial-mixed",
                "stress-single", "context", "agent"):
        n, ok, f, pct, rec, la, lx, ln = stat(sel(pattern=pat))
        if n:
            W("| %s | %d | %d | %d | %.1f%% |" % (pat, n, ok, f, pct))
    W("")
    W("## 4. 分 Phase 汇总")
    W("")
    W("| Phase | 调用 | 成功 | 失败 | 成功率 |")
    W("|-------|------|------|------|--------|")
    for ph in sorted({r["phase"] for r in REC}):
        n, ok, f, pct, rec, la, lx, ln = stat(sel(phase=ph))
        W("| %s | %d | %d | %d | %.1f%% |" % (ph, n, ok, f, pct))
    W("")
    W("## 5. 持续 Agent 轮数（P13）")
    W("")
    for tag in ("A-json", "B-xml", "C-json"):
        rs = [r for r in sel(phase="P13") if r["note"] == "loop=%s" % tag or
              ("loop=%s" % tag) in r["note"]]
        if not rs:
            continue
        n, ok, f, pct, rec, la, lx, ln = stat(rs)
        pt = rs[-1]["ptok"]
        W("- **%s**: %d 轮持续工具调用，成功 %d，失败 %d（%.1f%%），末端上下文 %d tok，"
          "平均延迟 %.1fs" % (tag, n, ok, f, pct, pt, la))
    W("")
    W("## 6. 异常观测")
    W("")
    leaks = [r for r in REC if r["leak"]]
    dups = [r for r in REC if r["dup"]]
    softs = [r for r in REC if r.get("soft")]
    W("- soft mismatch（调用格式合法但未按指定工具，多为偏用 bash）: %d 次" % len(softs))
    for r in softs[:8]:
        W("  - P%s r%d %s %s: %s (%s)" % (r["phase"], r["round"], r["fmt"],
                                          r["pattern"], r["soft"], r["note"]))
    W("- 泄漏签名（issue-01 类）出现: %d 次" % len(leaks))
    for r in leaks[:10]:
        W("  - P%s r%d %s %s: %s" % (r["phase"], r["round"], r["fmt"], r["pattern"], r["leak"]))
    W("- 重复 tool_calls（同 name+args 重复下发）: %d 次" % len(dups))
    for r in dups[:10]:
        W("  - P%s r%d %s %s dup=%d calls=%s" % (r["phase"], r["round"], r["fmt"],
                                                 r["pattern"], r["dup"], r["calls"]))
    W("")
    W("## 7. 失败案例与恢复")
    W("")
    fails = [r for r in REC if r["err"]]
    W("| Phase | 轮 | 格式 | 工具 | 错误 | 恢复 |")
    W("|-------|----|------|------|------|------|")
    for r in fails[:40]:
        W("| %s | %d | %s | %s | %s | %s |" % (
            r["phase"], r["round"], r["fmt"], (r["calls"] or ["-"])[0],
            (r["err"] or "")[:80], "yes" if r["recovered"] else "no"))
    W("")
    W("## 8. 结论要点")
    W("")
    n, ok, f, pct, rec, la, lx, ln = stat(sel(fmt="xml"))
    W("1. XML 提示式协议成功率 %.1f%%（%d/%d）。" % (pct, ok, n))
    n, ok, f, pct, rec, la, lx, ln = stat(sel(fmt="json"))
    W("2. JSON 原生 tool_calls 成功率 %.1f%%（%d/%d）。" % (pct, ok, n))
    ag = sel(pattern="agent")
    n, ok, f, pct, rec, la, lx, ln = stat(ag)
    W("3. 持续 agent 轮次成功率 %.1f%%（%d/%d 轮）。" % (pct, ok, n))
    W("4. 泄漏签名 %d 次，重复下发 %d 次（详见第 6 节）。" % (len(leaks), len(dups)))
    with open(A.out, "w") as f:
        f.write("\n".join(lines) + "\n")
    print("report -> %s" % A.out, flush=True)

sys.exit(main())
