# 0028 — structured-output draft coverage under the async PP scheduler

Fixes the concurrent `response_format`/`json_schema` corruption that survived
0027v2 (battery 17-21/24 since the 0027 port; every BAD response traced to a
TYPE-B "Grammar rejected N of 6 speculative tokens" → 0008 salvage disables
enforcement → corrupt tail). After 0028: **24/24 twice, TYPE-B 0, salvage 0**.

Files: `vllm/v1/worker/gpu/spec_decode/utils.py` (+ 0028v2 guard in
`vllm/v1/core/sched/scheduler.py`), both already in the launch mount list.
Apply: `python3 bench/apply_0028.py /mnt/nvme1/dsv4/vllm-c3046d1`
(idempotent, `.bak-0028v2`/`.bak-0028v3` backups, in-memory compile check).
Rollback: `--revert`. TDD: `bench/tdd_0028.py` (19 checks, container-run;
Parts V/G/H).

## Root cause (six instrumentation stages, two disproven fixes)

The engine runs **AsyncScheduler** (v2 model runner; core.py:169 factory —
grep for `AsyncScheduler(` misses it) + DSpark spec(5) + PP4, batch_queue
depth 4. Under this scheduler a request decodes every `pp_size` steps:
`next_decode_eligible_step = current_step + pp_size`
(async_scheduler.py:46-49, enforced at scheduler.py:593).

A structured request's spec window must be rewritten with its real drafts by
`update_draft_token_ids_in_output` (deferred branch of
`step_with_batch_queue`) *before* `get_grammar_bitmask` samples. Those drafts
are proposed during the request's **own** batch's sample and consumed at its
**next** batch — 4 steps later. But `DraftTokensHandler` is a **single-slot
snapshot**: `set_draft_tokens` replaces `req_ids`/`draft_tokens_np` every
sample, so 3 intermediate batches overwrite a request's drafts before the
rewrite runs.

diag6 evidence (env-gated logs, `bench/apply_0028dbg.py`, reverted after RCA):
`COVMISS=236/322` deferrals — the taken `DraftTokenIds` never covered the
deferred batch's rotating request subset. Uncovered windows stay the
`[-1]*5` placeholder set by AsyncScheduler (`_spec_token_placeholders`), and
stock `grammar_bitmask` leaves every row after the first `-1` **unconstrained**
(full mask). At low temperature the drafter mimics the target, so
FSM-invalid drafts are committed → TYPE-B → 0008 salvage → corrupt tail.
Single-request needle tests never caught it: one request keeps its drafts in
the single slot (producer→consumer is adjacent), so only 8-way concurrency
(rotated groups) triggers the loss.

Chain in full:
`rotation (4 steps) + single-slot handler → rewrite misses the req → [-1]
placeholder window → rows after -1 unconstrained → FSM-invalid commit →
TYPE-B → 0008 salvage abandons enforcement → one bad token ruins the
response` (corruption shapes: `"city1"`, `"age" :`, `"age", 34`, `"": 34`,
`"\tElena"`, leaked ```json fences — all single-token insertions at
state-strict positions).

## Fix (v3): per-req persistent draft map

`DraftTokensHandler` gains `_draft_rows_by_req: dict[str, list[int]]`
(FIFO cap 256 — finished reqs are never evicted explicitly):

- `set_draft_tokens` merges rows into the map **synchronously** after the
  D2H copy event (µs-scale; plain batches still return early before the
  copy, so the plain path is untouched). A take-time merge would drop any
  snapshot overwritten before the next take.
- `get_draft_tokens` returns the **union** of the map (the async-disabled
  `[-1]` manufacture fallback is kept for an empty map).
  `update_draft_token_ids_in_output` iterates `(req_id, row)` and skips reqs
  without a window in the current scheduler_output, so extra entries are
  inert; each request finds exactly the drafts proposed at its own last
  sample — which is what its next batch verifies, since the FSM advanced
  through that sample's deliveries in between.

## Also in 0028 (kept)

- **v2 drain guard** (`scheduler.py has_structured_output_in_flight`): the
  0027 port early-returned `False` under `use_v2_model_runner`. Now uses the
  async-native `num_output_placeholders` criterion: ph incremented by
  `(num_sampled_tokens_per_step + cur_num_spec_tokens)` at schedule,
  decremented by `len(new_token_ids)` (+ rejected) at delivery, so
  `ph > own share` ⇔ older undelivered positions ⇔ stale FSM. Under the
  4-step rotation ph == own share at drain time (a req's older batch has
  always left the queue), so this is normally inert — it closes the
  stale-FSM hole for shallow-queue regimes (diag6: all 322 drains qlen=3
  inflight=False).

## Post-mortems (kept for the record)

- **v1 (disproven live):** keep rows after the first `-1` constrained at the
  last-valid FSM state. RED→GREEN in TDD, then **4/24 live** — frozen-state
  rows force the same greedy token at successive positions when the
  window/state is misaligned → doubled tokens (`"namename"`, `{{`). Proof
  that stock `-1` semantics are correct *given real drafts + caught-up FSM*;
  Part G of tdd_0028 now pins stock semantics as a regression guard.
- **v2 (insufficient):** the drain predicate above. Correct but inert under
  rotation — the FSM staleness it fixes is not the failure mode; battery
  stayed 17/24.

## Validation (prod @5700, 2026-08-21)

| suite | result |
|---|---|
| bench/battery_0027.py (24× json_schema, 8-way) | **24/24** ×2 (ab0028v3, ab0028v3b); diag-era baseline 17-21/24 |
| engine tags during battery | TYPE-B 0, COVMISS 0, salvage 0, Traceback 0 (diag6: 6-8/6-8/8) |
| tdd_0028 (container) | 19/19 GREEN on clean tree (RED before: H01/H03 on stock handler, V01/V03/V07 on stock predicate) |
| tdd_0027 (container) | ALL GREEN |
| tdd_consecutive_tools / _stream | PASS (C 3/3, E 3/3) |
| hammer_0014 | 200/200 ALL GOOD (335 s) |
| tdd_issue_replay | deltas 0 (TYPE-B/tripwires/Traceback); dup5 0, think_leak 0 |

All temporary instrumentation (TYPEB-DBG/W1-W4/SRC, 0028dbg IMM/TAKENONE/
COVMISS/DEF, request.py spec_token_ids setter-trace) reverted; final tree =
0027v2 + 0028 only. Verified by marker grep + TDD rerun on the clean tree.
