#!/usr/bin/env python3
"""TDD-0017 unit suite (deterministic, runs INSIDE the container, no GPU
engine needed — drives the REAL patched Scheduler class + REAL tokenizer).

    docker cp tdd_0017.py dsv4-a100:/tmp/
    docker exec dsv4-a100 /opt/venv/bin/python3.12 /tmp/tdd_0017.py

RED on the current 0016 stack: U03 (new signatures absent),
U10/U14 (detector blind to new signatures), U20+ (clean-cut missing).
Everything else must be GREEN before AND after 0017.
"""
import sys
import types

sys.path.insert(0, "/vllm")  # bind-mounted patched checkout
from vllm.v1.core.sched.scheduler import Scheduler  # noqa: E402

CUR = [
    "<reference", "<tool_calls", "<tool-call-name", "<dies_cmd_wrapper",
    "<empty-tool-call", "<original_code_end", "<commit_begin",
    "text_placeholder", "<edit-path", "<source>placeholder",
    "<original_output",
]
NEW = [
    "<Write ", "<bash_command", "<call ", "<answer>", "<analyze>",
    "<thinking>", "</assistant>", "<assistant_unitsummary>",
    "<system-reminder>",
]
LEGIT = [
    "<｜DSML｜invoke>", "<｜DSML｜parameter>", "<｜DSML｜tool_calls>",
    "<think>", "</think>",
]

fails = []


def chk(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ((" | " + detail) if not cond else ""))
    if not cond:
        fails.append(name)


def join(ids):
    return "".join(ids)


# ---------- table content ----------
SIGS = getattr(Scheduler, "_DSV4_SIG_STRINGS", None)
chk("U01 sig-table exists", isinstance(SIGS, (tuple, list)) and len(SIGS) >= 11)
chk("U02 0015 old signatures kept", SIGS is not None and all(s in SIGS for s in CUR))
chk("U03 P0-a: 9 new signatures in table [RED]",
    SIGS is not None and all(s in SIGS for s in NEW),
    "missing: " + ",".join(s for s in NEW if SIGS and s not in SIGS))
chk("U04 zero overlap with legit DSML/think",
    SIGS is not None and not [s for s in SIGS for l in LEGIT if s in l])
chk("U05 trailing-space guards",
    "<call " not in "<callable object>"
    and "<Write " not in "<Writer"
    and "<analyze>" not in "<analysis>")

# ---------- real detector via __new__ + real tokenizer ----------
from transformers import AutoTokenizer  # noqa: E402

tok = AutoTokenizer.from_pretrained("/model", trust_remote_code=True)
inst = Scheduler.__new__(Scheduler)
inst.structured_output_manager = types.SimpleNamespace(tokenizer=tok)
if SIGS is not None:
    inst._dsv4_sig_first_ids = frozenset(
        _ids[0] for _ids in (
            tok.encode(s, add_special_tokens=False) for s in SIGS
        ) if _ids
    )


def detect(text, new_part=None):
    """One detector call over `text` (new_part = tokens appended this block)."""
    req = types.SimpleNamespace(_output_token_ids=tok.encode(text, add_special_tokens=False))
    return inst._dsv4_soup_tags(req, new_part or req._output_token_ids)


chk("U10 detector sees every NEW signature [RED]",
    all(s in detect(s + "\n" + s + "\n" + s) for s in NEW),
    "blind to: " + ",".join(s for s in NEW if s not in detect(s + "\n" + s + "\n" + s)))
chk("U11 detector still sees old signatures",
    "<reference" in detect("<reference x <reference y <reference"))


def streak_sim(blocks, threshold=12):
    """Replicates the exact streak loop from _update_request_with_output."""
    streaks, fired, out = {}, None, []
    for b in blocks:
        out.extend(tok.encode(b, add_special_tokens=False))
        req = types.SimpleNamespace(_output_token_ids=out)
        tags = inst._dsv4_soup_tags(req, tok.encode(b, add_special_tokens=False))
        for s in streaks:
            if s not in tags:
                streaks[s] = 0
        for s in tags:
            streaks[s] = streaks.get(s, 0) + 1
            if streaks[s] >= threshold:
                fired = s
                break
        if fired:
            break
    return fired


one_shot = ["intro text\n<system-reminder>quoted once</system-reminder>\n"] + \
           ["plain continuation text block %d with normal prose words.\n" % i for i in range(12)]
soup_blocks = ["<bash_command>cat x.py</bash_command>\n<answer>ok</answer>\n" for _ in range(12)]
chk("U12 one-shot legit mention never fires",
    streak_sim(one_shot) is None)
chk("U13 '<callable' bait never detected",
    detect("<callable object at 0x7f1a>" * 4) == set())
chk("U14 soup blocks fire by streak 12 [RED]",
    streak_sim(soup_blocks) is not None)

# ---------- P0-b clean-cut ----------
# Contract: Scheduler._dsv4_clean_cut(self, out_tail:list, decode:callable,
#               max_back:int=16) -> int  (number of trailing tokens to drop)
chk("U20 clean-cut helper exists [RED]", hasattr(Scheduler, "_dsv4_clean_cut"))
if hasattr(Scheduler, "_dsv4_clean_cut"):
    f = Scheduler._dsv4_clean_cut.__get__(inst)

    def cc(text, max_back=16):
        ids = list(text)
        n = f(ids, join, max_back)
        return n, join(ids[: len(ids) - n])

    n, t = cc("abc</Ba");        chk("U21 cut mid-tag '</Ba'", n == 4 and t == "abc", f"{n},{t!r}")
    n, t = cc("foo <bash_comm"); chk("U22 cut long open tag", n == 10 and t == "foo ", f"{n},{t!r}")
    n, t = cc("done.</Bash>");   chk("U23 closed tag untouched", n == 0, f"{n},{t!r}")
    n, t = cc("a < b comparison"); chk("U24 '<'+space not a tag", n == 0, f"{n},{t!r}")
    n, t = cc("<");              chk("U25 bare '<' cut", n == 1 and t == "", f"{n},{t!r}")
    n, t = cc("text with 5 < 6 and <tag"); chk("U26 last-unclosed wins",
        n == 4 and t == "text with 5 < 6 and ", f"{n},{t!r}")
    n, _ = cc("x" * 40 + "<" + "y" * 40);    chk("U27 backtrack capped at 16", n <= 16, str(n))
    n, _ = cc("");                          chk("U28 empty tail -> 0", n == 0, str(n))

print("\n" + ("ALL GREEN" if not fails else "RED: %d fail(s): %s" % (len(fails), fails)))
sys.exit(1 if fails else 0)
