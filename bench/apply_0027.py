#!/usr/bin/env python3
"""Apply patch 0027 v2 (PP structured-output drain) to the vllm-c3046d1 tree.

Port of buliaoyin/vllm-170hx-dsv4f-pp-dspark 6e959b2 (= upstream vllm#45015's
queue-drain approach), ADAPTED to the sync Scheduler this deployment runs.

v2 (post-mortem of the 0820 v1 EngineDead): the deferral criterion is
    num_in_flight_tokens > this batch's own share
— real, exactly-mirrored bookkeeping (+= at schedule, -= per batch at
delivery, stale shares drained in lockstep through preemption). v1 used
num_output_placeholders, which is load-bearing in six dormant sync-scheduler
paths that had never seen a nonzero value here; activating it corrupted
scheduling arithmetic, and spec-rejection drift deferred batches with an
EMPTY queue -> IndexError pop-from-empty-deque at the deferred main pop.

Edits:
  interface.py  + has_structured_output_in_flight() default False
  scheduler.py  + predicate impl (num_in_flight criterion)
                + _update_after_schedule: pending_structured_output_tokens
                  flag only — NO placeholder increment (ph stays 0)
  core.py       + _merge_engine_core_outputs
                + _pop_and_process_batch refactor
                + guarded main pop (empty queue -> {} instead of crash)
                + drain-until-caught-up loop in the deferred branch
                  (no post-drain assert: queue-empty exit is legitimate)

Usage (on 760T):
    python3 apply_0027.py /mnt/nvme1/dsv4/vllm-c3046d1

Safety: every anchor must match exactly once; .bak-0027 backup beside each
file; refuses double-apply (per-edit markers); py_compile on every edit;
prints md5s. Rollback: cp <file>.bak-0027 <file>
"""
import hashlib
import py_compile
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else "/mnt/nvme1/dsv4/vllm-c3046d1")

# ---------------------------------------------------------------- interface
IFACE_METHOD = '''    def has_structured_output_in_flight(
        self, scheduler_output: "SchedulerOutput"
    ) -> bool:
        """Returns True if any structured-output request scheduled in
        `scheduler_output` still has a previously scheduled token in flight,
        i.e. its grammar FSM has not yet caught up. Used under pipeline
        parallelism to decide whether in-flight batches must be drained before
        computing the next grammar bitmask. Defaults to False for schedulers
        that do not track in-flight output tokens."""
        return False

'''

# ---------------------------------------------------------------- scheduler
PREDICATE = '''    def has_structured_output_in_flight(
        self, scheduler_output: SchedulerOutput
    ) -> bool:
        # DSv4-0027: port of buliaoyin 6e959b2 / vllm#45015. True while a
        # structured request scheduled in `scheduler_output` still has token
        # positions in older in-flight batches: its grammar FSM only advances
        # in update_from_output, so a bitmask built now would be stale and
        # wrong under PP (batch_queue depth = pp_size = 4).
        #
        # Criterion: num_in_flight_tokens > this batch's own share. It is
        # incremented at schedule time and decremented per batch at delivery,
        # exactly mirrored, and carries stale shares through preemption in
        # lockstep — so strictly-greater means older, still-undelivered
        # positions. num_output_placeholders is deliberately NOT used: the
        # sync scheduler never increments it (async-only), and waking it
        # changes dormant scheduling arithmetic (num_new_tokens,
        # is_prefill_chunk, max_tokens skip) that has never run nonzero here.
        if self.use_v2_model_runner:
            return False

        for req_id, own_share in scheduler_output.num_scheduled_tokens.items():
            request = self.requests.get(req_id)
            if (
                request is not None
                and request.use_structured_output
                and not request.is_prefill_chunk
                and request.num_in_flight_tokens > own_share
            ):
                return True
        return False

'''

AFTER_SCHED_OLD = """            scheduler_output.has_structured_output_requests |= (
                request.use_structured_output and not request.is_prefill_chunk
            )
"""

AFTER_SCHED_NEW = """            scheduler_output.has_structured_output_requests |= (
                request.use_structured_output and not request.is_prefill_chunk
            )
            # DSv4-0027: defer grammar-bitmask sampling while older batches
            # still carry this request's positions (its FSM is stale until
            # they are processed; a bitmask built now would be wrong under
            # PP, #45014/#45015). num_in_flight_tokens already includes THIS
            # batch's share (incremented above), so strictly-greater means
            # older undelivered positions. Sync-native: no placeholder
            # bookkeeping (num_output_placeholders stays 0 — six other
            # scheduler paths read it and have never seen it nonzero here).
            scheduler_output.pending_structured_output_tokens |= (
                request.use_structured_output
                and not request.is_prefill_chunk
                and request.num_in_flight_tokens > num_scheduled_token
            )
"""

# ---------------------------------------------------------------- engine core
MERGE_FN = '''def _merge_engine_core_outputs(
    dst: dict[int, "EngineCoreOutputs"], src: dict[int, "EngineCoreOutputs"]
) -> None:
    # DSv4-0027: port of buliaoyin 6e959b2 / vllm#45015 — merge the drained
    # batches' outputs into the step's result.
    for client_index, out in src.items():
        cur = dst.get(client_index)
        if cur is None:
            dst[client_index] = out
            continue
        cur.outputs.extend(out.outputs)
        if out.finished_requests:
            if cur.finished_requests:
                cur.finished_requests |= out.finished_requests
            else:
                cur.finished_requests = out.finished_requests
        if out.scheduler_stats is not None:
            cur.scheduler_stats = out.scheduler_stats


'''

POP_METHOD = '''    def _pop_and_process_batch(
        self,
        batch_queue: (
            "deque[tuple[Future[ModelRunnerOutput], SchedulerOutput, Future[Any]]]"
        ),
    ) -> dict[int, EngineCoreOutputs]:
        """DSv4-0027: pop the oldest in-flight batch, wait for its result, and
        update the scheduler from it (refactored out of step_with_batch_queue
        so the PP structured-output drain can reuse it)."""
        future, scheduler_output, exec_model_fut = batch_queue.pop()
        with (
            self.capture_iteration_details(scheduler_output) as iteration_details,
            self.log_error_detail(scheduler_output),
        ):
            model_output = future.result()
            if model_output is None:
                # None from sample_tokens() implies that the original execute_model()
                # call failed - raise that exception.
                exec_model_fut.result()
                raise RuntimeError("unexpected error")

        # Before processing the model output, process any aborts that happened
        # during the model execution.
        self._process_aborts_queue()
        engine_core_outputs = self.scheduler.update_from_output(
            scheduler_output, model_output
        )
        self._attach_iteration_details(engine_core_outputs, iteration_details)
        return engine_core_outputs

'''

POPBLOCK_OLD = """        # Block until the next result is available.
        future, scheduler_output, exec_model_fut = batch_queue.pop()
        with (
            self.capture_iteration_details(scheduler_output) as iteration_details,
            self.log_error_detail(scheduler_output),
        ):
            model_output = future.result()
            if model_output is None:
                # None from sample_tokens() implies that the original execute_model()
                # call failed - raise that exception.
                exec_model_fut.result()
                raise RuntimeError("unexpected error")

        # Before processing the model output, process any aborts that happened
        # during the model execution.
        self._process_aborts_queue()
        engine_core_outputs = self.scheduler.update_from_output(
            scheduler_output, model_output
        )
        self._attach_iteration_details(engine_core_outputs, iteration_details)
"""

POPBLOCK_NEW = """        # Block until the next result is available. DSv4-0027: the guard is
        # defensive — the deferred branch reaches this pop without having
        # appended its own batch, and must not crash the engine if the queue
        # is already empty (0820 v1 incident: IndexError pop-from-empty-deque).
        engine_core_outputs = (
            self._pop_and_process_batch(batch_queue) if batch_queue else {}
        )
"""

DRAIN_OLD = """        if deferred_scheduler_output:
            # When draft tokens are used with structured output, validate them
"""

DRAIN_NEW = """        if deferred_scheduler_output:
            # DSv4-0027: PP + structured outputs — the single result processed
            # above only advances each structured request's grammar FSM by
            # ~one step. Fine when pp_size == 1 (the FSM lags by at most one
            # in-flight token), but with pp_size > 1 the previous token can be
            # several batches deep, so the FSM would still be stale and the
            # bitmask wrong. Drain the queue until those requests are caught
            # up (#45014). Exiting because the queue is EMPTY is legitimate
            # (trivially caught up), so there is no post-drain assert.
            while batch_queue and self.scheduler.has_structured_output_in_flight(
                deferred_scheduler_output
            ):
                _merge_engine_core_outputs(
                    engine_core_outputs,
                    self._pop_and_process_batch(batch_queue),
                )
            # When draft tokens are used with structured output, validate them
"""


def edit(rel: str, old: str, new: str, marker: str) -> None:
    p = ROOT / "vllm" / rel
    src = p.read_text()
    if marker in src:
        print(f"SKIP {rel} (marker already present)")
        return
    bak = p.with_name(p.name + ".bak-0027")
    if not bak.exists():
        bak.write_text(src)
    n = src.count(old)
    assert n == 1, f"{rel}: anchor matched {n} times (want 1):\n{old[:200]}"
    p.write_text(src.replace(old, new, 1))
    py_compile.compile(str(p), doraise=True)
    md5 = hashlib.md5(p.read_bytes()).hexdigest()
    print(f"OK {rel}  md5={md5}")


def insert_before(rel: str, anchor: str, text: str, marker: str) -> None:
    edit(rel, anchor, text + anchor, marker)


# --- interface.py: default predicate before the pause_state property ---
insert_before(
    "v1/core/sched/interface.py",
    "    @property\n    @abstractmethod\n    def pause_state(self) -> PauseState:",
    IFACE_METHOD,
    "def has_structured_output_in_flight(",
)

# --- scheduler.py: predicate implementation ---
insert_before(
    "v1/core/sched/scheduler.py",
    "\n    def reset_prefix_cache(",
    "\n" + PREDICATE,
    "def has_structured_output_in_flight(\n        self, scheduler_output: SchedulerOutput",
)

# --- scheduler.py: pending flag in _update_after_schedule (flag only, no
#     placeholder increment — ph must stay 0 on the sync path) ---
edit(
    "v1/core/sched/scheduler.py",
    AFTER_SCHED_OLD,
    AFTER_SCHED_NEW,
    "pending_structured_output_tokens |= (",
)

# --- core.py: module-level merge helper after the _R TypeVar ---
edit(
    "v1/engine/core.py",
    '_R = TypeVar("_R")  # Return type for collective_rpc\n',
    '_R = TypeVar("_R")  # Return type for collective_rpc\n\n\n' + MERGE_FN,
    "def _merge_engine_core_outputs(",
)

# --- core.py: _pop_and_process_batch before step_with_batch_queue ---
insert_before(
    "v1/engine/core.py",
    "\n    def step_with_batch_queue(",
    "\n" + POP_METHOD,
    "def _pop_and_process_batch(",
)

# --- core.py: inline pop block -> guarded _pop_and_process_batch call ---
edit(
    "v1/engine/core.py",
    POPBLOCK_OLD,
    POPBLOCK_NEW,
    "# Block until the next result is available. DSv4-0027:",
)

# --- core.py: drain loop in the deferred branch ---
edit(
    "v1/engine/core.py",
    DRAIN_OLD,
    DRAIN_NEW,
    "while batch_queue and self.scheduler.has_structured_output_in_flight(",
)

print("\n0027 v2 applied cleanly to all 3 files.")
