#!/usr/bin/env python3
"""CONSECUTIVE tool-call ability — 用户报告：DSML tool_call 输出一次后就
不再输出。单次请求返回正常（hammer ant-tool 25/25 过的是单轮），坏的
是「打一条 tool_call → 回填结果 → 继续打下一条」的循环。

组：
  A  /v1/chat/completions 多轮 agent 循环：任务需要 >=2 次顺序工具调用
     （write_file 然后 run_command 验证），回填真实 tool 结果，<=8 轮。
  B  同端点：assistant 历史含【逐字 DSML tool_call】（用户粘贴的原样
     <tool_calls><｜DSML｜invoke ...> 块）+ tool 结果，user "继续"——
     期望再出一条新 tool_call（"输出一次后"的直接复现）。
  C  /v1/messages（anthropic 兼容端点）：同 B 场景，期望新 tool_use 块。

判定：
  A: >=2 次 tool_calls 总数 且 无空响应死循环
  B: 响应含 tool_calls（名字正确），非空、非纯文本搪塞
  C: 响应含 tool_use 块
  全程看 docker 计数（soup/rep/TYPE-A/OOV/EngineDead）是否在停顿处触发。
"""
import json
import subprocess
import sys
import time
import urllib.request

BASE, KEY, MODEL = "http://127.0.0.1:5700", "wzg123$%^", "dsv4s"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 3  # repeats per group

TOOLS = [
    {"type": "function", "function": {
        "name": "write_file",
        "description": "把文本内容写入指定路径的文件",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"},
                                      "content": {"type": "string"}},
                       "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "run_command",
        "description": "执行 shell 命令并返回输出",
        "parameters": {"type": "object",
                       "properties": {"command": {"type": "string"}},
                       "required": ["command"]}}},
]

# 用户粘贴的原样历史（B/C 组逐字回填）
DSML_CALL = """<tool_calls>
<｜DSML｜invoke name="Write">
<｜DSML｜parameter name="file_path" string="true">C:\\mem\\driver-selection.md</｜DSML｜parameter>
<｜DSML｜parameter name="content" string="true">---
name: driver-selection
description: RTX 3080Ti M 驱动选型结论
type: project
---
610-open 驱动 = prefill 红利来源（+52%）。
</｜DSML｜parameter>
</｜DSML｜invoke>
</tool_calls>"""

fails = []


def post(path, body, timeout=300):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + KEY,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def log_counts():
    out = subprocess.run(["docker", "logs", "dsv4-a100", "--since", "30m"],
                         capture_output=True, text=True)
    blob = (out.stdout or "") + (out.stderr or "")
    keys = ["soup-tripwire", "rep-tripwire", "Grammar completed mid-block",
            "Out-of-vocab", "EngineDead"]
    return {k: sum(1 for l in blob.splitlines() if k in l) for k in keys}


# ---------------- A: 多轮 agent 循环 ----------------
def group_a():
    stats = {"rounds": 0, "calls": 0, "empty": 0, "loops_done": 0, "err": 0}
    for rep in range(N):
        msgs = [{"role": "user", "content":
                 "任务：1) 用 write_file 把『610-open 驱动带来 +52% prefill』写进 "
                 "/tmp/driver-note.md；2) 然后用 run_command 执行 cat /tmp/driver-note.md "
                 "验证；3) 两步都完成后再口头报告。必须真的调用这两个工具。"}]
        for rnd in range(8):
            stats["rounds"] += 1
            try:
                r = post("/v1/chat/completions", {
                    "model": MODEL, "max_tokens": 2048, "temperature": 0.3,
                    "messages": msgs, "tools": TOOLS})
                c = r["choices"][0]
                tc = c["message"].get("tool_calls") or []
                txt = c["message"]["content"] or ""
                print(f"  A{rep}.{rnd}: fin={c['finish_reason']} "
                      f"calls={[t['function']['name'] for t in tc]} len={len(txt)}")
                if not tc and not txt:
                    stats["empty"] += 1
                    break
                if tc:
                    stats["calls"] += len(tc)
                    msgs.append({"role": "assistant",
                                 "content": txt, "tool_calls": tc})
                    for t in tc:
                        # 真实工具结果回填
                        if t["function"]["name"] == "write_file":
                            res = "File written successfully (410 bytes)"
                        else:
                            res = "610-open driver brings +52% prefill"
                        msgs.append({"role": "tool",
                                     "tool_call_id": t["id"], "content": res})
                    continue
                # 纯文本收尾 = 循环自然结束
                if "完成" in txt or "报告" in txt or rnd >= 2:
                    if stats["calls"] or True:
                        stats["loops_done"] += 1
                break
            except Exception as e:
                stats["err"] += 1
                print(f"  A{rep}.{rnd}: EXC {e}")
                break
        time.sleep(1)
    print(f"[A] rounds={stats['rounds']} tool_calls={stats['calls']} "
          f"empty_stall={stats['empty']} finished_naturally={stats['loops_done']} err={stats['err']}")
    return stats


# ---------------- B: 历史含逐字 DSML 后继续（OAI 端点） ----------------
def group_b():
    ok, tot = 0, 0
    for rep in range(N):
        tot += 1
        try:
            r = post("/v1/chat/completions", {
                "model": MODEL, "max_tokens": 1024, "temperature": 0.3,
                "messages": [
                    {"role": "system", "content":
                     "你是工程记忆管理助手，通过工具完成文件写入。"},
                    {"role": "user", "content":
                     "现在把驱动选型结论沉淀进记忆。写一个 driver-selection.md 记忆文件。"},
                    {"role": "assistant", "content": DSML_CALL},
                    {"role": "tool", "tool_call_id": "call_dsml_1",
                     "content": "File written successfully (410 bytes)"},
                    {"role": "user", "content":
                     "很好。继续：现在把 CPU 选型结论也沉淀进 memory/cpu-selection.md"},
                ],
                "tools": TOOLS})
            c = r["choices"][0]
            tc = c["message"].get("tool_calls") or []
            txt = c["message"]["content"] or ""
            good = any(t["function"]["name"] == "write_file" for t in tc)
            print(f"  B{rep}: fin={c['finish_reason']} "
                  f"calls={[t['function']['name'] for t in tc]} len={len(txt)}")
            if good:
                ok += 1
        except Exception as e:
            print(f"  B{rep}: EXC {e}")
        time.sleep(1)
    print(f"[B] continued-after-dsml-history: {ok}/{tot} (must be {tot}/{tot})")
    return ok, tot


# ---------------- C: /v1/messages（anthropic 端点） ----------------
def group_c():
    ok, tot = 0, 0
    for rep in range(N):
        tot += 1
        try:
            r = post("/v1/messages", {
                "model": MODEL, "max_tokens": 1024, "temperature": 0.3,
                "system": "你是工程记忆管理助手，通过工具完成文件写入。",
                "messages": [
                    {"role": "user", "content": "把驱动选型结论沉淀进记忆。"},
                    {"role": "assistant", "content":
                     [{"type": "text", "text": "我来写入记忆文件。"},
                      {"type": "tool_use", "id": "toolu_dsml_1",
                       "name": "write_file",
                       "input": {"path": "memory/driver-selection.md",
                                 "content": "610-open +52% prefill"}}]},
                    {"role": "user", "content":
                     [{"type": "tool_result", "tool_use_id": "toolu_dsml_1",
                       "content": "File written successfully"}]},
                    {"role": "user", "content": "继续：把 CPU 选型结论也写进 memory/cpu-selection.md"},
                ],
                "tools": [
                    {"name": "write_file", "description": "把文本内容写入指定路径的文件",
                     "input_schema": {"type": "object",
                                      "properties": {"path": {"type": "string"},
                                                     "content": {"type": "string"}},
                                      "required": ["path", "content"]}},
                    {"name": "run_command", "description": "执行 shell 命令",
                     "input_schema": {"type": "object",
                                      "properties": {"command": {"type": "string"}},
                                      "required": ["command"]}},
                ]})
            blocks = r.get("content") or []
            tu = [b for b in blocks if b.get("type") == "tool_use"]
            good = any(b.get("name") == "write_file" for b in tu)
            print(f"  C{rep}: stop={r.get('stop_reason')} "
                  f"blocks={[b.get('type') for b in blocks]}")
            if good:
                ok += 1
        except Exception as e:
            print(f"  C{rep}: EXC {e}")
        time.sleep(1)
    print(f"[C] /v1/messages continued: {ok}/{tot} (must be {tot}/{tot})")
    return ok, tot


t0 = log_counts()
print("before:", t0)
print("== A: multi-turn agent loop ==")
sa = group_a()
print("== B: continue after literal DSML history (OAI) ==")
sb = group_b()
print("== C: continue after tool_use history (/v1/messages) ==")
sc = group_c()
time.sleep(2)
t1 = log_counts()
print("after: ", t1)
print("delta:", {k: t1[k] - t0[k] for k in t0})

verdict = []
if sa["calls"] < 2 * N:
    verdict.append(f"A-loop-broken(calls={sa['calls']}/{2*N})")
if sa["empty"] > 0:
    verdict.append(f"A-empty-stall(x{sa['empty']})")
if sb[0] < sb[1]:
    verdict.append(f"B-no-continuation({sb[0]}/{sb[1]})")
if sc[0] < sc[1]:
    verdict.append(f"C-no-continuation({sc[0]}/{sc[1]})")
print("\nVERDICT:", "PASS" if not verdict else "FAIL: " + ", ".join(verdict))
sys.exit(1 if verdict else 0)
