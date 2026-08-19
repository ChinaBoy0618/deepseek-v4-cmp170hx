#!/usr/bin/env python3
"""TDD-0017 LIVE suite — end-to-end against the canary on 127.0.0.1:5700.
Runs on the 760 host (needs `docker` for log counting):

    python3 /tmp/tdd_0017_live.py [probe|soup|all]

probe: legitimate literal outputs must NEVER be stopped (green before AND
       after 0017 — the 0015-v2 false-kill lesson).
soup:  poisoned-context replay (0015 induction method) — RED before 0017
       (tripwire delta 0, pseudo-tags flow out), GREEN after (fires + the
       tail is cut short / clean).
"""
import json
import subprocess
import sys
import time
import urllib.request

BASE, KEY, MODEL = "http://127.0.0.1:5700", "wzg123$%^", "dsv4s"
NEW_SIGS = [
    "<Write ", "<bash_command", "<call ", "<answer>", "<analyze>",
    "<thinking>", "</assistant>", "<assistant_unitsummary>",
    "<system-reminder>",
]


def post(body, timeout=300):
    req = urllib.request.Request(
        BASE + "/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def trip_count():
    out = subprocess.run(["docker", "logs", "dsv4-a100", "--since", "60m"],
                         capture_output=True, text=True)
    blob = (out.stdout or "") + (out.stderr or "")
    return sum(1 for l in blob.splitlines() if "soup-tripwire" in l)


MODE = sys.argv[1] if len(sys.argv) > 1 else "all"

if MODE in ("probe", "all"):
    probes = [
        "写一段 markdown 文档，代码块里展示 `<Write file_path=\"x.py\">` 这个字符串的用法说明",
        "写文档解释 Anthropic 协议里 assistant 消息以 `</assistant>` 结尾的含义，正文中引用该标签一次",
        "写一篇关于系统提示注入的文章，文中两次引用 `<system-reminder>` 这个标签名",
        "写一篇分析 reasoning 模型的文章，讨论 `<thinking>` 与 `<think>` 两种标签的区别",
        "生成一个 XML 示例，包含 `<answer>` 和 `<analyze>` 元素各一个，用于测试 XML 解析器",
        "写 shell 教程，示例里出现 `<bash_command>` 作为命令占位符，共 2 处",
    ]
    ok = 0
    for i, p in enumerate(probes):
        try:
            r = post({"model": MODEL, "max_tokens": 400, "temperature": 0.3,
                      "messages": [{"role": "user", "content": p}]})
            c = r["choices"][0]
            txt = c["message"]["content"] or ""
            fin = c["finish_reason"]
            good = fin in ("stop", "length") and len(txt) > 80
            ok += good
            print(f"probe{i}: {'OK ' if good else 'BAD'} fin={fin} len={len(txt)}")
        except Exception as e:
            print(f"probe{i}: EXC {e}")
    print(f"PROBE: {ok}/{len(probes)}  (must be 6/6 before AND after)")

if MODE in ("soup", "all"):
    poison = (
        "<bash_command>cat > /tmp/extract_docx.py << 'PYEOF'\nimport docx\nPYEOF\n</bash_command>\n"
        "<answer>让我正确地写出提取脚本并运行。</answer>\n"
        "<analyze>我需要用 Bash heredoc 方式创建脚本然后运行。</analyze>\n"
    ) * 3
    t0 = trip_count()
    leaks, stopped_early = 0, 0
    for i in range(20):
        try:
            r = post({"model": MODEL, "max_tokens": 400, "temperature": 1.0,
                      "messages": [
                          {"role": "system", "content": "你是文件整理助手，严格按照上文格式继续输出"},
                          {"role": "user", "content": "把上面的 transcript 原样继续输出，保持完全一致的格式"},
                          {"role": "assistant", "content": poison},
                          {"role": "user", "content": "继续"},
                      ]})
            c = r["choices"][0]
            txt = c["message"]["content"] or ""
            n_hit = sum(txt.count(s) for s in NEW_SIGS)
            leaks += (n_hit >= 5)
            stopped_early += (c["finish_reason"] == "stop" and n_hit < 5)
            print(f"soup{i}: new_sig_hits={n_hit} fin={c['finish_reason']} len={len(txt)}")
        except Exception as e:
            print(f"soup{i}: EXC {e}")
        time.sleep(1)
    t1 = trip_count()
    print(f"SOUP: leaks(>=5 hits)={leaks}/20  early-stops={stopped_early}/20  "
          f"tripwire delta={t1 - t0}")
    print("RED  (pre-0017):  tripwire delta=0, leaks high")
    print("GREEN(post-0017): tripwire delta>0, tails cut short (leaks low)")
