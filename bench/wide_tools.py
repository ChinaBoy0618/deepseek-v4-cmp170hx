#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DSV4 round-2 test: wide/complex tool schema stress.

0012-era salvage fires all came from real Claude Code traffic: ~15+
tools with nested JSON schemas -> a large grammar union. This harness
registers 20 tools (nested objects, arrays, enums, numbers, booleans),
sends N requests at temp 1.0 with a long filler context, and verifies
every returned tool call parses with valid arguments. Server-side
TYPE-B/salvage counts are correlated from logs.
"""
import argparse, json, random, sys, time, urllib.request, urllib.error

AP = argparse.ArgumentParser()
AP.add_argument("--base-url", default="http://localhost:5700")
AP.add_argument("--model", default="dsv4s")
AP.add_argument("-n", type=int, default=30)
AP.add_argument("--ctx-paras", type=int, default=80)
A = AP.parse_args()
BASE = A.base_url.rstrip("/")

PARA = (
    "Systems review context: scheduler windows, grammar bitmask timing, "
    "draft anchor assembly, and acceptance bookkeeping were re-audited. "
    "Each finding includes code references so the evidence chain can be "
    "reconstructed. This padding exists only to occupy prompt tokens. ")

TOOL_NAMES = ["Task", "Glob", "Grep", "Read", "Write", "Edit", "NotebookEdit",
              "Bash", "WebFetch", "WebSearch", "TodoWrite", "AskUserQuestion",
              "ListMcpResources", "ReadMcpResource", "EnterPlanMode",
              "ExitPlanMode", "KillShell", "BashOutput", "SlashCommand",
              "Compact"]

def mk_tool(i, name):
    rnd = random.Random(i)
    flavor = i % 4
    if flavor == 0:  # nested object
        deep = {"type": "object", "properties": {
            "level": {"type": "integer"},
            "tags": {"type": "array", "items": {"type": "string"}}},
            "required": ["level"]}
        opts = {"type": "object", "properties": {"deep": deep},
                "required": ["deep"]}
        params = {"type": "object", "properties": {
            "path": {"type": "string"}, "opts": opts},
            "required": ["path"]}
    elif flavor == 1:  # enum + bool + number
        params = {"type": "object", "properties": {
            "mode": {"type": "string", "enum": ["fast", "slow", "auto"]},
            "force": {"type": "boolean"},
            "ratio": {"type": "number"},
            "note": {"type": "string"}},
            "required": ["mode"]}
    elif flavor == 2:  # array of strings + integer
        params = {"type": "object", "properties": {
            "files": {"type": "array", "items": {"type": "string"}},
            "count": {"type": "integer"},
            "label": {"type": "string"}},
            "required": ["files"]}
    else:  # plain string
        params = {"type": "object", "properties": {
            "command": {"type": "string"},
            "description": {"type": "string"}},
            "required": ["command"]}
    return {"type": "function", "function": {
        "name": name,
        "description": "Tool %s: %s (idx %d, variant %d)" % (
            name, rnd.choice(["read", "write", "search", "execute"]), i, flavor),
        "parameters": params}}

TOOLS = [mk_tool(i, n) for i, n in enumerate(TOOL_NAMES)]

TASKS = [
    "call the Bash tool with command 'df -h'",
    "call the Edit tool to change a path",
    "call the TodoWrite tool with a files array",
    "call the Grep tool with mode auto and force true",
    "call the WebFetch tool with a command",
    "call the Read tool with a deep nested opts object",
    "call the Task tool with a files array and count 3",
    "call the Glob tool with a path",
]

def post(body, timeout=600):
    req = urllib.request.Request(BASE + "/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(r.read().decode()), None
    except urllib.error.HTTPError as e:
        return None, "http%d: %s" % (e.code, e.read().decode(errors="replace")[:200])
    except Exception as e:
        return None, "conn:" + repr(e)[:80]

def main():
    filler = PARA * A.ctx_paras
    npass = 0
    fails = []
    t0 = time.time()
    for i in range(A.n):
        task = TASKS[i % len(TASKS)]
        body = {"model": A.model, "temperature": 1.0, "top_p": 0.95,
                "max_tokens": 1536, "tools": TOOLS, "tool_choice": "required",
                "chat_template_kwargs": {"thinking": True},
                "messages": [{"role": "user", "content":
                    filler + "\n\nContext notes recorded. Final instruction: "
                    "you MUST " + task + "."}]}
        d, err = post(body)
        if err:
            fails.append((i, "NET", err))
            print("[%d/%d] NET %s" % (i + 1, A.n, err[:60]), flush=True)
            continue
        ch = d["choices"][0]
        m = ch["message"]
        txt = m.get("content") or ""
        tcs = m.get("tool_calls") or []
        finish = ch.get("finish_reason")
        pt = d.get("usage", {}).get("prompt_tokens", 0)
        bad = None
        if not tcs:
            bad = "NOTOOL finish=%s" % finish
        else:
            for tc in tcs:
                try:
                    args = json.loads(tc["function"]["arguments"] or "{}")
                except Exception as e:
                    bad = "PARSE " + repr(e)[:40]
                    break
                if not args:
                    bad = "EMPTY_ARGS name=%s" % tc["function"]["name"]
                    break
        if bad:
            fails.append((i, bad.split()[0], bad))
        else:
            npass += 1
        print("[%d/%d] pt=%d tools=%d finish=%s %s" %
              (i + 1, A.n, pt, len(tcs), finish, bad or "ok"), flush=True)
    print("\nWIDE-TOOLS RESULT: %d/%d pass (%.0fs)" % (npass, A.n, time.time() - t0))
    for i, v, d in fails[:10]:
        print("  req%d: %s %s" % (i, v, d[:90]))
    print("VERDICT:", "ALL GOOD" if not fails else "FAILURES PRESENT")
    return 0 if not fails else 1

sys.exit(main())
