#!/usr/bin/env python3
"""DSV4 0021: normalize raw-DSML tool-call blocks echoed in assistant history.

Live finding (user report "tool_call 输出一次后就不再输出"): a client that
echoes the assistant tool call back as RAW TEXT (client `<tool_calls>`
wrapper, canonical or lenient DSML wrapper) gets it rendered as plain
text; the model then often answers the next turn in text instead of
calling again (B 1/3, E 2/3 live). Structured tool_calls render
canonically and continue reliably. Extract COMPLETE blocks into
structured tool_calls during encode_messages preprocessing; unbalanced
prose mentions stay untouched."""
import py_compile
import shutil

P = "/mnt/nvme1/dsv4/vllm-c3046d1/vllm/tokenizers/deepseek_v4_encoding.py"
src = open(P, encoding="utf-8").read()
shutil.copy(P, P + ".bak-0021")


def rep(old, new, cnt=1):
    global src
    assert src.count(old) == cnt, ("anchor broken", old[:70], src.count(old))
    src = src.replace(old, new)


# 1. extraction helpers before merge_tool_messages
rep('''def merge_tool_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:''',
    '''# ============================================================
# DSV4 0021: raw-DSML history normalization
# ============================================================
import re as _re_0021

_DSML_0021_BLOCK_RE = _re_0021.compile(
    r"<(?:tool_calls>|｜DSML｜_?tool_calls>)\\s*(.*?)</(?:tool_calls>|｜DSML｜_?tool_calls>)",
    _re_0021.S)
_DSML_0021_INVOKE_RE = _re_0021.compile(
    r'<｜DSML｜invoke name="([^"]+)">\\s*(.*?)</｜DSML｜invoke>', _re_0021.S)
_DSML_0021_PARAM_RE = _re_0021.compile(
    r'<｜DSML｜parameter name="([^"]+)"(?: string="(true|false)")?>(.*?)</｜DSML｜parameter>',
    _re_0021.S)


def extract_dsml_tool_calls(content):
    """(leftover_text, tool_calls) when `content` carries a COMPLETE
    raw DSML tool-call block; None when it does not (or the block is
    unbalanced prose that merely mentions the tags)."""
    if not isinstance(content, str) or "<｜DSML｜invoke" not in content:
        return None
    calls = []
    for m in _DSML_0021_BLOCK_RE.finditer(content):
        block = m.group(1)
        invokes = _DSML_0021_INVOKE_RE.findall(block)
        if not invokes or len(invokes) != block.count("<｜DSML｜invoke"):
            return None
        for name, argblob in invokes:
            params = _DSML_0021_PARAM_RE.findall(argblob)
            if len(params) != argblob.count("<｜DSML｜parameter"):
                return None
            args = {}
            for key, is_str, val in params:
                try:
                    args[key] = val if is_str != "false" else json.loads(val)
                except Exception:
                    return None
            calls.append({
                "id": f"dsv4-dsml-{len(calls)}",
                "type": "function",
                "function": {"name": name,
                             "arguments": json.dumps(args, ensure_ascii=False)},
            })
    if not calls:
        return None
    leftover = _DSML_0021_BLOCK_RE.sub("", content).strip()
    return leftover, calls


def _normalize_dsml_history_messages(messages):
    """Assistant messages with a raw DSML block in content and no
    structured tool_calls get the block lifted into tool_calls so the
    canonical template renders it (0021)."""
    out = []
    for msg in messages:
        if msg.get("role") == "assistant" and not msg.get("tool_calls"):
            ext = extract_dsml_tool_calls(msg.get("content"))
            if ext is not None:
                leftover, calls = ext
                msg = dict(msg)
                msg["content"] = leftover
                msg["tool_calls"] = calls
        out.append(msg)
    return out


def merge_tool_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:''')

# 2. hook in encode_messages: messages branch
rep('''    messages = merge_tool_messages(messages)
    messages = sort_tool_results_by_call_order(context + messages)[len(context):]''',
    '''    messages = merge_tool_messages(messages)
    messages = _normalize_dsml_history_messages(messages)
    messages = sort_tool_results_by_call_order(context + messages)[len(context):]''')

# 3. hook in encode_messages: context branch
rep('''        context = merge_tool_messages(context)
        context = sort_tool_results_by_call_order(context)''',
    '''        context = merge_tool_messages(context)
        context = _normalize_dsml_history_messages(context)
        context = sort_tool_results_by_call_order(context)''')

open(P, "w", encoding="utf-8").write(src)
py_compile.compile(P, doraise=True)
print("0021 applied + py_compile OK")
