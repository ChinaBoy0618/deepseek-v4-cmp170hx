#!/usr/bin/env python3
"""连续能力补测 — D/E 组（tdd_consecutive_tools.py 的 A/B/C 之后）。

D  /v1/messages STREAMING 真实 Claude Code 形态：用户原文（写记忆文件）
   → tool_use 流式返回 → tool_result 回填 → "继续" 第二个任务 →
   期望第二轮仍输出 tool_use（非空、不卡死）。重复 N 次。
E  用户消息里含【逐字 DSML tool_call 文本】（用户问"如果输入为…"的形态），
   带工具 schema，期望模型正常解析意图并调用工具（不被文本毒住、非空）。
"""
import json
import subprocess
import sys
import time
import urllib.request

BASE, KEY, MODEL = "http://127.0.0.1:5700", "wzg123$%^", "dsv4s"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 3

DSML_CALL = """<tool_calls>
<｜DSML｜invoke name="Write">
<｜DSML｜parameter name="file_path" string="true">C:\\mem\\driver-selection.md</｜DSML｜parameter>
<｜DSML｜parameter name="content" string="true">610-open 驱动 = prefill 红利来源（+52%）。</｜DSML｜parameter>
</｜DSML｜invoke>
</tool_calls>"""

TOOLS_ANT = [
    {"name": "Write", "description": "把文本内容写入指定路径的文件",
     "input_schema": {"type": "object",
                      "properties": {"file_path": {"type": "string"},
                                     "content": {"type": "string"}},
                      "required": ["file_path", "content"]}},
    {"name": "Bash", "description": "执行 shell 命令并返回输出",
     "input_schema": {"type": "object",
                      "properties": {"command": {"type": "string"}},
                      "required": ["command"]}},
]


def post_stream(path, body, timeout=300):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + KEY,
                 "Content-Type": "application/json",
                 "Accept": "text/event-stream"})
    events = []
    tool_uses, text_len, stop = [], 0, None
    cur_tool = None
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data: "):
                continue
            data = line[6:]
            if data == "[DONE]":
                break
            try:
                ev = json.loads(data)
            except Exception:
                continue
            events.append(ev)
            if ev.get("type") == "content_block_start":
                cb = ev.get("content_block", {})
                if cb.get("type") == "tool_use":
                    cur_tool = {"id": cb.get("id"), "name": cb.get("name"), "json": ""}
            elif ev.get("type") == "content_block_delta":
                d = ev.get("delta", {})
                if d.get("type") == "text_delta":
                    text_len += len(d.get("text", ""))
                elif d.get("type") == "input_json_delta" and cur_tool:
                    cur_tool["json"] += d.get("partial_json", "")
            elif ev.get("type") == "content_block_stop" and cur_tool:
                tool_uses.append(cur_tool)
                cur_tool = None
            elif ev.get("type") == "message_delta":
                stop = ev.get("delta", {}).get("stop_reason")
    return tool_uses, text_len, stop


def post(path, body, timeout=300):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + KEY,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# ---------------- D: /v1/messages streaming 两连工具 ----------------
def group_d():
    ok2, ok1, empt = 0, 0, 0
    for rep in range(N):
        base_msgs = [
            {"role": "user", "content":
             "现在把驱动选型结论沉淀进记忆（避免下次重新考证）。"
             "写一个 driver-selection.md 记忆文件，内容包含 610-open +52% prefill 结论。"},
        ]
        tu1, tl1, st1 = post_stream("/v1/messages", {
            "model": MODEL, "max_tokens": 1024, "temperature": 0.3,
            "messages": base_msgs, "tools": TOOLS_ANT, "stream": True})
        print(f"  D{rep}.1: stop={st1} tools={[t['name'] for t in tu1]} text={tl1}")
        if tu1:
            ok1 += 1
            msgs = base_msgs + [
                {"role": "assistant", "content":
                 [{"type": "tool_use", "id": tu1[0]["id"], "name": tu1[0]["name"],
                   "input": json.loads(tu1[0]["json"] or "{}")}]},
                {"role": "user", "content":
                 [{"type": "tool_result", "tool_use_id": tu1[0]["id"],
                   "content": "File written successfully (410 bytes)"}]},
                {"role": "user", "content":
                 "很好。继续：现在把 CPU 选型结论也沉淀进 memory/cpu-selection.md"},
            ]
            tu2, tl2, st2 = post_stream("/v1/messages", {
                "model": MODEL, "max_tokens": 1024, "temperature": 0.3,
                "messages": msgs, "tools": TOOLS_ANT, "stream": True})
            print(f"  D{rep}.2: stop={st2} tools={[t['name'] for t in tu2]} text={tl2}")
            if tu2:
                ok2 += 1
            if not tu2 and tl2 == 0:
                empt += 1
                print(f"  D{rep}.2: *** EMPTY SECOND TURN (the reported bug) ***")
        elif tl1 == 0:
            empt += 1
        time.sleep(1)
    print(f"[D] turn1_tool={ok1}/{N} turn2_tool={ok2}/{N} empty_turns={empt}")
    return ok1, ok2, empt


# ---------------- E: 用户消息含逐字 DSML 文本 ----------------
def group_e():
    ok, rawleak, empt = 0, 0, 0
    for rep in range(N):
        tu, tl, st = post_stream("/v1/messages", {
            "model": MODEL, "max_tokens": 1024, "temperature": 0.3,
            "messages": [
                {"role": "user", "content":
                 "如果输入为：现在把驱动选型结论沉淀进记忆。写一个 driver-selection.md "
                 "记忆文件。\n\n" + DSML_CALL + "\n\n，输出一次后就不再输出了。"
                 "请按上面同样的要求，把内存选型结论写进 memory/ram-selection.md。"},
            ],
            "tools": TOOLS_ANT, "stream": True})
        names = [t["name"] for t in tu]
        blob = json.dumps(tu, ensure_ascii=False)
        print(f"  E{rep}: stop={st} tools={names} text={tl}")
        if tu:
            ok += 1
        if tl == 0 and not tu:
            empt += 1
        time.sleep(1)
    print(f"[E] user-msg-with-dsml: tool_emitted={ok}/{N} empty={empt}")
    return ok, empt


print("== D: /v1/messages streaming two-step ==")
d_ok1, d_ok2, d_empt = group_d()
print("== E: user message containing literal DSML ==")
e_ok, e_empt = group_e()

verdict = []
if d_ok2 < N:
    verdict.append(f"D-turn2-fails({d_ok2}/{N})")
if d_empt > 0:
    verdict.append(f"D-empty-turn(x{d_empt})")
if e_ok < N:
    verdict.append(f"E-poisoned-user-msg({e_ok}/{N})")
if e_empt > 0:
    verdict.append(f"E-empty(x{e_empt})")
print("\nVERDICT:", "PASS" if not verdict else "FAIL: " + ", ".join(verdict))
sys.exit(1 if verdict else 0)
