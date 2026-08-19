#!/usr/bin/env python3
"""TDD-0021: normalize raw-DSML tool calls in assistant history.

Live finding (user report "tool_call 输出一次后就不再输出"): when an
assistant message carries its tool call as RAW TEXT in `content`
(client-echoed `<tool_calls>`+DSML or canonical `<｜DSML｜tool_calls>`),
render_message emits it as plain text; the model then often answers the
next turn in text instead of calling again (B 1/3, E 2/3). Structured
`tool_calls` history renders canonically and continues reliably (C 3/3,
D 6/6).

Fix contract: encode_messages preprocessing extracts COMPLETE DSML
tool-call blocks from assistant content into structured tool_calls, so
rendering is canonical regardless of what the client echoes back.

RED on current stack: U01, U02, U03, U06, U07, U09.
"""
import copy
import json
import sys

sys.path.insert(0, "/vllm")
from vllm.tokenizers.deepseek_v4_encoding import encode_messages  # noqa: E402

TOOLS = [{"type": "function", "function": {
    "name": "write_file",
    "description": "写文件",
    "parameters": {"type": "object",
                   "properties": {"path": {"type": "string"},
                                  "content": {"type": "string"}},
                   "required": ["path", "content"]}}}]

# 用户粘贴的客户端回显形态（非 DSML wrapper + DSML invoke）
CLIENT_ECHO = """<tool_calls>
<｜DSML｜invoke name="write_file">
<｜DSML｜parameter name="path" string="true">/tmp/driver-note.md</｜DSML｜parameter>
<｜DSML｜parameter name="content" string="true">610-open 驱动 = prefill 红利来源（+52%）。</｜DSML｜parameter>
</｜DSML｜invoke>
</tool_calls>"""

# 模型原始输出回显形态（规范 DSML wrapper）
CANONICAL = CLIENT_ECHO.replace("<tool_calls>", "<｜DSML｜tool_calls>") \
                       .replace("</tool_calls>", "</｜DSML｜tool_calls>")

# 宽松变体（token 边界劣化形态）
LENIENT = CANONICAL.replace("｜DSML｜tool_calls>", "｜DSML｜_tool_calls>")

# string=false 的 JSON 参数
JSON_ARGS = """<｜DSML｜tool_calls>
<｜DSML｜invoke name="run_command">
<｜DSML｜parameter name="retries" string="false">3</｜DSML｜parameter>
<｜DSML｜parameter name="verbose" string="false">true</｜DSML｜parameter>
</｜DSML｜invoke>
</｜DSML｜tool_calls>"""

fails = []


def chk(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ((" | " + detail) if not cond else ""))
    if not cond:
        fails.append(name)


def render(asst_content, asst_tool_calls=None, user2="继续写第二个"):
    msgs = [
        {"role": "user", "content": "把驱动选型结论写进记忆", "tools": TOOLS},
        {"role": "assistant", "content": asst_content,
         **({"tool_calls": asst_tool_calls} if asst_tool_calls else {})},
        {"role": "tool", "tool_call_id": "c1", "content": "written ok"},
        {"role": "user", "content": user2},
    ]
    return encode_messages(msgs, thinking_mode="thinking")


def canonical_bits(p):
    return ("<｜DSML｜tool_calls>" in p
            and '<｜DSML｜invoke name="write_file">' in p
            and '<｜DSML｜parameter name="path" string="true">' in p)


# ---------- U01: 客户端回显形态 → 规范渲染 [RED] ----------
p = render(CLIENT_ECHO)
chk("U01 client-echo renders canonical DSML [RED]", canonical_bits(p),
    "raw block still literal text in prompt")

# ---------- U02: 规范 wrapper 回显 → 与结构化对照逐字节相等 [RED] ----------
def control_for(asst_content_echo_args, tc_id="c1"):
    return render("", asst_tool_calls=[{
        "id": tc_id, "type": "function",
        "function": {"name": "write_file", "arguments": asst_content_echo_args}}])


p = render(CANONICAL)
p_ctl2 = control_for(json.dumps(
    {"path": "/tmp/driver-note.md", "content": "610-open 驱动 = prefill 红利来源（+52%）。"},
    ensure_ascii=False))
chk("U02 canonical echo == structured control byte-for-byte [RED]",
    p == p_ctl2)

# ---------- U03: 宽容 wrapper → 规范渲染 [RED] ----------
p = render(LENIENT)
chk("U03 lenient wrapper renders canonical DSML [RED]", canonical_bits(p))

# ---------- U04: 结构化 tool_calls 对照（改前改后都必须绿） ----------
p_ctl = render("", asst_tool_calls=[{
    "id": "c1", "type": "function",
    "function": {"name": "write_file",
                 "arguments": json.dumps({"path": "/tmp/x.md",
                                          "content": "y"})}}])
chk("U04 structured control canonical", canonical_bits(p_ctl))

# ---------- U05: 纯提及（不平衡块）不动 [guard] ----------
p_mention = render("文档说你可以用 <｜DSML｜invoke 标签发起工具调用。")
chk("U05 prose mention stays text",
    "<｜DSML｜tool_calls>" not in p_mention
    and "文档说" in p_mention)

# ---------- U06: 块前后有正文 → 残留正文保留为 content [RED] ----------
p_mix = render("我先把驱动结论写下来。\n\n" + CLIENT_ECHO + "\n\n写好了。")
chk("U06 surrounding text kept as content [RED]",
    canonical_bits(p_mix) and "我先把驱动结论写下来" in p_mix
    and "<tool_calls>" not in p_mix)

# ---------- U07: string=false → JSON 参数还原成 dict，与对照相等 [RED] ----------
p_js = render(JSON_ARGS, user2="再跑一次")
p_js_ctl = render("", asst_tool_calls=[{
    "id": "c1", "type": "function",
    "function": {"name": "run_command",
                 "arguments": json.dumps({"retries": 3, "verbose": True})}}],
    user2="再跑一次")
chk("U07 string=false parses JSON == control [RED]", p_js == p_js_ctl)

# ---------- U08: 无工具对话不受影响 [guard] ----------
p_plain = encode_messages([
    {"role": "user", "content": "你好"},
    {"role": "assistant", "content": "你好！有什么可以帮你？"},
    {"role": "user", "content": "没事"},
], thinking_mode="chat")
chk("U08 no-tools conversation untouched",
    "你好！有什么可以帮你？" in p_plain)

# ---------- U09: 一块多 invoke → 依序两条 tool_calls [RED] ----------
TWO = """<tool_calls>
<｜DSML｜invoke name="write_file">
<｜DSML｜parameter name="path" string="true">/tmp/a.md</｜DSML｜parameter>
<｜DSML｜parameter name="content" string="true">A</｜DSML｜parameter>
</｜DSML｜invoke>
<｜DSML｜invoke name="write_file">
<｜DSML｜parameter name="path" string="true">/tmp/b.md</｜DSML｜parameter>
<｜DSML｜parameter name="content" string="true">B</｜DSML｜parameter>
</｜DSML｜invoke>
</tool_calls>"""
p_two = render(TWO)
p_two_ctl = render("", asst_tool_calls=[
    {"id": "c1", "type": "function",
     "function": {"name": "write_file",
                  "arguments": json.dumps({"path": "/tmp/a.md", "content": "A"})}},
    {"id": "c2", "type": "function",
     "function": {"name": "write_file",
                  "arguments": json.dumps({"path": "/tmp/b.md", "content": "B"})}},
])
chk("U09 two invokes == two-call control byte-for-byte [RED]", p_two == p_two_ctl)

# ---------- U10: 调用方消息列表不被原地污染 ----------
orig = [
    {"role": "user", "content": "把结论写进记忆", "tools": TOOLS},
    {"role": "assistant", "content": CLIENT_ECHO},
    {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    {"role": "user", "content": "继续"},
]
snap = copy.deepcopy(orig)
encode_messages(orig, thinking_mode="thinking")
chk("U10 caller list not mutated", orig == snap)

print("\n" + ("ALL GREEN" if not fails else "RED: %d fail(s): %s" % (len(fails), fails)))
sys.exit(1 if fails else 0)
