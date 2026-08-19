#!/usr/bin/env python3
"""TDD-0019 v2: repetition window vs live block sizes.

v1 lesson (live TDD): _dsv4_rep_lines window = new_token_ids + 16 overlap.
Live spec-decode blocks are ~5 tokens -> window ~21 tokens. A repeated
short line is ~5 tokens, so 5 occurrences need >= ~25-30 tokens: the
window can NEVER satisfy the >=5 threshold -> the tripwire is dead in
production (i2 replay: dup5 responses, delta=0 fires).

v2: window floor DSV4_REP_WINDOW (default 160 tokens) so the count sees
a stable trailing region; streak-6 semantics unchanged.

RED on 0019 v1 stack: W01, W02, W08.
Everything else must be GREEN before AND after.
"""
import sys
import types

sys.path.insert(0, "/vllm")
from vllm.v1.core.sched.scheduler import Scheduler  # noqa: E402
from transformers import AutoTokenizer  # noqa: E402

tok = AutoTokenizer.from_pretrained("/model", trust_remote_code=True)
inst = Scheduler.__new__(Scheduler)
inst.structured_output_manager = types.SimpleNamespace(tokenizer=tok)

fails = []


def chk(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ((" | " + detail) if not cond else ""))
    if not cond:
        fails.append(name)


def rep_line(text, new_ids):
    req = types.SimpleNamespace(_output_token_ids=tok.encode(text, add_special_tokens=False))
    return inst._dsv4_rep_lines(req, new_ids)


# ---------- W01: 12 consecutive repeats VISIBLE to one detector call [RED] ----------
twelve = "Lets reload.\n" * 12
all_ids = tok.encode(twelve, add_special_tokens=False)
chk("W01 window sees 5+ repeats of 6-token line [RED]",
    rep_line(twelve, all_ids[-5:]) is not None,
    "window=%d tokens, saw <5 occurrences" % (5 + 16))


# ---------- W02: live-size 5-token block stream fires by streak 6 [RED] ----------
def stream_fire(text, block=5, streak=6):
    """Exact live shape: decode blocks of `block` tokens, streak resets
    on any non-detecting block, fire at streak >= 6 (0019 rule)."""
    ids = tok.encode(text, add_special_tokens=False)
    s, out = 0, []
    for i in range(0, len(ids), block):
        b = ids[i:i + block]
        out.extend(b)
        req = types.SimpleNamespace(_output_token_ids=out)
        if inst._dsv4_rep_lines(req, b) is not None:
            s += 1
        else:
            s = 0
        if s >= streak:
            return True
    return False


degenerate = "Setup test env.\n" + "Lets reload.\n" * 14 + "Then verify.\n"
chk("W02 live-size block stream fires [RED]", stream_fire(degenerate))


# ---------- guards (must stay GREEN before AND after) ----------
prose = "We test dialog handling now.\n" + \
        "First check the bindings.\nLets reload.\nThen the store syncs fine.\n" * 3
chk("W03 3 sparse mentions never fire", not stream_fire(prose))
four = "Intro line here.\n" + "Some prose about testing.\nLets reload.\nMore prose follows here.\n" * 4
chk("W04 4 interleaved mentions never fire", not stream_fire(four))
seps = "---\n\n===\n***\n___\n--\n==\n```\n" * 6
chk("W05 whitelisted separators never fire", not stream_fire(seps))
long_lines = "This line is deliberately much longer than twenty eight chars.\n" * 10
chk("W06 long lines (>28 chars) never fire", not stream_fire(long_lines))
table = "| col | val |\n|---|---|\n| a | 1 |\n| b | 2 |\n| c | 3 |\n" \
        "| d | 4 |\n| e | 5 |\n| f | 6 |\nplain closing sentence.\n" * 3
chk("W07 table with distinct short rows never fire", not stream_fire(table))


# ---------- W08: dedicated window knob present [RED] ----------
chk("W08 _dsv4_rep_window knob attribute exists [RED]",
    hasattr(Scheduler, "_dsv4_rep_window")
    or "_dsv4_rep_window" in Scheduler.__init__.__code__.co_names
    or "DSV4_REP_WINDOW" in open(
        "/vllm/vllm/v1/core/sched/scheduler.py", encoding="utf-8").read(),
    "scheduler.py has no DSV4_REP_WINDOW")

print("\n" + ("ALL GREEN" if not fails else "RED: %d fail(s): %s" % (len(fails), fails)))
sys.exit(1 if fails else 0)
