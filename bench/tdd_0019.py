#!/usr/bin/env python3
"""TDD-0019 unit suite — P1-c: line-repetition tripwire (issue-2:
"Lets reload."x60 inside ONE message). Same streak framework as 0015,
but the detected pattern is a short (<=6 token) line appearing >=5x in
the new-token window. Separators whitelisted; long lines never flagged.

    docker cp tdd_0019.py dsv4-a100:/tmp/
    docker exec dsv4-a100 /opt/venv/bin/python3.12 /tmp/tdd_0019.py

RED on current stack: R00 (helper missing) -> everything downstream.
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


chk("R00 rep helper exists [RED]", hasattr(Scheduler, "_dsv4_rep_lines"))


def hit(text, chunk=None):
    ids = tok.encode(text, add_special_tokens=False)
    req = types.SimpleNamespace(_output_token_ids=ids)
    chunk = chunk if chunk is not None else ids
    return inst._dsv4_rep_lines(req, chunk)


if hasattr(Scheduler, "_dsv4_rep_lines"):
    chk("R01 'Lets reload.'x5 window -> hit",
        hit("Lets reload.\n" * 5) == "Lets reload.")
    chk("R02 'Grep.'x6 -> hit", hit("Grep.\n" * 6) == "Grep.")
    chk("R03 separators whitelisted", hit("---\n" * 10) is None)
    chk("R04 only 4 repeats -> no hit", hit("Lets reload.\n" * 4) is None)
    long_line = "The quick brown fox jumps over the lazy dog by the river.\n"
    chk("R05 long lines never flagged", hit(long_line * 6) is None)
    mixed = "start prose here\n" + "Let me do it.\n" * 5 + "more prose\n"
    chk("R06 repetition amid prose -> hit",
        hit(mixed) == "Let me do it.")
    chk("R07 code-ish repeated short line still flagged",
        hit("end\n" * 5) == "end")

    # streak simulation over consecutive scheduler blocks
    def rep_streak(blocks, threshold=6):
        streak, out = 0, []
        for b in blocks:
            ids = tok.encode(b, add_special_tokens=False)
            out.extend(ids)
            req = types.SimpleNamespace(_output_token_ids=out)
            if inst._dsv4_rep_lines(req, ids):
                streak += 1
            else:
                streak = 0
            if streak >= threshold:
                return True
        return False

    soup = ["Lets reload.\nLets reload.\nLets reload.\nLets reload.\nLets reload.\n"] * 6
    diverse = ["sentence %d with entirely different words each time here.\n" % i for i in range(6)]
    chk("R08 6 consecutive rep blocks fire", rep_streak(soup) is True)
    chk("R09 diverse blocks never fire", rep_streak(diverse) is False)

print("\n" + ("ALL GREEN" if not fails else "RED: %d fail(s): %s" % (len(fails), fails)))
sys.exit(1 if fails else 0)
