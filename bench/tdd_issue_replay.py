#!/usr/bin/env python3
"""ISSUE REPLAY — 用 issues/01、issues/02 的原始问题上下文（逐字摘录）
重放，验证 0017v2+0018+0019 后还会不会出问题。

    python3 /tmp/tdd_issue_replay.py          # 全部四组
    python3 /tmp/tdd_issue_replay.py i1       # 只跑 issue-1 重放

组：
  i1   issue-1 原文毒上下文（Write/bash_command/answer/analyze/Bash）纯文本续写
  i1t  同上但挂真实工具 schema（走 grammar/DSML/salvage 链路）
  i2   issue-2 原文（Lets reload 循环 + nem</assistant> + assistant_unitsummary）
  stat 日志侧计数：soup-tripwire / rep-tripwire / clean-cut / salvage-cap / TYPE-B / 612

判定（用户问题"会不会出问题"）：
  - 全程 HTTP 200、零崩溃、零 traceback（对照 issue-1 的硬停）
  - <thinking> 字面量在 content 出现次数 = 0（0018 剥离）
  - 重毒响应要么被截停（finish=stop 且尾巴短）要么泄漏量远小于 RED 基准
  - 工具调用可正常解析（i1t 组）
"""
import json
import subprocess
import sys
import time
import urllib.request

BASE, KEY, MODEL = "http://127.0.0.1:5700", "wzg123$%^", "dsv4s"
N = 12

I1_POISON = """<Write file_path="/tmp/extract_docx.py">
<content>

<analyze>Let me emit a real Write tool call with the actual content of the extractor script.</analyze>

I'll write the docx text extractor script now.
</content>
</Write>

<bash_command>
cat > /tmp/extract_docx.py << 'PYEOF'
import docx
PYEOF
python3 /tmp/extract_docx.py "02_分类归档/规章制度/4.培训规程_培训服务规范书_V1.0.docx" 2>&1 | head -80
</bash_command>

<bash_command>python3 /tmp/extract_docx.py "..." 2>&1 | head -80</bash_command>

<answer>让我正确地写出提取脚本并运行。</answer>"""

I1_TAIL = """<Bash command>
cd "/Users/zhanguowang/Documents/数智邮务/我的输出/知识库蒸馏" && cat > /tmp/extract_docx.py << 'PYEOF'
PYEOF
python3 /tmp/extract_docx.py "02_分类归档/规章制度/4.培训规程_培训服务规范书_V1.0.docx" 2>&1 | head -60
</Bash>"""

I2_POISON = """vue-tsc passes (no output, clean exit). The fix is just a template binding name, and code compiles. Let me reload the storyboard and retest the dialog.

Lets reload.

Let me reload the page.

Let me reload. The dialog state may have a beforeunload. Let me handle it.

Let me reload.

Lets navigate and handle the dialog.

Let me reload the storyboard.

Lets reload.

Lets reload.

Let me do the flow.nem</assistant> I'll execute the flow in one code snippet.

<assistant_unitsummary>Running full F4 flow test</assistant_unitsummary>"""

TOOLS = [
    {"type": "function", "function": {
        "name": "run_command",
        "description": "Run a shell command",
        "parameters": {"type": "object",
                       "properties": {"command": {"type": "string"}},
                       "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Write a file",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"},
                                      "content": {"type": "string"}},
                       "required": ["path", "content"]}}},
]

SIGS = ["<Write ", "<bash_command", "<call ", "<answer>", "<analyze>",
        "<thinking>", "</assistant>", "<assistant_unitsummary>",
        "<system-reminder>", "<Bash command", "</Bash>"]


def post(body, timeout=300):
    req = urllib.request.Request(
        BASE + "/v1/chat/completions", data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + KEY,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def log_counts():
    out = subprocess.run(["docker", "logs", "dsv4-a100", "--since", "90m"],
                         capture_output=True, text=True)
    blob = (out.stdout or "") + (out.stderr or "")
    keys = ["soup-tripwire", "rep-tripwire", "clean-cut", "salvage-cap",
            "TYPE-B", "grammar_matcher.cc:612", "Traceback", "EngineDead"]
    return {k: sum(1 for l in blob.splitlines() if k in l) for k in keys}


def dup_max(txt):
    lines = [l.strip() for l in txt.splitlines() if l.strip()]
    return max((lines.count(l) for l in set(lines)), default=0)


def run_group(name, messages_fn, tools=None, n=N):
    stats = {"sig_hits": 0, "think_leak": 0, "dup5": 0, "stopped": 0, "err": 0}
    for i in range(n):
        body = {"model": MODEL, "max_tokens": 400, "temperature": 1.0,
                "messages": messages_fn(i)}
        if tools:
            body["tools"] = tools
        try:
            r = post(body)
            c = r["choices"][0]
            txt = c["message"]["content"] or ""
            hits = sum(txt.count(s) for s in SIGS)
            stats["sig_hits"] += hits
            stats["think_leak"] += ("<thinking>" in txt) + ("</thinking>" in txt)
            stats["dup5"] += (dup_max(txt) >= 5)
            stats["stopped"] += (c["finish_reason"] == "stop")
            tc = c["message"].get("tool_calls") or []
            print(f"{name}{i}: sig_hits={hits} dup={dup_max(txt)} fin={c['finish_reason']} len={len(txt)} tools={len(tc)}")
        except Exception as e:
            stats["err"] += 1
            print(f"{name}{i}: EXC {e}")
        time.sleep(1)
    print(f"[{name}] totals: {stats}")
    return stats


MODE = sys.argv[1] if len(sys.argv) > 1 else "all"
t0 = log_counts()
print("before:", t0)

if MODE in ("i1", "all"):
    run_group("i1", lambda i: [
        {"role": "system", "content": "你是数智邮务文件整理助手，继续按上文一模一样的格式输出"},
        {"role": "user", "content": "继续输出上面的 transcript，保持完全一致的格式"},
        {"role": "assistant", "content": I1_POISON + "\n\n" + I1_TAIL},
        {"role": "user", "content": "继续"},
    ])

if MODE in ("i1t", "all"):
    run_group("i1t", lambda i: [
        {"role": "system", "content": "你有 run_command 和 write_file 工具。上文的 bash_command/Write 意图请直接用工具完成"},
        {"role": "user", "content": "用工具把上面的 extract_docx.py 写出来并运行"},
        {"role": "assistant", "content": I1_POISON},
        {"role": "user", "content": "继续，用工具"},
    ], tools=TOOLS)

if MODE in ("i2", "all"):
    run_group("i2", lambda i: [
        {"role": "system", "content": "你是浏览器测试助手，按上文节奏继续重载页面"},
        {"role": "user", "content": "继续按上面格式输出"},
        {"role": "assistant", "content": I2_POISON},
        {"role": "user", "content": "继续"},
    ])

time.sleep(2)
t1 = log_counts()
print("after: ", t1)
print("delta:", {k: t1[k] - t0[k] for k in t1})
