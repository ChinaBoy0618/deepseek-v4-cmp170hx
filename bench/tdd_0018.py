#!/usr/bin/env python3
"""TDD-0018 unit suite — P1-a: <thinking>/</thinking> hallucinated-variant
absorption in the deepseek_v4 reasoning parser (strip-only: tags vanish
from visible text; inner text STAYS visible; stock <think> flow untouched;
never enters REASONING state so an unclosed variant cannot eat a legit doc).

    docker cp tdd_0018.py dsv4-a100:/tmp/
    docker exec dsv4-a100 /opt/venv/bin/python3.12 /tmp/tdd_0018.py

RED on current stack: V1-V4 (variant tags leak into content).
GREEN invariants: V5-V7 (stock <think> behavior unchanged).
"""
import sys

sys.path.insert(0, "/vllm")
from transformers import AutoTokenizer  # noqa: E402

from vllm.entrypoints.openai.chat_completion.protocol import (  # noqa: E402
    ChatCompletionRequest,
)
from vllm.reasoning import ReasoningParserManager  # noqa: E402

tok = AutoTokenizer.from_pretrained("/model", trust_remote_code=True)
parser_cls = ReasoningParserManager.get_reasoning_parser("deepseek_v4")
REQ = ChatCompletionRequest(messages=[], model="dsv4s")

fails = []


def chk(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ((" | " + detail) if not cond else ""))
    if not cond:
        fails.append(name)


def nonstream(out, thinking):
    p = parser_cls(tok, chat_template_kwargs={"thinking": thinking})
    return p.extract_reasoning(model_output=out, request=REQ)


def stream(deltas, thinking):
    p = parser_cls(tok, chat_template_kwargs={"thinking": thinking})
    content, reasoning = None, None
    prev_text, prev_toks = "", []
    for d in deltas:
        toks = [p.vocab.get(t) for t in p.model_tokenizer.tokenize(d)
                if t in p.vocab]
        cur_text, cur_toks = prev_text + d, prev_toks + toks
        dm = p.extract_reasoning_streaming(
            prev_text, cur_text, d, prev_toks, cur_toks, toks)
        for delta in (dm if isinstance(dm, list) else [dm]):
            if delta is None:
                continue
            if delta.content:
                content = (content or "") + delta.content
            if getattr(delta, "reasoning", None):
                reasoning = (reasoning or "") + delta.reasoning
        prev_text, prev_toks = cur_text, cur_toks
    return reasoning, content


# ---- RED: variant tags must vanish from content (strip-only) ----
r, c = nonstream("abc<thinking>inner</thinking>def", False)
chk("V1 nonstream variant stripped [RED]",
    c is not None and "<thinking>" not in c and "</thinking>" not in c
    and c == "abcinnerdef", f"content={c!r}")
r, c = stream(["abc", "<thinking>", "inner", "</thinking>", "def"], False)
chk("V2 stream variant stripped [RED]",
    c == "abcinnerdef" and "<thinking>" not in (c or ""), f"content={c!r}")
r, c = nonstream("a<thinking>b<thinking>c</thinking>d</thinking>e", False)
chk("V3 nested variants stripped", c == "abcde", f"content={c!r}")
doc = "The tag <thinking> is hallucinated; compare with <think> too."
r, c = nonstream(doc, False)
chk("V4 legit doc keeps prose, drops tags",
    c is not None and "hallucinated" in c and "<thinking>" not in c
    and "<think>" not in c, f"content={c!r}")

# ---- GREEN: stock <think> behavior unchanged ----
r, c = nonstream("my reasoning</think>the answer", True)
chk("V5 stock stream-open reasoning",
    r == "my reasoning" and c == "the answer", f"r={r!r} c={c!r}")
r, c = nonstream("<think>r</think>c", False)
chk("V6 stock explicit tags", r == "r" and c == "c", f"r={r!r} c={c!r}")
r, c = stream(["<think>", "reason", "</think>", "visible"], False)
chk("V7 stock streaming", r == "reason" and c == "visible", f"r={r!r} c={c!r}")

print("\n" + ("ALL GREEN" if not fails else "RED: %d fail(s): %s" % (len(fails), fails)))
sys.exit(1 if fails else 0)
