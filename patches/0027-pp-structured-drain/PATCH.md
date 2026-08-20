# 0027 — PP structured-output drain (v2)

Port of buliaoyin/vllm-170hx-dsv4f-pp-dspark `6e959b2` (= upstream vllm#45015
queue-drain approach) to this deployment's **sync Scheduler** + batch-queue
(`step_with_batch_queue`, depth = pp_size = 4).

## Root cause being fixed

The grammar FSM only advances in `update_from_output`. Under PP the engine
keeps up to 4 batches in flight; a batch's grammar bitmask is computed at
schedule time, i.e. **before** older batches carrying the same request's
tokens have been processed → stale FSM → wrong mask (#45014). The stock
deferral machinery (pre-existing in this fork's `core.py`) pops exactly ONE
older batch before sampling — correct for pp=1, insufficient for pp>1.

## v1 post-mortem (2026-08-20, engine dead — why v2 exists)

v1 mirrored the AsyncScheduler's `num_output_placeholders` bookkeeping
verbatim. On this sync scheduler that counter is **load-bearing in six
dormant code paths** that had never seen a nonzero value (L578 max_tokens
skip, L604 `num_new_tokens` arithmetic, L735, L1425 `is_prefill_chunk`,
L1568) — activating it corrupted scheduling arithmetic (empty-content
responses, 0-14% spec acceptance), and drift vs. the stock spec-rejection
decrement (L1899) made the pending flag fire with an **empty batch queue**
→ `IndexError: pop from an empty deque` at the deferred main pop →
EngineDead. Live-crash signature is reproduced deterministically as tdd
U17.

## v2 design (sync-native criterion)

Deferral predicate: **`request.num_in_flight_tokens > this batch's own
share`** — real, exactly-mirrored bookkeeping (`+=` at schedule L1420,
`-=` per batch at delivery L1850, stale shares drained in lockstep through
preemption L1396). Strictly-greater ⇒ older, still-undelivered positions ⇒
FSM stale. `num_output_placeholders` is never touched (stays 0; all six
dormant paths stay dormant).

| file | change |
|------|--------|
| `v1/core/sched/interface.py` | `+ has_structured_output_in_flight()` default False |
| `v1/core/sched/scheduler.py` | `+` predicate impl (in-flight criterion) |
| | `+` `pending_structured_output_tokens` flag in `_update_after_schedule` (flag ONLY — no placeholder increment) |
| `v1/engine/core.py` | `+ _merge_engine_core_outputs` (fork verbatim) |
| | `+ _pop_and_process_batch` refactor (fork verbatim) |
| | guarded main pop (empty queue → `{}` instead of IndexError) |
| | drain-until-caught-up loop in the deferred branch (no post-drain assert: queue-empty exit is legitimate) |

## TDD

`bench/tdd_0027.py` — 20 checks. RED on stock (incl. baked image): U01-U08,
U14-U16b missing, U16 semantic (bitmask at lag 1), **U17 = the v1 crash**.
v1-regression guards green on stock AND v2, red on v1: U09/U12 (ph stays 0,
no settle helper). ALL GREEN in-container on v2.

`bench/apply_0027.py` — deterministic patcher (unique anchors, per-edit
markers/idempotent SKIP, `.bak-0027` backups, py_compile, md5).
Rollback: `cp <file>.bak-0027 <file>` + relaunch (or `DSV4_NO_MOUNT=1`).

## Validation (2026-08-20/21, live on 760T :5700)

- tdd_0027 20/20 ALL GREEN (live container, both runs)
- tool smoke: nonstream/stream tool_calls clean; single json_schema VALID
- regressions: consecutive_tools PASS (B 3/3, C 3/3), consecutive_stream
  PASS (D 3/3+3/3, E 3/3), issue_replay 0 leaks/0 dead, hammer 200/200
- A/B battery (`bench/battery_0027.py`, 3×8 concurrent json_schema):
  **pre-0027 17/24, 17/24 — v2 18/24** → no regression, no measurable
  concurrent gain. Residual ~25% corruption under 8-way concurrency is a
  PRE-EXISTING defect: TYPE-B grammar rejection → 0008 salvage commits the
  block **unconstrained** (log: "abandoning structured-output enforcement")
  → wrong keys / whitespace / fences. Same failure classes both stacks.
- 0025 interaction found: `DSV4_TYPEB_POLICY=finish` truncates concurrent
  json_schema to `{`-prefix (1/8 valid); reverted to default `commit`.
  The finish-mode A/B (pending per APPLIED.md) must account for this — it
  is appropriate for the DSML tool path, destructive for response_format.

## Open follow-up (0028 candidate)

Concurrent response_format corruption lives in the TYPE-B salvage path
(0008/0013), not in mask staleness. Fix direction: on TYPE-B under
response_format, don't abandon enforcement — re-validate/trim to the
FSM-valid prefix (0013 TYPE-A semantics) or grammar-filter drafts before
rejection sampling (extend 0016's `validate_tokens_ex`).

## Deployed md5s (2026-08-21 02:4x relaunch)

- `interface.py` 9dbb9cacbd526a4d175a0f723009ac0d
- `scheduler.py` 3e3c1ac34ddcdb90c2b075b7095a001b
- `engine/core.py` 6e809276adeb0cc37cadd629dd62d076
