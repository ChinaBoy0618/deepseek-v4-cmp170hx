#!/usr/bin/env python3
"""TDD-0027 (0027 PP structured-output drain — port of buliaoyin 6e959b2 /
upstream vllm#45015, adapted to the sync Scheduler, v2 criterion).

Deterministic, runs INSIDE the container, no GPU engine needed.

    docker cp tdd_0027.py dsv4-a100:/tmp/
    docker exec dsv4-a100 /opt/venv/bin/python3.12 /tmp/tdd_0027.py

First-principles invariant under test:

    The grammar bitmask for a scheduled batch must be computed only from
    FSM state that already includes every token position scheduled for
    that request in older, still-in-flight batches (batch_queue depth =
    pp_size = 4 on this deployment; the FSM only advances in
    update_from_output).

v2 criterion (post-mortem of the 0820 v1 crash): the deferral predicate is
    num_in_flight_tokens > this batch's own share
NOT num_output_placeholders. num_in_flight_tokens is real, exactly
mirrored bookkeeping (+= share at schedule L1420, -= share per batch at
delivery L1850, stale-shares drained in lockstep through preemption).
num_output_placeholders, by contrast, is load-bearing in SIX dormant sync
scheduler paths that have never seen a nonzero value on this deployment
(L578 max_tokens skip, L604 num_new_tokens arithmetic, L735, L1425
is_prefill_chunk, L1568) — activating it (v1) corrupted scheduling and,
combined with spec-rejection drift, deferred batches with an EMPTY queue
-> IndexError pop-from-empty-deque -> EngineDead.

RED on the current stock stack (sync Scheduler, no 0027):
    U01/U02*  has_structured_output_in_flight absent (AttributeError)
    U08       sync _update_after_schedule never sets
              pending_structured_output_tokens (stock sync leaves the
              engine's deferred branch permanently dead)
    U14/U15   _merge_engine_core_outputs / _pop_and_process_batch absent
    U16   ★   step_with_batch_queue computes the deferred batch's bitmask
              while older batches are still unprocessed (lag=1) -> stale
              FSM -> wrong mask. This is the bug being fixed.
Regression guards (GREEN on stock AND v2; RED on v1):
    U09/U12   num_output_placeholders must STAY 0 (v1 activated it)
    U17   ★   deferred flag with an EMPTY queue must not pop-crash
              (the 0820 v1 EngineDead, IndexError: pop from an empty deque)
Everything else must be GREEN before AND after 0027.
"""
import contextlib
import inspect
import sys
import types
from collections import deque
from concurrent.futures import Future

sys.path.insert(0, "/vllm")  # bind-mounted patched checkout

from vllm.v1.core.sched.interface import SchedulerInterface  # noqa: E402
from vllm.v1.core.sched.scheduler import Scheduler  # noqa: E402
from vllm.v1.engine.core import EngineCore  # noqa: E402
from vllm.v1.engine import EngineCoreOutputs  # noqa: E402
from vllm.v1.request import Request  # noqa: E402
from vllm import SamplingParams  # noqa: E402

fails = []


def chk(name, cond, detail=""):
    ok = bool(cond)
    print(("PASS " if ok else "FAIL ") + name + ((" | " + detail) if not ok else ""))
    if not ok:
        fails.append(name)


def fut(value):
    f = Future()
    f.set_result(value)
    return f


# ============ U01: interface default ============
try:
    # Unbound call with a dummy self: the default impl must not touch self.
    v = SchedulerInterface.has_structured_output_in_flight(object(), object())
    chk("U01 interface default exists and returns False", v is False, f"got {v!r}")
except (AttributeError, TypeError) as e:
    chk("U01 interface default exists and returns False [RED]", False, repr(e))


# ============ U02-U07: Scheduler predicate (real method) ============
def mk_sched():
    inst = Scheduler.__new__(Scheduler)
    inst.use_v2_model_runner = False
    inst.num_sampled_tokens_per_step = 1
    inst.requests = {}
    return inst


def mk_so(inst, rid, share=1, spec=()):
    inst.requests[rid] = types.SimpleNamespace(
        use_structured_output=True,
        num_output_placeholders=0,
        num_in_flight_tokens=share,  # default: exactly caught up
        is_prefill_chunk=False,
    )
    return types.SimpleNamespace(
        num_scheduled_tokens={rid: share},
        scheduled_spec_decode_tokens={rid: list(spec)} if spec else {},
    )


def pred(inst, so):
    return inst.has_structured_output_in_flight(so)


# U02: older positions in flight (in_flight > own share) -> True
s = mk_sched(); so = mk_so(s, "r1", share=1)
s.requests["r1"].num_in_flight_tokens = 7  # 1 own + 6 older
try:
    chk("U02 in_flight 7 > share 1 -> True [RED]", pred(s, so) is True)
except AttributeError as e:
    chk("U02 in_flight 7 > share 1 -> True [RED]", False, repr(e))

# U03: exactly caught up (in_flight == share) -> False
s = mk_sched(); so = mk_so(s, "r1", share=1)
try:
    chk("U03 in_flight == share -> False", pred(s, so) is False)
except AttributeError as e:
    chk("U03 in_flight == share -> False", False, repr(e))

# U04: spec decode share = 6 positions (1 sampled + 5 drafts)
s = mk_sched(); so = mk_so(s, "r1", share=6, spec=[9] * 5)
s.requests["r1"].num_in_flight_tokens = 6
try:
    chk("U04a spec share 6 == in_flight 6 -> False", pred(s, so) is False)
    s.requests["r1"].num_in_flight_tokens = 7
    chk("U04b spec in_flight 7 > share 6 -> True", pred(s, so) is True)
except AttributeError as e:
    chk("U04 spec share arithmetic", False, repr(e))

# U05: non-structured never flags
s = mk_sched(); so = mk_so(s, "r1", share=1)
s.requests["r1"].use_structured_output = False
s.requests["r1"].num_in_flight_tokens = 99
try:
    chk("U05 non-structured never in-flight", pred(s, so) is False)
except AttributeError as e:
    chk("U05 non-structured never in-flight", False, repr(e))

# U06: V2 runner excluded
s = mk_sched(); so = mk_so(s, "r1", share=1)
s.use_v2_model_runner = True
s.requests["r1"].num_in_flight_tokens = 5
try:
    chk("U06 use_v2_model_runner -> False", pred(s, so) is False)
except AttributeError as e:
    chk("U06 use_v2_model_runner -> False", False, repr(e))

# U07: finished request (id gone from self.requests) skipped
s = mk_sched()
so = types.SimpleNamespace(
    num_scheduled_tokens={"gone": 1}, scheduled_spec_decode_tokens={}
)
try:
    chk("U07 finished request skipped", pred(s, so) is False)
except AttributeError as e:
    chk("U07 finished request skipped", False, repr(e))


# ============ U08-U10: sync _update_after_schedule flag bookkeeping ============
def mk_sched2():
    inst = Scheduler.__new__(Scheduler)
    inst.num_sampled_tokens_per_step = 1
    inst.defer_block_free = False
    inst.enable_return_routed_experts = False
    inst._inflight_prefills = set()
    inst.sched_step_seq = 0
    inst.requests = {}
    return inst


class ReqNS:
    """Hashable attribute bag (SimpleNamespace is unhashable; the real
    scheduler puts requests into sets)."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def mk_req(num_tokens=10, num_computed=10, ph=0, in_flight=0, structured=True,
           prefill_chunk=False):
    return ReqNS(
        request_id="r1",
        num_tokens=num_tokens,
        num_computed_tokens=num_computed,
        num_output_placeholders=ph,
        num_in_flight_tokens=in_flight,
        last_sched_seq=0,
        use_structured_output=structured,
        is_prefill_chunk=prefill_chunk,
    )


def run_after_schedule(inst, req, ntok=6, spec=()):
    inst.requests["r1"] = req
    so = types.SimpleNamespace(
        num_scheduled_tokens={"r1": ntok},
        scheduled_spec_decode_tokens={"r1": list(spec)} if spec else {},
        num_spec_tokens_to_schedule=0,
        pending_structured_output_tokens=False,
        has_structured_output_requests=False,
    )
    inst._update_after_schedule(so)
    return so


# U08 [RED]: pending flag set when older positions are still in flight.
# num_in_flight_tokens enters at 1 (one older position); this batch adds 6.
s = mk_sched2(); r = mk_req(in_flight=1)
so = run_after_schedule(s, r, ntok=6)
chk("U08 sync sets pending_structured_output_tokens [RED]",
    so.pending_structured_output_tokens is True,
    f"flag={so.pending_structured_output_tokens}")

# U08b: caught-up request (0 older positions) -> no flag
s = mk_sched2(); r = mk_req(in_flight=0)
so = run_after_schedule(s, r, ntok=6)
chk("U08b caught-up request -> no flag",
    so.pending_structured_output_tokens is False,
    f"flag={so.pending_structured_output_tokens}")

# U09 [v1 regression guard]: placeholders must STAY 0. num_in_flight_tokens
# carries the bookkeeping instead (stock increments it at L1420).
s = mk_sched2(); r = mk_req(in_flight=1, ph=0)
run_after_schedule(s, r, ntok=6, spec=[9] * 5)
chk("U09 placeholders stay 0; in_flight 1+6=7 (v1-regression)",
    r.num_output_placeholders == 0 and r.num_in_flight_tokens == 7,
    f"ph={r.num_output_placeholders} in_flight={r.num_in_flight_tokens}")

# U10: prefill chunk -> no flag
s = mk_sched2(); r = mk_req(num_tokens=20, num_computed=3, in_flight=3,
                            prefill_chunk=True)
so = run_after_schedule(s, r, ntok=3)
chk("U10 prefill chunk skipped (no flag)",
    so.pending_structured_output_tokens is False,
    f"flag={so.pending_structured_output_tokens}")


# ============ U11/U12: delivery path must not touch placeholders ============
def mk_real_request(max_tokens=64):
    return Request(
        request_id="tdd0027",
        prompt_token_ids=[1, 2, 3, 4],
        sampling_params=SamplingParams(max_tokens=max_tokens, temperature=0.0),
        pooling_params=None,
    )


try:
    s = Scheduler.__new__(Scheduler)
    s.structured_output_manager = None
    s.max_model_len = 32768
    s._dsv4_soup_enabled = False  # 0015 tripwire off for this unit test
    s._dsv4_rep_enabled = False   # 0019 tripwire off for this unit test
    r = mk_real_request()
    out, _stopped = s._update_request_with_output(r, [11, 12, 13])
    chk("U11 delivery leaves placeholders at 0, tokens delivered",
        r.num_output_placeholders == 0 and len(out) == 3,
        f"ph={r.num_output_placeholders} out={out}")
except Exception as e:  # noqa: BLE001
    chk("U11 delivery leaves placeholders at 0, tokens delivered", False,
        f"{type(e).__name__}: {e}")

# U12 [v1 regression guard]: the v1 settle helper must be GONE
chk("U12 no _settle_output_placeholders on Scheduler (v1-regression)",
    not hasattr(Scheduler, "_settle_output_placeholders"))

# U13: preemption zeroes placeholders (stock regression guard)
src = inspect.getsource(Scheduler)
chk("U13 preemption zeroes placeholders (stock)",
    "num_output_placeholders = 0" in src)


# ============ U14: _merge_engine_core_outputs (module-level fn) ============
try:
    from vllm.v1.engine.core import _merge_engine_core_outputs as merge  # noqa: E402
    a = {0: EngineCoreOutputs(outputs=["o1"], finished_requests={1, 2})}
    b = {
        0: EngineCoreOutputs(outputs=["o2"], finished_requests={2, 3},
                             scheduler_stats="S"),
        1: EngineCoreOutputs(outputs=["o3"]),
    }
    merge(a, b)
    chk("U14 merge outputs/finished/stats",
        a[0].outputs == ["o1", "o2"] and a[0].finished_requests == {1, 2, 3}
        and a[0].scheduler_stats == "S" and 1 in a)
except (AttributeError, ImportError) as e:
    chk("U14 merge outputs/finished/stats [RED]", False, repr(e))


# ============ U15/U16/U17: engine drain (real step_with_batch_queue) ============
def mk_engine(lag):
    """EngineCore via __new__ with stub collaborators.

    Fake scheduler: `lag` = older in-flight batches still carrying the
    structured request's unprocessed positions. Each update_from_output
    processes one older batch (lag -= 1). get_grammar_bitmask records the
    lag AT THE MOMENT the mask is computed — the invariant is that this is
    always 0.
    """
    fs = types.SimpleNamespace()
    fs.lag = lag
    fs.calls = []
    fs.bitmask_lags = []
    fs.has_requests = lambda: True

    def _schedule(_throttle=False):
        # The batch being scheduled this step: structured + pending (i.e.
        # its bitmask needs tokens still in flight) -> deferred sampling.
        return types.SimpleNamespace(
            pending_structured_output_tokens=True,
            total_num_scheduled_tokens=6,
            scheduled_spec_decode_tokens={},
            tag="D",
        )

    fs.schedule = _schedule
    fs.has_structured_output_in_flight = lambda so: fs.lag > 0
    fs.update_from_output = (
        lambda so, mo: (fs.calls.append(so.tag), setattr(fs, "lag", fs.lag - 1),
                        {0: EngineCoreOutputs(outputs=[])})[-1]
    )
    fs.get_grammar_bitmask = (
        lambda so: (fs.bitmask_lags.append(fs.lag), None)[-1]
    )
    fs.update_draft_token_ids_in_output = lambda *a, **k: None

    eng = EngineCore.__new__(EngineCore)
    eng.batch_queue_size = 4
    # Real queue semantics: newest is appendleft'ed, pop() takes the RIGHT
    # (oldest). Build so OLD0 is rightmost = processed first.
    older = [
        (fut(f"mo{i}"), types.SimpleNamespace(tag=f"OLD{i}"), fut(None))
        for i in range(lag)
    ]
    eng.batch_queue = deque(reversed(older))
    eng.scheduler = fs
    eng.model_executor = types.SimpleNamespace(
        execute_model=lambda so, non_block: fut(None),
        sample_tokens=lambda g, non_block: fut("sampled"),
    )
    eng.is_ec_consumer = True
    eng.is_pooling_model = False
    eng.check_for_draft_tokens = False
    eng.capture_iteration_details = lambda so: contextlib.nullcontext(None)
    eng.log_error_detail = lambda so: contextlib.nullcontext(None)
    eng._process_aborts_queue = lambda: None
    eng._attach_iteration_details = lambda a, b: None
    eng._should_throttle_prefills = lambda: False
    return eng, fs

# U15 [RED]: _pop_and_process_batch exists and works
try:
    eng, fs = mk_engine(lag=1)
    out = eng._pop_and_process_batch(eng.batch_queue)
    chk("U15 _pop_and_process_batch pops oldest, returns outputs",
        fs.calls == ["OLD0"] and len(eng.batch_queue) == 0 and isinstance(out, dict))
except AttributeError as e:
    chk("U15 _pop_and_process_batch pops oldest, returns outputs [RED]",
        False, repr(e))

# U16 [RED-semantic]: bitmask computed only after FSM caught up
eng, fs = mk_engine(lag=2)
eng.step_with_batch_queue()
chk("U16 bitmask computed at lag 0 (drained) [RED-semantic]",
    fs.bitmask_lags == [0] and fs.calls == ["OLD0", "OLD1"],
    f"bitmask_lags={fs.bitmask_lags} processed={fs.calls} "
    f"queue_left={len(eng.batch_queue)}")
chk("U16b deferred batch re-queued for sampling",
    len(eng.batch_queue) == 1)

# U17 [v1 crash regression, 0820 IndexError pop-from-empty-deque]: a
# deferred batch arriving while the queue is ALREADY empty must not
# pop-crash the engine. (v2 criterion makes this state unreachable from
# correct bookkeeping; the guarded pop makes it survivable regardless.)
try:
    eng, fs = mk_engine(lag=0)  # empty queue, predicate False
    eng.step_with_batch_queue()
    chk("U17 empty-queue deferred step survives (no pop-crash)",
        fs.bitmask_lags == [0] and len(eng.batch_queue) == 1
        and fs.calls == [],
        f"bitmask_lags={fs.bitmask_lags} queue={len(eng.batch_queue)} "
        f"calls={fs.calls}")
except IndexError as e:
    chk("U17 empty-queue deferred step survives (no pop-crash)", False, repr(e))

print()
print(f"{'ALL GREEN' if not fails else 'FAILURES: ' + ', '.join(fails)}")
sys.exit(0 if not fails else 1)
