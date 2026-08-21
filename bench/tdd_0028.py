#!/usr/bin/env python3
"""TDD-0028 v2 (drain-predicate fix: async/v2 ph-based in-flight criterion).

Deterministic, runs INSIDE the container, no GPU engine needed.

    docker run --rm --runtime=nvidia -e NVIDIA_VISIBLE_DEVICES=0 \
      -v $R/vllm/v1/core/sched/scheduler.py:/vllm/vllm/v1/core/sched/scheduler.py:ro \
      -v $R/vllm/v1/structured_output/__init__.py:/vllm/vllm/v1/structured_output/__init__.py:ro \
      -v $REPO/bench/tdd_0028.py:/tmp/tdd_0028.py:ro \
      --entrypoint /opt/venv/bin/python3.12 dsv4-a100-fullbuild:devel /tmp/tdd_0028.py

Root cause (0028 RCA 2026-08-21, five instrumentation stages + live A/B):

    The engine runs the ASYNC scheduler (v2 model runner; core.py:169
    factory). Every structured request is deferred
    (AsyncScheduler._update_after_schedule sets pending_structured_output_
    tokens when num_output_placeholders > 0 at schedule time) and its
    grammar mask must be built only after older in-flight batches have
    been delivered -- the FSM advances only in update_from_output. 0027
    v2's drain predicate has_structured_output_in_flight early-returns
    False under use_v2_model_runner, so the drain in
    step_with_batch_queue NEVER ran: FSM stale by up to queue_depth-1
    batches (pp_size=4) -> masks valid at a STALE state -> rejection
    sampler accepts FSM-invalid drafts (greedy agreement) -> TYPE-B ->
    0008 salvage abandons enforcement -> corrupt tail (17-18/24 battery,
    every BAD traced to a TYPE-B; single-request never goes deep enough
    to go stale).

    v1 (keep rows after -1 constrained) went RED->GREEN here but FAILED
    live A/B (4/24, doubled tokens: namename, {{) -- proof that stock -1
    semantics are correct GIVEN a caught-up FSM + real windows, and the
    defect is the missing drain. v1 reverted.

Fix under test: replace the early-return with the async-native ph
criterion. num_output_placeholders is incremented by
(num_sampled_tokens_per_step + cur_num_spec_tokens) at schedule
(async_scheduler.py:38-41) and decremented by len(new_token_ids) at
delivery (async_scheduler.py:62) plus num_rejected on rejection, so
ph == own_share exactly when every older position is delivered -> the
drain terminates exactly, independent of accept counts.

RED on stock (v1 reverted): V01, V03, V07 (predicate returns False under v2)
GREEN on stock and fix: V02, V04, V05, V06, V08, V09 (bounds + sync regression)
Part G (grammar_bitmask stock -1 semantics): GREEN on both -- guards
against re-introducing the disproven v1 change.
"""
import sys
import types

sys.path.insert(0, "/vllm")  # bind-mounted patched checkout

import torch  # noqa: E402

from vllm.v1.core.sched.scheduler import Scheduler  # noqa: E402
from vllm.v1.structured_output import StructuredOutputManager  # noqa: E402

fails = []


def chk(name, cond, detail=""):
    ok = bool(cond)
    print(("PASS " if ok else "FAIL ") + name + ((" | " + detail) if not ok else ""))
    if not ok:
        fails.append(name)


# ============================ Part A: drain predicate ============================


def mk_sched(v2=True):
    s = Scheduler.__new__(Scheduler)
    s.use_v2_model_runner = v2
    s.requests = {}
    s.num_sampled_tokens_per_step = 1
    return s


def mk_req(ph=0, structured=True, prefill=False, in_flight=0):
    return types.SimpleNamespace(
        use_structured_output=structured,
        is_prefill_chunk=prefill,
        num_output_placeholders=ph,
        num_in_flight_tokens=in_flight,
    )


def mk_so(sched_tokens, spec):
    return types.SimpleNamespace(
        num_scheduled_tokens=sched_tokens,
        scheduled_spec_decode_tokens=spec,
    )


# V01 [RED]: v2 engine, structured spec req: ph=12 (own share 1+5=6, two
# older batches undelivered) -> must report in-flight so the drain runs.
# Stock returns False -> FSM stale -> TYPE-B -> corrupt tail.
s = mk_sched(v2=True)
s.requests["r1"] = mk_req(ph=12)
so = mk_so({"r1": 6}, {"r1": (0, 0, 0, 0, 0)})
chk(
    "V01 v2: ph=12 > own 6 -> in flight",
    s.has_structured_output_in_flight(so) is True,
    "stock early-returns False under use_v2_model_runner (drain disabled)",
)

# V02: v2 engine, caught up exactly (ph == own share) -> not in flight.
s = mk_sched(v2=True)
s.requests["r2"] = mk_req(ph=6)
so = mk_so({"r2": 6}, {"r2": (0, 0, 0, 0, 0)})
chk("V02 v2: ph=6 == own 6 -> not in flight", s.has_structured_output_in_flight(so) is False)

# V03 [RED]: v2 engine, non-spec decode req (no spec window, own share 1):
# ph=2 (one older position) -> in flight.
s = mk_sched(v2=True)
s.requests["r3"] = mk_req(ph=2)
so = mk_so({"r3": 1}, {})
chk("V03 v2: non-spec ph=2 > own 1 -> in flight", s.has_structured_output_in_flight(so) is True)

# V04: v2 engine, non-spec caught up (ph == 1) -> not in flight.
s = mk_sched(v2=True)
s.requests["r4"] = mk_req(ph=1)
so = mk_so({"r4": 1}, {})
chk("V04 v2: non-spec ph=1 == own 1 -> not in flight", s.has_structured_output_in_flight(so) is False)

# V05: prefill chunks never contribute (mirror async_scheduler.py:28-29 skip).
s = mk_sched(v2=True)
s.requests["r5"] = mk_req(ph=12, prefill=True)
so = mk_so({"r5": 6}, {"r5": (0, 0, 0, 0, 0)})
chk("V05 v2: prefill chunk skipped", s.has_structured_output_in_flight(so) is False)

# V06: non-structured requests never contribute (no FSM to catch up).
s = mk_sched(v2=True)
s.requests["r6"] = mk_req(ph=12, structured=False)
so = mk_so({"r6": 6}, {"r6": (0, 0, 0, 0, 0)})
chk("V06 v2: non-structured skipped", s.has_structured_output_in_flight(so) is False)

# V07 [RED]: mixed batch -- one caught-up structured, one stale -> in flight.
s = mk_sched(v2=True)
s.requests["r7a"] = mk_req(ph=6)
s.requests["r7b"] = mk_req(ph=13)
so = mk_so({"r7a": 6, "r7b": 6}, {"r7a": (0,) * 5, "r7b": (0,) * 5})
chk("V07 v2: any stale structured req -> in flight", s.has_structured_output_in_flight(so) is True)

# V08: finished/aborted request id (absent from scheduler.requests) -> no crash.
s = mk_sched(v2=True)
so = mk_so({"gone": 6}, {"gone": (0,) * 5})
chk("V08 v2: unknown req id -> not in flight, no crash", s.has_structured_output_in_flight(so) is False)

# V09: sync path (use_v2_model_runner=False) criterion unchanged:
# num_in_flight_tokens vs the num_scheduled_tokens value.
s = mk_sched(v2=False)
s.requests["r9"] = mk_req(in_flight=12)
so = mk_so({"r9": 6}, {})
chk("V09 sync: in_flight 12 > own 6 -> in flight", s.has_structured_output_in_flight(so) is True)
s.requests["r9"] = mk_req(in_flight=6)
chk("V09b sync: in_flight 6 == own 6 -> not in flight", s.has_structured_output_in_flight(so) is False)


# ==================== Part G: grammar_bitmask stock -1 semantics ====================
# 0028 v2 does NOT touch grammar_bitmask. These pin the STOCK behavior the
# live A/B validated (v1's constrained-tail change was disproven: 4/24).
# Stock: the -1 row itself is still constrained (fill runs before the
# flip); every row AFTER the first -1 is unconstrained; the FSM never
# advances past an unverified token.

class FakeGrammar:
    def __init__(self):
        self.advances = []
        self.rollbacks = []
        self.terminated = False

    def is_terminated(self):
        return self.terminated

    def accept_tokens(self, req_id, tokens):
        self.advances.extend(tokens)
        return all(t >= 0 for t in tokens)

    def validate_tokens(self, tokens):
        return [t for t in tokens if t >= 0]

    def rollback(self, n):
        self.rollbacks.append(n)

    def fill_bitmask(self, bitmask, index):
        pass  # spy path replaces _fill_bitmasks entirely


def mk_mgr():
    mgr = StructuredOutputManager.__new__(StructuredOutputManager)
    mgr.vllm_config = types.SimpleNamespace(
        num_speculative_tokens=5,
        scheduler_config=types.SimpleNamespace(max_num_seqs=8),
        model_config=types.SimpleNamespace(is_diffusion=False),
    )
    mgr._grammar_bitmask = torch.zeros((64, 4), dtype=torch.int32)
    mgr._full_mask = torch.ones(4, dtype=torch.int32)
    mgr.fill_bitmask_parallel_threshold = 10**9
    mgr.enable_in_reasoning = False
    mgr._get_reasoner = lambda request: None  # post-thinking
    mgr.calls = []
    mgr._fill_bitmasks = lambda batch: mgr.calls.extend(batch)
    return mgr


def mk_req_grammar(grammar):
    return types.SimpleNamespace(
        structured_output_request=types.SimpleNamespace(grammar=grammar),
        all_token_ids=[1, 2, 3],
    )


mgr_grammar = {}


def run(mgr, rid, window):
    mgr.calls.clear()
    reqs = {rid: mk_req_grammar(mgr_grammar[rid])}
    return mgr.grammar_bitmask(reqs, [rid], {rid: window})


# G01: [10,-1,-1,-1,-1] -- stock stock: -1 row constrained, rows after NOT.
g = FakeGrammar()
mgr_grammar["g1"] = g
m = mk_mgr()
run(m, "g1", [10, -1, -1, -1, -1])
applies = [a for (_, _, a) in m.calls]
chk(
    "G01 stock -1 tail: [T,T,F,F,F] + bonus T, FSM advances only [10]",
    applies == [True, True, False, False, False, True]
    and g.advances == [10]
    and sum(g.rollbacks) == 1,
    f"applies={applies} advances={g.advances} rollbacks={g.rollbacks}",
)

# G02: [10,11,-1,-1,-1] -- valid prefix advanced, -1 row still constrained
# (fill runs before the flip), rows after it unconstrained.
g2 = FakeGrammar()
mgr_grammar["g2"] = g2
m2 = mk_mgr()
run(m2, "g2", [10, 11, -1, -1, -1])
applies2 = [a for (_, _, a) in m2.calls]
chk(
    "G02 stock -1 tail after 2 valid: [T,T,T,F,F] + bonus T, advances [10,11]",
    applies2 == [True, True, True, False, False, True]
    and g2.advances == [10, 11]
    and sum(g2.rollbacks) == 2,
    f"applies={applies2} advances={g2.advances}",
)

# G03: fully-valid window unchanged.
g3 = FakeGrammar()
mgr_grammar["g3"] = g3
m3 = mk_mgr()
run(m3, "g3", [10, 11, 12, 13, 14])
applies3 = [a for (_, _, a) in m3.calls]
chk(
    "G03 valid window: 5 rows + bonus all constrained, 5 advances",
    applies3 == [True] * 6 and g3.advances == [10, 11, 12, 13, 14] and sum(g3.rollbacks) == 5,
    f"applies={applies3} advances={g3.advances}",
)

# G04: thinking window stays unconstrained (no over-constraining).
g4 = FakeGrammar()
mgr_grammar["g4"] = g4
m4 = mk_mgr()
m4._get_reasoner = lambda request: object()
m4.enable_in_reasoning = False
m4.should_fill_bitmask = lambda request: False
req4 = mk_req_grammar(g4)
req4.structured_output_request.reasoning_ended = False
m4.calls.clear()
m4.grammar_bitmask({"g4": req4}, ["g4"], {"g4": [-1, -1, -1, -1, -1]})
applies4 = [a for (_, _, a) in m4.calls]
chk("G04 thinking window: all rows unconstrained", applies4 == [False] * 6, f"applies={applies4}")


# ============ Part H: DraftTokensHandler per-req draft persistence ============
# 0028v3 RCA (diag6): under the v2/async PP scheduler a request decodes
# every `pp_size` steps (next_decode_eligible_step, scheduler.py:593), so
# drafts proposed during request R's batch B_k are consumed by the
# scheduler-side rewrite only at R's NEXT batch B_{k+4} -- by which time 3
# other batches have overwritten the single-snapshot handler. COVMISS=236:
# the rewrite never sees R -> placeholder [-1]*5 window -> unconstrained
# spec rows -> FSM-invalid commits -> TYPE-B -> corrupt tail.
#
# Fix: get_draft_tokens merges each snapshot into a per-req map and returns
# the UNION (FIFO-capped). in_output skips reqs without a window in the
# current scheduler_output, so the union is safe to feed it.
#
# RED on stock: H01, H03 (single-snapshot loses rotated-out reqs)
# GREEN on both: H02, H04, H05 (guards)
from vllm.v1.worker.gpu.spec_decode.utils import DraftTokensHandler  # noqa: E402

DEV = torch.device("cuda:0")


def mk_ib(req_ids, structured=True):
    return types.SimpleNamespace(
        req_ids=list(req_ids), has_structured_output_reqs=structured
    )


h = DraftTokensHandler(DEV)
h.set_draft_tokens(mk_ib(["rA", "rB"]), torch.randint(0, 100, (2, 5), device=DEV))
h.set_draft_tokens(mk_ib(["rC", "rD"]), torch.randint(0, 100, (2, 5), device=DEV))
got = h.get_draft_tokens()
chk(
    "H01 union covers the rotating group from 4 steps ago",
    set(got.req_ids) >= {"rA", "rB", "rC", "rD"},
    f"reqs={got.req_ids} (stock single-snapshot returns only the latest batch)",
)
chk(
    "H02 rows well-formed (5 ints)",
    all(len(r) == 5 and all(isinstance(t, int) for t in r) for r in got.draft_token_ids),
    f"rows={got.draft_token_ids}",
)

# H03: a batch with no structured reqs discards the snapshot (np=None) but
# must not lose earlier merged rows; its own reqs are grammar-irrelevant.
h.set_draft_tokens(mk_ib(["rE"], structured=False), torch.randint(0, 100, (1, 5), device=DEV))
got2 = h.get_draft_tokens()
chk(
    "H03 plain batch keeps union, adds nothing",
    "rA" in got2.req_ids and "rE" not in got2.req_ids,
    f"reqs={got2.req_ids}",
)

# H04: a re-drafted req gets its NEWEST row.
h.set_draft_tokens(mk_ib(["rA"]), torch.full((1, 5), 7, device=DEV, dtype=torch.int64))
got3 = h.get_draft_tokens()
row_a = got3.draft_token_ids[got3.req_ids.index("rA")]
chk("H04 re-drafted req gets newest row", row_a == [7] * 5, f"row_a={row_a}")

# H05: FIFO cap bounds the map (finished reqs never evicted explicitly).
h2 = DraftTokensHandler(DEV)
for i in range(300):
    h2.set_draft_tokens(mk_ib([f"q{i}"]), torch.full((1, 5), i, device=DEV, dtype=torch.int64))
got5 = h2.get_draft_tokens()
chk("H05 FIFO cap bounds the map", len(got5.req_ids) <= 256, f"n={len(got5.req_ids)}")

print()
if fails:
    print(f"TDD-0028: {len(fails)} FAIL: {fails}")
    sys.exit(1)
print("TDD-0028: ALL GREEN")
