#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DSV4 0014 tool-call hammer: sustained mixed-arm traffic against :5700.

Measures the user's acceptance criterion: tool calls must not fail.
Client-side detection of degenerate signatures (what killed the dead
session) independent of server logs.
"""
import argparse, json, sys, time, urllib.request, urllib.error

AP = argparse.ArgumentParser()
AP.add_argument("--base-url", default="http://localhost:5700")
AP.add_argument("--model", default="dsv4s")
AP.add_argument("-n", type=int, default=200, help="total requests")
AP.add_argument("--max-tokens", type=int, default=2048)
AP.add_argument("--ctx-pad", type=int, default=0, help="repeat filler to lengthen context")
A = AP.parse_args()
BASE = A.base_url.rstrip("/")

SIGS = ("<reference", "<tool_calls", "<tool-call-name", "<dies_cmd_wrapper",
        "<empty-tool-call", "<original_code_end", "<commit_begin",
        "text_placeholder", "<edit-path", "<source>placeholder")

OAI_TOOL = {"type": "function", "function": {"name": "get_weather",
    "description": "Get current weather for a city",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                   "required": ["city"]}}}
ANT_TOOL = {"name": "get_weather", "description": "Get current weather for a city",
    "input_schema": {"type": "object", "properties": {"city": {"type": "string"}},
                     "required": ["city"]}}

def filler():
    return ("You are verifying structured tool-call stability under long context. "
            "Prior context includes driver analysis, scheduler patches and benchmark "
            "data. Continue operating normally and follow the final instruction. ") * 1

def post(path, body, timeout=300):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r, time.time()
    except urllib.error.HTTPError as e:
        return ("http%d" % e.code, time.time())
    except Exception as e:
        return ("conn:" + repr(e)[:80], time.time())

def check_text(txt):
    hits = [s for s in SIGS if s in txt]
    return hits

def oai_arm(tc, think, stream, pad):
    msgs = [{"role": "user", "content": (filler() * pad if pad else "") +
             "Think briefly, then you MUST call the get_weather tool for Beijing."}]
    body = {"model": A.model, "stream": stream, "temperature": 1.0, "top_p": 0.95,
            "max_tokens": A.max_tokens, "tools": [OAI_TOOL], "tool_choice": tc,
            "messages": msgs}
    if think is not None:
        body["chat_template_kwargs"] = {"thinking": think}
    r, t0 = post("/v1/chat/completions", body)
    if isinstance(r, str):
        return "FAIL_NET", r, 0.0
    dt = time.time() - t0
    if stream:
        txt, tc_acc, finish = "", {}, None
        for raw in r:
            line = raw.decode(errors="replace").strip()
            if not line.startswith("data: "):
                continue
            data = line[6:]
            if data == "[DONE]":
                break
            try:
                d = json.loads(data)
                ch = d["choices"][0]
                delta = ch.get("delta", {})
                txt += delta.get("content") or ""
                for tcd in delta.get("tool_calls") or []:
                    slot = tc_acc.setdefault(tcd.get("index", 0),
                                             {"name": "", "arguments": ""})
                    fn = tcd.get("function") or {}
                    slot["name"] += fn.get("name") or ""
                    slot["arguments"] += fn.get("arguments") or ""
                if ch.get("finish_reason"):
                    finish = ch["finish_reason"]
            except Exception:
                pass
        tc_items = list(tc_acc.values())
    else:
        d = json.loads(r.read().decode())
        ch = d["choices"][0]
        finish = ch.get("finish_reason")
        txt = ch["message"].get("content") or ""
        tc_items = [{"name": t["function"].get("name") or "",
                     "arguments": t["function"].get("arguments") or ""}
                    for t in (ch["message"].get("tool_calls") or [])]
    if finish == "error" or finish == "length" and not tc_items:
        pass  # length w/o toolcall handled below
    sigs = check_text(txt)
    if sigs:
        return "FAIL_SIG", ",".join(sigs), dt
    if tc and tc != "auto" and not tc_items:
        return "FAIL_NOTOOL", finish or "?", dt
    if tc_items:
        try:
            args = json.loads(tc_items[0]["arguments"] or "{}")
            if not args.get("city"):
                return "FAIL_ARGS", json.dumps(args)[:60], dt
        except Exception as e:
            return "FAIL_PARSE", repr(e)[:60], dt
    if finish == "error":
        return "FAIL_ERR", finish, dt
    return "PASS", finish or "?", dt

def ant_arm(mode, stream, pad):
    sysp = "Use the get_weather tool for Beijing." + (filler() * pad if pad else "")
    choice = {"type": mode}
    if mode == "tool":
        choice["name"] = ANT_TOOL["name"]
    body = {"model": A.model, "stream": stream, "max_tokens": A.max_tokens,
            "temperature": 1.0, "top_p": 0.95,
            "system": sysp,
            "tools": [ANT_TOOL],
            "tool_choice": choice,
            "messages": [{"role": "user", "content": "Beijing weather now."}]}
    r, t0 = post("/v1/messages", body)
    if isinstance(r, str):
        return "FAIL_NET", r, 0.0
    dt = time.time() - t0
    if stream:
        txt, blocks, stop = "", [], None
        cur_type, input_buf = None, ""
        for raw in r:
            line = raw.decode(errors="replace").strip()
            if not line.startswith("data: "):
                continue
            data = line[6:]
            if data == "[DONE]":
                break
            try:
                d = json.loads(data)
                t = d.get("type")
                if t == "content_block_start":
                    cb = d.get("content_block", {})
                    cur_type = cb.get("type")
                    if cur_type == "tool_use":
                        input_buf = ""
                        blocks.append(dict(cb))
                elif t == "content_block_delta":
                    dl = d.get("delta", {})
                    if dl.get("type") == "text_delta" or "text" in dl:
                        txt += dl.get("text") or ""
                    elif dl.get("type") == "input_json_delta":
                        input_buf += dl.get("partial_json") or ""
                elif t == "content_block_stop":
                    if cur_type == "tool_use" and blocks:
                        try:
                            blocks[-1]["input"] = json.loads(input_buf or "{}")
                        except Exception:
                            blocks[-1]["input"] = {}
                    cur_type = None
                elif t == "message_delta":
                    stop = d.get("delta", {}).get("stop_reason")
            except Exception:
                pass
    else:
        d = json.loads(r.read().decode())
        stop = d.get("stop_reason")
        txt = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
        blocks = [b for b in d.get("content", []) if b.get("type") == "tool_use"]
    sigs = check_text(txt)
    if sigs:
        return "FAIL_SIG", ",".join(sigs), dt
    if mode == "tool" and not blocks:
        return "FAIL_NOTOOL", stop or "?", dt
    if blocks:
        inp = blocks[0].get("input") or {}
        if not (inp.get("city") if isinstance(inp, dict) else inp):
            return "FAIL_ARGS", json.dumps(inp)[:60], dt
    return "PASS", stop or "?", dt

def main():
    arms = [
        ("oai-forced-think", lambda: oai_arm("required", True, False, A.ctx_pad)),
        ("oai-required-think", lambda: oai_arm("required", True, False, A.ctx_pad)),
        ("oai-auto-think", lambda: oai_arm("auto", True, False, A.ctx_pad)),
        ("oai-forced-nothink", lambda: oai_arm("required", False, False, A.ctx_pad)),
        ("oai-forced-stream", lambda: oai_arm("required", True, True, A.ctx_pad)),
        ("ant-tool-think", lambda: ant_arm("tool", False, A.ctx_pad)),
        ("ant-any-think", lambda: ant_arm("any", False, A.ctx_pad)),
        ("ant-tool-stream", lambda: ant_arm("tool", True, A.ctx_pad)),
    ]
    stats = {name: {"PASS": 0, "fails": [], "lat": []} for name, _ in arms}
    t_start = time.time()
    for i in range(A.n):
        name, fn = arms[i % len(arms)]
        verdict, detail, dt = fn()
        st = stats[name]
        if verdict == "PASS":
            st["PASS"] += 1
        else:
            st["fails"].append((i, verdict, detail))
        st["lat"].append(dt)
        if (i + 1) % 25 == 0:
            print(f"[{i+1}/{A.n}] elapsed={time.time()-t_start:.0f}s", flush=True)
    total_pass = total_fail = 0
    print("\n== HAMMER RESULT ==")
    for name, st in stats.items():
        n = len(st["lat"])
        npass = st["PASS"]
        nfail = n - npass
        total_pass += npass; total_fail += nfail
        lat = sorted(st["lat"])
        p50 = lat[len(lat)//2] if lat else 0
        print(f"{name:22s} {npass}/{n} pass  p50={p50:.1f}s")
        for i, v, d in st["fails"][:5]:
            print(f"    req#{i}: {v} {d[:100]}")
        if len(st["fails"]) > 5:
            print(f"    ... +{len(st['fails'])-5} more")
    print(f"\nTOTAL: {total_pass} pass / {total_fail} fail  ({time.time()-t_start:.0f}s)")
    print("VERDICT:", "ALL GOOD" if total_fail == 0 else "FAILURES PRESENT")
    return 0 if total_fail == 0 else 1

sys.exit(main())
