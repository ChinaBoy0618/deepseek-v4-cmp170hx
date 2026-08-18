#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DSV4 round-2 test: multi-turn agentic tool loop, the dead-session shape.

The 93fc20ce session died mid tool-loop at ~127K context. This harness
runs N agentic turns against /v1/chat/completions with a real-ish tool
set (Bash + Read + Write), feeding synthetic tool results back each
turn, growing the context past 128K, at agentic sampling (temp 1.0 /
top_p 0.95). Every turn must yield either a parseable tool call with
valid arguments or clean text+stop; any degenerate-signature content,
unparseable call, or HTTP error counts as a failure of the user's
criterion.

Usage: python3 agent_loop.py [--turns 16] [--seed-k 2]
"""
import argparse, json, random, sys, time, urllib.request, urllib.error

AP = argparse.ArgumentParser()
AP.add_argument("--base-url", default="http://localhost:5700")
AP.add_argument("--model", default="dsv4s")
AP.add_argument("--turns", type=int, default=16)
AP.add_argument("--seed-k", type=int, default=2, help="filler docs per turn")
AP.add_argument("--max-tokens", type=int, default=2048)
A = AP.parse_args()
BASE = A.base_url.rstrip("/")

SIGS = ("<reference", "<tool_calls", "<tool-call-name", "<dies_cmd_wrapper",
        "<empty-tool-call", "<original_code_end", "<original_output",
        "<commit_begin", "text_placeholder", "<edit-path", "<source>placeholder")

TOOLS = [
    {"type": "function", "function": {"name": "Bash",
        "description": "Execute a shell command and return its output",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "The command to run"},
            "description": {"type": "string"}},
            "required": ["command"]}}},
    {"type": "function", "function": {"name": "Read",
        "description": "Read a file from the filesystem",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "Write",
        "description": "Write content to a file",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"]}}},
]

def doc(seed):
    rnd = random.Random(seed)
    lines = []
    for i in range(60):
        pkg = rnd.choice(["scheduler", "grammar", "drafter", "kv-cache"])
        mod = rnd.choice(["asyncio", "queue", "bitmask", "allocator"])
        lines.append(
            "Note %d: the %s/%s interaction was reviewed again; acceptance "
            "window %d kept its invariants under the new rollback order. "
            "Measured acceptance %0.2f, drafted %d, rejected %d." % (
                i, pkg, mod, i, rnd.uniform(0.6, 0.95), rnd.randint(3, 6),
                rnd.randint(0, 2)))
    return "\n".join(lines)

def fake_result(call):
    name = call["function"]["name"]
    try:
        args = json.loads(call["function"]["arguments"] or "{}")
    except Exception:
        return '{"error": "unparseable arguments"}'
    if name == "Bash":
        cmd = str(args.get("command", ""))[:80]
        return json.dumps({"stdout": "OK: " + cmd + "\nexit 0", "stderr": "",
                           "exit": 0})
    if name == "Read":
        return json.dumps({"content": "file body line 1\nline 2\nline 3"})
    if name == "Write":
        return json.dumps({"bytes_written": len(str(args.get("content", "")))})
    return "{}"

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
    msgs = [{"role": "user", "content":
        "You are a site-reliability agent. Work through the diagnostic "
        "checklist step by step: at each step use the Bash tool to run one "
        "check, read the result, then proceed. Keep going until checklist "
        "completion, then summarize. Checklist: 1) uptime 2) disk 3) memory "
        "4) gpu 5) network. Between checks, these are previously collected "
        "investigation notes:\n" + doc(1) + "\n" + doc(2)}]
    fail = 0
    t0 = time.time()
    for turn in range(1, A.turns + 1):
        msgs.append({"role": "user", "content":
            "Step %d. Continue the checklist. Also weigh these new notes "
            "from the log archive:\n%s" % (turn, doc(100 + turn) * A.seed_k)})
        body = {"model": A.model, "messages": msgs, "tools": TOOLS,
                "tool_choice": "auto", "temperature": 1.0, "top_p": 0.95,
                "max_tokens": A.max_tokens,
                "chat_template_kwargs": {"thinking": True}}
        d, err = post(body)
        if err:
            print("turn %2d FAIL_NET %s" % (turn, err), flush=True)
            fail += 1
            break
        ch = d["choices"][0]
        m = ch["message"]
        txt = m.get("content") or ""
        tcs = m.get("tool_calls") or []
        finish = ch.get("finish_reason")
        pt = d.get("usage", {}).get("prompt_tokens", 0)
        sig = [s for s in SIGS if s in txt]
        bad = None
        if sig:
            bad = "SIG:" + ",".join(sig[:3])
        elif finish == "length" and not tcs:
            bad = "LENGTH_BURN(no tool call)"
        elif tcs:
            for tc in tcs:
                try:
                    args = json.loads(tc["function"]["arguments"] or "{}")
                except Exception as e:
                    bad = "ARG_PARSE:" + repr(e)[:40]
                    break
                if not args:
                    bad = "ARG_EMPTY"
                    break
        print("turn %2d pt=%6d finish=%-11s tools=%d %s%s" %
              (turn, pt, finish, len(tcs), bad or "ok",
               (" " + bad) if bad else ""), flush=True)
        if bad:
            fail += 1
            print("  content head:", repr(txt[:300]))
            break
        # feed tool results back and continue the loop
        msgs.append({"role": "assistant", "content": txt or None,
                     "tool_calls": tcs or None})
        for i, tc in enumerate(tcs or []):
            msgs.append({"role": "tool", "tool_call_id": tc["id"],
                         "content": fake_result(tc)})
        if not tcs:
            # model chose plain text this turn; nudge once, then accept
            print("  (no tool call this turn; plain answer)")
    print("\nAGENT-LOOP RESULT: %d turns attempted, %s (%.0fs)" %
          (turn, "FAIL: " + bad if fail else "ALL TURNS CLEAN",
           time.time() - t0))
    return 1 if fail else 0

sys.exit(main())
