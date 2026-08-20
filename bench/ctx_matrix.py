#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DSV4 round-1 test: context-size x tool-call matrix.

The dead session died at ~127K context; 0015 validation only covered
80K replay. This matrix walks prompt sizes 4K -> 256K (max-model-len
524288) x 4 arms, sampling at agentic defaults (temp 1.0 / top_p 0.95),
and reports per-size tool-call success + client-side degenerate-signature
detection. Server-side signatures are correlated separately from logs.

Usage: python3 ctx_matrix.py [--base-url http://localhost:5700] [--reps 3]
"""
import argparse, json, sys, time, urllib.request, urllib.error
from collections import defaultdict

AP = argparse.ArgumentParser()
AP.add_argument("--base-url", default="http://localhost:5700")
AP.add_argument("--model", default="dsv4s")
AP.add_argument("--reps", type=int, default=3)
AP.add_argument("--max-tokens", type=int, default=1024)
A = AP.parse_args()
BASE = A.base_url.rstrip("/")

# ~200-token English filler paragraph (measured ~190-210 BPE tokens)
PARA = (
    "Background context for a long-horizon systems investigation. The team "
    "reviewed scheduler internals, speculative decoding acceptance windows, "
    "grammar bitmask application paths, and the interaction between "
    "structured-output managers and per-worker draft model anchors. Several "
    "configuration matrices were run on the four-card pipeline: chunked "
    "prefill behaviour, KV pool sizing, and the impact of context length on "
    "acceptance rates were all recorded. The notes below summarize each "
    "finding with code references so the reader can reconstruct the evidence "
    "chain independently. No action is required from you regarding this "
    "context; it exists solely to occupy prompt tokens. "
)
TOKENS_PER_PARA = 105  # conservative; real count reported by usage

SIGS = ("<reference", "<tool_calls", "<tool-call-name", "<dies_cmd_wrapper",
        "<empty-tool-call", "<original_code_end", "<original_output",
        "<commit_begin", "text_placeholder", "<edit-path", "<source>placeholder")

OAI_TOOL = {"type": "function", "function": {"name": "get_weather",
    "description": "Get current weather for a city",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                   "required": ["city"]}}}
ANT_TOOL = {"name": "get_weather", "description": "Get current weather for a city",
    "input_schema": {"type": "object", "properties": {"city": {"type": "string"}},
                     "required": ["city"]}}

import os
SIZES_K = [int(x) for x in os.environ.get("CTX_SIZES", "4,16,64,128,256").split(",")]
ARMS = ["oai-forced", "oai-auto", "ant-tool", "ant-any"]

def build_filler(target_tokens):
    n = max(0, target_tokens // TOKENS_PER_PARA)
    return PARA * n

def post(path, body, timeout=600):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r, time.time()
    except urllib.error.HTTPError as e:
        return ("http%d" % e.code, time.time())
    except Exception as e:
        return ("conn:" + repr(e)[:70], time.time())

def oai(tc, ctx_tokens):
    msgs = [{"role": "user", "content": build_filler(ctx_tokens) +
             "\n\nFinal instruction: think briefly, then you MUST call the "
             "get_weather tool for Beijing."}]
    body = {"model": A.model, "stream": False, "temperature": 1.0, "top_p": 0.95,
            "max_tokens": A.max_tokens, "tools": [OAI_TOOL], "tool_choice": tc,
            "chat_template_kwargs": {"thinking": True},
            "messages": msgs}
    r, t0 = post("/v1/chat/completions", body)
    if isinstance(r, str):
        return "FAIL_NET", r, 0.0, 0
    dt = time.time() - t0
    d = json.loads(r.read().decode())
    ch = d["choices"][0]
    finish = ch.get("finish_reason")
    txt = ch["message"].get("content") or ""
    tc_items = ch["message"].get("tool_calls") or []
    pt = d.get("usage", {}).get("prompt_tokens", 0)
    if any(s in txt for s in SIGS):
        return "FAIL_SIG", finish or "?", dt, pt
    if tc == "required" and not tc_items:
        return "FAIL_NOTOOL", finish or "?", dt, pt
    if tc_items:
        try:
            args = json.loads(tc_items[0]["function"]["arguments"] or "{}")
            if not args.get("city"):
                return "FAIL_ARGS", json.dumps(args)[:50], dt, pt
        except Exception as e:
            return "FAIL_PARSE", repr(e)[:50], dt, pt
    if finish == "error":
        return "FAIL_ERR", finish, dt, pt
    return "PASS", finish or "?", dt, pt

def ant(mode, ctx_tokens):
    choice = {"type": mode}
    if mode == "tool":
        choice["name"] = ANT_TOOL["name"]
    body = {"model": A.model, "stream": False, "max_tokens": A.max_tokens,
            "temperature": 1.0, "top_p": 0.95,
            "system": build_filler(ctx_tokens) + "\n\nUse the get_weather tool.",
            "tools": [ANT_TOOL], "tool_choice": choice,
            "messages": [{"role": "user", "content": "Beijing weather now."}]}
    r, t0 = post("/v1/messages", body)
    if isinstance(r, str):
        return "FAIL_NET", r, 0.0, 0
    dt = time.time() - t0
    d = json.loads(r.read().decode())
    stop = d.get("stop_reason")
    txt = "".join(b.get("text", "") for b in d.get("content", [])
                  if b.get("type") == "text")
    blocks = [b for b in d.get("content", []) if b.get("type") == "tool_use"]
    pt = d.get("usage", {}).get("input_tokens", 0)
    if any(s in txt for s in SIGS):
        return "FAIL_SIG", stop or "?", dt, pt
    if mode == "tool" and not blocks:
        return "FAIL_NOTOOL", stop or "?", dt, pt
    if blocks:
        inp = blocks[0].get("input") or {}
        if not (inp.get("city") if isinstance(inp, dict) else inp):
            return "FAIL_ARGS", json.dumps(inp)[:50], dt, pt
    return "PASS", stop or "?", dt, pt

def main():
    stats = defaultdict(lambda: {"pass": 0, "n": 0, "fails": [], "lat": [], "pt": 0})
    t0 = time.time()
    i = 0
    total = len(SIZES_K) * len(ARMS) * A.reps
    for size_k in SIZES_K:
        for rep in range(A.reps):
            for arm in ARMS:
                i += 1
                ctx = size_k * 1024
                if arm.startswith("oai"):
                    tc = "required" if arm == "oai-forced" else "auto"
                    v, det, dt, pt = oai(tc, ctx)
                else:
                    v, det, dt, pt = ant("tool" if arm == "ant-tool" else "any", ctx)
                st = stats[(size_k, arm)]
                st["n"] += 1
                st["pt"] = pt
                if v == "PASS":
                    st["pass"] += 1
                else:
                    st["fails"].append((rep, v, det))
                st["lat"].append(dt)
                print("[%d/%d] %4dK %-11s %s %s (%.0fs)" %
                      (i, total, size_k, arm, v, det[:40], dt), flush=True)
    print("\n== CTX MATRIX RESULT ==")
    tp = tf = 0
    for (size_k, arm), st in sorted(stats.items()):
        np_, nf = st["pass"], st["n"] - st["pass"]
        tp += np_; tf += nf
        lat = sorted(st["lat"])
        p50 = lat[len(lat)//2] if lat else 0
        print("%4dK %-11s %d/%d pass  p50=%4.0fs  actual_prompt=%d" %
              (size_k, arm, np_, st["n"], p50, st["pt"]))
        for rep, v, det in st["fails"][:3]:
            print("    rep%d: %s %s" % (rep, v, det[:80]))
    print("\nTOTAL: %d pass / %d fail (%.0fs)" % (tp, tf, time.time()-t0))
    print("VERDICT:", "ALL GOOD" if tf == 0 else "FAILURES PRESENT")
    return 0 if tf == 0 else 1

sys.exit(main())
