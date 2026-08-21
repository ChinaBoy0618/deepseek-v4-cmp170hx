#!/usr/bin/env python3
"""0028: fix concurrent response_format corruption (TYPE-B -> 0008 salvage).

RCA (2026-08-21, six instrumentation stages + two live A/Bs):

    Engine = AsyncScheduler (v2 model runner) + DSpark spec(5) + PP4.
    A structured request decodes every pp_size steps
    (next_decode_eligible_step, scheduler.py:593). Its drafts are proposed
    during its own batch's sample and must reach the scheduler-side
    window rewrite (update_draft_token_ids_in_output) at its NEXT batch
    -- 4 steps later. But DraftTokensHandler is a SINGLE-SLOT snapshot:
    3 intermediate batches overwrite it first (diag6: COVMISS=236/322
    deferrals). The rewrite then leaves the [-1]*5 placeholder window;
    rows after the first -1 are UNCONSTRAINED, so the sampler commits
    FSM-invalid drafts at low temperature -> TYPE-B -> 0008 salvage
    abandons enforcement -> corrupt tail (battery 17-21/24 since 0027).

    Earlier fixes kept: 0028v2 drain predicate (async ph-based
    has_structured_output_in_flight -- under rotation it correctly
    returns False; closes the theoretical stale-FSM hole) and the v1
    post-mortem (stock -1 semantics are right GIVEN real drafts; the
    missing piece was draft delivery, not the -1 rule).

Fix v3: per-req persistent draft map in DraftTokensHandler, merged
synchronously at set_draft_tokens (take-time merge would drop
snapshots on any take-cadence hiccup), returned as a UNION from
get_draft_tokens (FIFO cap 256; in_output skips reqs without a window
in the current scheduler_output, so the union is safe).

Deterministic patcher: unique anchors (present in both stock and
instrumented trees), per-edit idempotent SKIP markers, .bak backups,
in-memory compile check, md5. Rollback: --revert.
"""
import hashlib
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else "/mnt/nvme1/dsv4/vllm-c3046d1")

# ---------------------------------------------------------------------------
# v2: scheduler drain predicate under the async engine
# ---------------------------------------------------------------------------
SCHED = "vllm/v1/core/sched/scheduler.py"
ANCHOR_V2 = """        if self.use_v2_model_runner:
            return False
"""
REPL_V2 = '''        if self.use_v2_model_runner:
            # DSv4-0028: async/v2 path. num_in_flight_tokens is not maintained
            # under AsyncScheduler, but num_output_placeholders is the
            # async-native mirror of it: incremented by
            # (num_sampled_tokens_per_step + cur_num_spec_tokens) at schedule
            # time (AsyncScheduler._update_after_schedule) and decremented by
            # len(new_token_ids) (+ num_rejected for spec) at delivery, so
            # ph > this batch's own share exactly when older positions are
            # still undelivered and the grammar FSM is stale. The old
            # `return False` disabled the 0027 drain under the async engine.
            # (Under the 4-step decode rotation ph == own share at drain
            # time, so this guard is normally inert -- it closes the
            # stale-FSM hole for shallow-queue regimes.)
            for req_id in scheduler_output.num_scheduled_tokens:
                request = self.requests.get(req_id)
                if (
                    request is not None
                    and request.use_structured_output
                    and not request.is_prefill_chunk
                    and request.num_output_placeholders
                    > self.num_sampled_tokens_per_step
                    + len(
                        scheduler_output.scheduled_spec_decode_tokens.get(
                            req_id, ()
                        )
                    )
                ):
                    return True
            return False
'''

# ---------------------------------------------------------------------------
# v3: per-req draft map in DraftTokensHandler
# ---------------------------------------------------------------------------
UTILS = "vllm/v1/worker/gpu/spec_decode/utils.py"

ANCHOR_INIT = """        self.req_ids: list[str] = []
        self.draft_tokens_np: np.ndarray | None = None
        self.num_draft_tokens: int = 0
"""
REPL_INIT = """        self.req_ids: list[str] = []
        self.draft_tokens_np: np.ndarray | None = None
        self.num_draft_tokens: int = 0
        # DSv4-0028v3: per-req draft rows. Under the v2/async PP scheduler a
        # request decodes every pp_size steps (next_decode_eligible_step), so
        # its drafts (proposed in its own batch's sample) are consumed by the
        # scheduler-side rewrite only at its NEXT batch -- after pp_size-1
        # other samples replaced the single-slot snapshot. Without this map
        # every structured spec window stays the [-1] placeholder ->
        # unconstrained rows -> FSM-invalid commits -> TYPE-B -> 0008 salvage
        # -> corrupt tail (diag6: COVMISS=236/322).
        self._draft_rows_by_req: dict[str, list[int]] = {}
        self._draft_map_cap: int = 256  # FIFO bound (finished reqs linger)
"""

ANCHOR_SET = """        # For spec decoding + structured outputs, we must transfer the
        # draft tokens back to the scheduler for grammar validation.
        current_stream = torch.cuda.current_stream(self.device)
        self.copy_stream.wait_stream(current_stream)
        with torch.cuda.stream(self.copy_stream):
            self.draft_tokens_np = async_copy_to_np(draft_tokens)
            # draft_tokens is a temporary allocation on the main stream and read here on
            # copy_stream; without record_stream, the caching allocator may reuse its
            # memory before the async copy executes.
            draft_tokens.record_stream(self.copy_stream)
            self.copy_event.record()
"""
REPL_SET = ANCHOR_SET + """        # DSv4-0028v3: merge rows into the per-req map NOW, synchronously.
        # A take-time merge would drop any snapshot that is overwritten
        # before the next take (take-cadence hiccup) and silently leave
        # that request's spec window the [-1] placeholder.
        self.copy_event.synchronize()
        for _rid, _row in zip(self.req_ids, self.draft_tokens_np.tolist()):
            self._draft_rows_by_req[_rid] = _row
"""

ANCHOR_RET = """        return DraftTokenIds(self.req_ids, draft_token_ids)
"""
REPL_RET = """        if not self._draft_rows_by_req:
            # async-disabled fallback: manufactured placeholders above.
            return DraftTokenIds(self.req_ids, draft_token_ids)
        # DSv4-0028v3: return the per-req UNION. update_draft_token_ids_
        # in_output skips reqs without a window in the current scheduler
        # output, so extra entries are inert; each request finds the drafts
        # proposed at its own last sample.
        while len(self._draft_rows_by_req) > self._draft_map_cap:
            self._draft_rows_by_req.pop(next(iter(self._draft_rows_by_req)))
        return DraftTokenIds(
            list(self._draft_rows_by_req),
            [list(_r) for _r in self._draft_rows_by_req.values()],
        )
"""

EDITS = [
    (SCHED, "DSv4-0028:", ".bak-0028v2", ANCHOR_V2, REPL_V2),
    (UTILS, "DSv4-0028v3", ".bak-0028v3", ANCHOR_INIT, REPL_INIT),
    (UTILS, None, None, ANCHOR_SET, REPL_SET),
    (UTILS, None, None, ANCHOR_RET, REPL_RET),
]


def apply_one(root, rel, marker, suffix, anchor, repl):
    p = root / rel
    src = p.read_text()
    if marker is not None and marker in src:
        return f"SKIP {rel} (marker present)"
    n = src.count(anchor)
    if n != 1:
        return f"FAIL {rel}: anchor matched {n} times (want 1)"
    if suffix is not None:
        bak = p.with_name(p.name + suffix)
        if not bak.exists():
            bak.write_text(src)
    new = src.replace(anchor, repl, 1)
    compile(new, str(p), "exec")
    p.write_text(new)
    return f"OK {rel}"


if "--revert" in sys.argv:
    for rel, marker, suffix, _, _ in EDITS:
        if suffix is None:
            continue
        p = ROOT / rel
        bak = p.with_name(p.name + suffix)
        if bak.exists():
            bak.replace(p)
            print(f"reverted {rel} from {bak.name}")
    sys.exit(0)

# v3 utils edits must apply together; guard by the init marker.
p = ROOT / UTILS
if "DSv4-0028v3" not in p.read_text():
    for rel, marker, suffix, anchor, repl in EDITS[1:]:
        msg = apply_one(ROOT, rel, marker, suffix, anchor, repl)
        print(msg)
        if msg.startswith("FAIL"):
            sys.exit(1)
else:
    print("SKIP utils (v3 already applied)")

msg = apply_one(ROOT, SCHED, "DSv4-0028:", ".bak-0028v2", ANCHOR_V2, REPL_V2)
print(msg)
if msg.startswith("FAIL"):
    sys.exit(1)

for rel in {SCHED, UTILS}:
    print(rel, "md5", hashlib.md5((ROOT / rel).read_bytes()).hexdigest())
