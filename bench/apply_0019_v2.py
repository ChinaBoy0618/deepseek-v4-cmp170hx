#!/usr/bin/env python3
"""DSV4 0019 v2: repetition window floor.

v1 lesson (live TDD): window = new_token_ids + 16 overlap (~21 tokens at
spec=5). A repeated ~4-6 token line needs 5 occurrences ~ 25-30 tokens,
and the sliding boundary truncates one occurrence -> count tops at 4 ->
tripwire dead in production. v2: window floor DSV4_REP_WINDOW (default
160 tokens) so the count sees a stable trailing region; streak-6 rule
unchanged (a parked legit duplicate set stops firing as soon as the
line scrolls out / generation moves on, same as 0015 soup semantics)."""
import py_compile
import shutil

P = "/mnt/nvme1/dsv4/vllm-c3046d1/vllm/v1/core/sched/scheduler.py"
src = open(P, encoding="utf-8").read()
shutil.copy(P, P + ".bak-0019v2")


def rep(old, new, cnt=1):
    global src
    assert src.count(old) == cnt, ("anchor broken", old[:70], src.count(old))
    src = src.replace(old, new)


# 1. knob next to the streak knob
rep('''        self._dsv4_rep_streak = int(os.environ.get("DSV4_REP_STREAK", "6"))''',
    '''        self._dsv4_rep_streak = int(os.environ.get("DSV4_REP_STREAK", "6"))
        # DSV4 0019 v2: the +16 overlap window (~21 tokens at spec=5) can
        # never hold 5 occurrences of a ~5-token line; floor the window.
        self._dsv4_rep_window = int(os.environ.get("DSV4_REP_WINDOW", "160"))''')

# 2. rep-detector window floor (anchor: the `if not _win:` guard is
#    unique to _dsv4_rep_lines; the soup detector's is `if not any(...)`)
rep('''        _win = request._output_token_ids[-(len(new_token_ids) + 16):]
        if not _win:''',
    '''        _w = max(len(new_token_ids) + 16,
                 getattr(self, "_dsv4_rep_window", 160))
        _win = request._output_token_ids[-_w:]
        if not _win:''')

open(P, "w", encoding="utf-8").write(src)
py_compile.compile(P, doraise=True)
print("0019v2 applied + py_compile OK")
