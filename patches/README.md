# Patches

## ★ 2026-08-13: recommended base is now `c3046d1` — patch 0001 is no longer needed

Upstream's 41-commit serving-optimization campaign (base
`f8ea5bb` → `c3046d1ebd2dae9b94ad2ef5f966ea153632251e`, 2026-08-04) is worth a measured
**+7% decode (p<0.001)** on this hardware with correctness intact — see
[RESULTS](../RESULTS.md#rebase-to-c3046d1-2026-08-13), including why the "+30%" you may
have seen claimed for this range does not survive a paired A/B.

On `c3046d1`:

- **Drop `0001`** — its `has_device_capability(90)` gate is now upstream.
- **`0002 0003 0004 0005 0005a 0006 0007 0008 0009 0010 0011 0012 0013` apply unchanged, zero rejects**, in glob order. (0012 needs the 0007-0011 series present; its scheduler hunks context-match the patched tree. 0013 needs 0012 present — it rewrites 0012's grammar block.)
  (`0005a` must still precede `0006`; the glob order does that.)
- ⚠️ **The range touches `csrc/`** (`libtorch_stable/topk.cu` FilteredTopK decode routing —
  one of the real wins — plus `marlin.cu`, `custom_all_reduce.cuh`), so
  `VLLM_USE_PRECOMPILED=1` and the bind-mount method **cannot deliver the kernel changes**.
  Build from source with [docker/Dockerfile.fullbuild](../docker/Dockerfile.fullbuild)
  (sm_80-only, ~115 min) with the patches applied to the tree first.
- New env worth setting: `VLLM_MARLIN_FP8_DEQUANT_BF16=1` (upstream-adopted prefill win,
  −35 ms TTFT@8k on block-fp8 dense; inert but harmless on the INT4 repack).
  `VLLM_MARLIN_DENSE_OCCUPANCY` was refuted by its own authors (leave unset), and
  `VLLM_DSPARK_VOCAB_SHARD` has **zero consumers** at this commit.

### Getting `c3046d1` — it is unreachable by ANY git method

The branch has been force-pushed **again** (tip is now a single squashed commit with newer,
un-benchmarked work), and this time fetching all refs does not help: `c3046d1` is referenced
by nothing. GitHub still serves unreachable commits by SHA over HTTP, and the tarball
reconstructs the exact tree:

```bash
git clone https://github.com/haosdent/vllm.git && cd vllm
curl -sL -o /tmp/c3046d1.tar.gz \
  https://codeload.github.com/haosdent/vllm/tar.gz/c3046d1ebd2dae9b94ad2ef5f966ea153632251e
mkdir /tmp/c3046d1-src && tar xzf /tmp/c3046d1.tar.gz -C /tmp/c3046d1-src --strip-components=1
export GIT_INDEX_FILE=/tmp/c3046d1.index
git read-tree --empty && git --work-tree=/tmp/c3046d1-src add -Af
git write-tree   # MUST print d13ae12b9a6621ef8d218f53741e59c6db2f68d2 — the upstream tree SHA
git tag c3046d1-recon "$(git commit-tree d13ae12b9a6621ef8d218f53741e59c6db2f68d2 \
  -p f8ea5bb163c161ef38b401d055cc5fd4a934091a -m 'c3046d1 reconstructed from tarball')"
unset GIT_INDEX_FILE && git checkout -B rebase-c3046d1 c3046d1-recon
```

The `git write-tree` check is the whole safety story: if it prints the upstream tree SHA,
your working tree is byte-identical to `c3046d1`.

---

## Legacy base `f8ea5bb` (all eight patches)

Against [haosdent/vllm@dsv4-flash-a100](https://github.com/haosdent/vllm/tree/dsv4-flash-a100)
(commit `f8ea5bb`). Apply with `patch -p1` from the vLLM checkout root.

> ⚠️ **You must check out `f8ea5bb`, and a plain clone will not have it.** The branch was
> force-pushed after these patches were generated: `f8ea5bb` is no longer reachable from the
> tip, a `--depth` clone will not contain it, and the server **refuses fetch-by-SHA**.
> Fetching all refs is what makes it reachable:
>
> ```bash
> git clone --branch dsv4-flash-a100 --single-branch https://github.com/haosdent/vllm.git
> cd vllm
> git fetch origin '+refs/*:refs/remotes/all/*'
> git checkout f8ea5bb
> ```
>
> Verified end to end: from that checkout the seven patches apply in glob order with **zero
> rejects** and reproduce our live production tree byte-for-byte. (Reported in
> [#1](https://github.com/allover326/deepseek-v4-cmp170hx/issues/1).)

The container installs vLLM with `pip install -e .`, so `/vllm/vllm/...` inside the image is
live source. You can therefore apply these by **bind-mounting the patched files** instead of
rebuilding — which is what [`launch/run-pp-dspark.sh`](../launch/run-pp-dspark.sh) does.

| # | file | what | why |
|---|---|---|---|
| 0001 | `model_executor/layers/sparse_attn_indexer.py` | add the missing `has_device_capability(90)` gate to `use_persistent_topk` | ⚠️ **Precautionary — the failure it guards against does NOT reproduce on this branch.** The original report (another CMP 170HX owner, vllm#50576) was that sm_80 selects a radix top-k returning wrong indices when the candidate count falls between k and 2k — prompt length 2049–4096 at `index_topk=512`/`compress_ratio=4` — emitting fluent-looking degenerate text. **That reporter has since retracted it for `dsv4-flash-a100`, and we could not reproduce it either.** We kept the patch because it costs nothing; see [RESULTS](../RESULTS.md#-patch-0001-is-precautionary-not-a-fix-for-an-observed-bug). |
| 0002 | `config/speculative.py` | `draft_parallel_config.pipeline_parallel_size = 1` for dspark | The DSpark draft is **not** pipelined — the model runner builds it on the last PP rank only and it runs there whole. Inheriting the target's PP size makes `verify_with_parallel_config` demand `SupportsPP` from the *draft* architecture, which it neither implements nor needs. |
| 0003 | `v1/worker/gpu/pp_utils.py` | add `broadcast_draft()`, the matching receive, and sampled-token padding | This is **vLLM PR #46994**, which is not in upstream main. Without it, non-last pipeline ranks verify against a zero-initialised `req_states.draft_tokens` — acceptance near zero and corrupt output. The padding matters too: the receiver always posts a `max_sample_len`-wide buffer, so an unpadded narrow send is an element-count mismatch that deadlocks. |
| 0004 | `v1/worker/gpu/model_runner.py` | drop the dspark PP guard; call `broadcast_draft()` after `propose()`; scatter relayed draft tokens on non-last ranks | The guard covered eagle3/dflash/dspark; only dspark is enabled here — the other two are untested and their aux layers are spread across ranks rather than landing on one. |
| 0005 | `v1/worker/gpu/spec_decode/dspark/utils.py` | drop `NotImplementedError("DSpark does not support pipeline parallelism.")`; add `_has_real_weight()`; load the draft's token embedding from the checkpoint | Under PP the target's `embed_tokens` is a `PPMissingLayer` on the drafter's rank — and **aliasing one is a silent no-op, not an error**, hence the explicit check. The embedding (~1 GB) is read straight from `embed.weight` in the checkpoint, which avoids adding a cross-rank collective to model load. |
| **0005a** | `model_executor/layers/sparse_attn_indexer.py` **(must precede 0006)** | add `_prefill_topk_needs_torch_fallback()`, `_top_k_per_row_prefill_torch()`, and the prefill `if fallback / else CUDA kernel` branch | ★ **Patches 0001-0006 as first published were INCOMPLETE in two ways.** (1) Those two functions were called at four sites and defined nowhere. (2) 0006 does not *add* the fallback branch — it *rewrites* one, turning `if _prefill_topk_needs_torch_fallback():` into an `elif` and carrying `_top_k_per_row_prefill_torch(` as unchanged context — while the base `f8ea5bb` has a bare unconditional `ops.top_k_per_row_prefill(...)`. So supplying only the definitions is **not** enough. Reported by @fouvy, diagnosed by @snoby in [#1](https://github.com/allover326/deepseek-v4-cmp170hx/issues/1). Named `0005a` so a plain `patches/*.patch` glob applies it before 0006. **The fallback is ACTIVE on sm_80 by design — see below.** |
| **0006** | `model_executor/layers/sparse_attn_indexer.py` **(stacks on 0001)** | row-chunk the `[M, N]` float32 logits transient, gated by `DSV4_LOGITS_ROW_CHUNK` | ★ **The context-ceiling fix — ~134k → 1,047,736 tokens.** `fp8_mqa_logits_triton` allocates `logits = torch.empty((M, N), float32)` (`M` = prefill-chunk tokens, `N = seq_len / compress_ratio`) and hands the whole buffer to the top-k; it grows with context and is the largest allocation on the Triton fallback path. **Each row's top-k reads only its own `[ks, ke)`, so rows are independent and blocking them is exact, not approximate.** Default-OFF (`0` reproduces upstream byte-for-byte) because it is the same file as 0001 and you may want to bisect them. `256` reaches ~957,600; `128` reaches the full 1M. Costs nothing measurable — prefill 1,456 vs 1,448 tok/s at 4k, and the change is inside `if has_prefill:` so decode cannot be affected. |

## ⚠️ The sm_80 prefill top-k fallback (0005a) is load-bearing — do not stub it out

`_prefill_topk_needs_torch_fallback()` returns **True on sm_80 deliberately**, and patch 0005a
must not be reduced to `return False`.

`ops.top_k_per_row_prefill`'s histogram path (taken by rows with more than `topk_tokens`
candidates) can leave part of its dynamic-shared-memory output uninitialised and copy it out
as indices; downstream, `compute_global_topk_indices_and_lens` treats any index `>= 0` as
valid and dereferences it into the KV block table. Upstream added this torch fallback for
SM12x in [vllm#49897](https://github.com/vllm-project/vllm/pull/49897); we enable it for SM8x
too because on 4x CMP 170HX it reproduces as **`Xid 31 MMU Fault ... ACCESS_TYPE_VIRT_WRITE`
killing a worker on prefills above roughly 128k tokens** (123k passes). Disabling it will look
fine until you go deep, then kill a worker with no obvious cause.

**This is unrelated to patch 0001.** 0001's `persistent_topk` gate is precautionary and its
originating report was retracted; **that retraction does not apply to 0005a**, which fixes a
different bug in a different kernel that we did reproduce on our own cards.

Overrides: `VLLM_DSV4_PREFILL_TOPK_TORCH=0` forces the CUDA kernel back on, `=1` forces the
fallback on any architecture.

**Verified end to end:** from a pristine checkout of `f8ea5bb`, applying `0001 → 0005a → 0006`
produces **zero rejects** and reproduces our live in-production file byte-for-byte
(md5 `96380027dd74c5913b6c4aeca6b25b02`). That check is what should have run before the first
release, and it is the only reason the second attempt at this fix was caught as incomplete.

**Ordering contract:** the downstream sparse-attention kernels iterate selected KV positions
in **ascending position order**, whereas `torch.topk` returns *score* order.
`_top_k_per_row_prefill_torch` sorts accordingly and pads short rows with `-1` at the tail. A
naive mask-then-`torch.topk` reimplementation compiles and runs but feeds wrongly ordered
indices downstream.

## DSpark OOV sentinel guard (0010) — engine-death belt at the grammar boundary

DSpark can surface its draft-buffer sentinel (id == vocab_size) as a sampled
token when a degraded draft stream meets an empty grammar row. Committing it
feeds the sentinel id back into the next forward embedding lookup and kills
the worker with a device-side assert (2x on 2026-08-18, /v1/messages with
forced tools). 0010 truncates the block at the first out-of-vocab id, rolls
the suffix back, and abandons structured enforcement for the request — the
same salvage policy as 0008, one layer down.

The guard itself first shipped with a latent crash: it read
model_config.vocab_size, which c3046d1 removed in favor of get_vocab_size();
the first live trigger AttributeErrored EngineCore dead (2026-08-18 10:52,
all of /v1/messages went 500 until restart). The patch here carries the
fixed call — take it, not the pre-fix 760 checkout copy.

## Structural-tag reasoning port (0011) — port of upstream vllm#46149 (closed unmerged)

Upstream _apply_structural_tag hardcodes reasoning=False, so a thinking
request with a constrained tool_choice gets a grammar that models only the
tool-call suffix; the FSM then rejects tokens at the reasoning->tool_call
boundary. Ports all three commits of the PR: _reasoning_enabled() reads the
per-request reasoner (abstract_parser.py), thinking_enabled is exposed on
the engine adapter (adapters.py), and _grammar_from_tool_parser=True makes
enforcement start at token 0 once the tag includes reasoning. Validated
live 2026-08-18 on dsv4s: 10-arm matrix (OpenAI + Anthropic endpoints x
forced/required/auto x thinking x stream) plus 35-request unique-prompt
stress — zero FSM rejections, zero engine deaths.
## Spec-decode grammar commit invariant (0009) — port of upstream #52452 + #51870

The principled fix for the class 0008 mitigates: instead of abandoning (or
killing) after the fact, validate the accepted speculative block BEFORE it is
committed to request history. Ported verbatim from upstream PRs
[vllm-project/vllm#52452](https://github.com/vllm-project/vllm/pull/52452)
(author notes it "fixes problems on ds4 flash") and
[#51870](https://github.com/vllm-project/vllm/pull/51870), both open at port
time (2026-08-18).

- `filter_speculative_grammar_tokens()` (52452): locate the reasoning-end
  boundary inside the block (multi-token markers included), `validate_tokens()`
  the grammar part, commit only the longest grammar-valid prefix, roll back
  `num_computed_tokens` / placeholders so the rejected suffix stays schedulable
  and is resampled under an active mask. Commit invariant: request history and
  grammar advance through the same longest valid prefix — requests never die
  AND output stays schema-valid.
- Quiet post-reasoning draft probes (51870): the `grammar_bitmask` simulation
  probes drafts with the non-advancing `validate_tokens()` instead of the
  mutating `accept_tokens()`, so expected rejections neither log nor perturb
  the matcher.

0008's salvage stays as the last-resort layer for the residual class (model
degeneration loops that defeat the mask); with 0009 the post-commit rejection
becomes structurally unreachable in the transition cases. Verified live
(2026-08-18 14:20): 30x required + 5x json_schema + auto all 200; 37 organic
requests in the following 25 min with zero FSM errors, zero terminations,
zero 500s.

## Grammar salvage (0008) — FSM rejection no longer kills the request

Under `tool_choice=required` / `response_format` plus DSpark, production hit
intermittent 500s: `Failed to advance FSM` followed by the scheduler's
`Unexpected: grammar rejected tokens ... Terminating request`, killing the stream
mid-response (clients then retried into the same failure — 12 terminations in 80 s).
Decoding the rejected tokens settles it: `<｜DSML｜tool_calls` envelope fragments
repeated in a loop, and parameter names with self-invented tags — the checkpoint
degrading into malformed DSML loops under grammar constraint (same protocol-drift
root cause as 0007's leak audit). The FSM is *right* to refuse; terminating the
request is what hurts. The much more frequent "Failed to advance FSM" ERROR lines
(~60/90 min) are DSpark draft pre-advance noise the baseline already tolerates via
rollback — the branch tip (12810046) demotes exactly that log to `debug` and
nothing else, so 0008 ports that verbatim.

What 0008 adds on top of the tip's log demotion: on the scheduler-side rejection,
instead of `FINISHED_ERROR`, abandon structured-output enforcement for that
request (`use_structured_output = False`) and let it complete unconstrained. The
stream survives; 0007's lenient parser can still recover a tool call from the
degraded output. Restore stock terminating behavior with `DSV4_GRAMMAR_SALVAGE=0`.

Verified live (2026-08-18): 30x `tool_choice=required` + 5x `json_schema` + auto
all 200 with valid output after deploy; zero FSM ERROR / zero 500 in the window
after restart. Pure-Python, both files bind-mountable.

## Lenient DSML tool-calls envelope (0007) — parser-side mitigation for long-context protocol drift

Near the context ceiling the Flash checkpoint occasionally degrades its tool-call
envelope: a well-formed `<｜DSML｜invoke>`/`<｜DSML｜parameter>` body wrapped in
`<｜DSML｜_tool_calls>` (leading underscore). The stock parser treats that envelope as
literal text, so the entire call leaks into `content` — the client sees raw protocol
tags instead of `tool_calls`, and agentic sessions die on it. Observed in production:
12 leak events in 5 minutes at 143k input tokens (2026-08-18, on the c3046d1
full-build image); the same responses also contained well-formed calls that parsed
fine, and a 6-shot replay at 80k tokens reproduced the drift once at temp 0.6 and
never at temp 0 — model-side probabilistic drift, not a client or parser bug. The
official encoding README explicitly does not recover malformed output.

`0007` adds `_tool_calls` aliases for the two envelope terminals and mirrors the four
existing `TOOL_START`/`TOOL_END` state-machine transitions onto them. Nothing else
changes: the lexer is longest-literal-first, so the official `tool_calls` path is
byte-identical, and a chunked-stream parity test (the real leaked text fed in 37-char
slices) produces identical event sequences for both envelopes, ending in
`TOOL_CALL_END`. Verified live after deploy: parity holds, structured `tool_calls`
responses unchanged, DSpark acceptance unaffected.

This is a mitigation, not a cure — the drift itself is a checkpoint property. Keep
accumulated conversations compacted below ~80k tokens (see RESULTS on the
accumulated-conversation ceiling).

## Why DSpark-on-PP works at all

The model runner **already** builds the speculator on the last pipeline rank only:

```python
if self.is_last_pp_rank:
    self.speculator = init_speculator(self.vllm_config, self.device)
```

And the layer arithmetic cooperates. DeepSeek-V4-Flash has 43 layers; over PP4 they split:

```
rank 0: layers  0..10      rank 2: layers 22..32
rank 1: layers 11..21      rank 3: layers 33..42
```

DSpark taps `dspark_target_layer_ids = [40, 41, 42]` for its auxiliary hidden states, and
`lm_head` also lives on the last rank. **All three land on rank 3, together with the
drafter.** Only the token embedding (rank 0) is stranded, which patch 0005 handles.

That alignment is what makes the DSpark-on-PP work five small patches rather than a rewrite
(0006 is independent of it — it fixes the long-context ceiling). It is specific to
this model and this PP degree — a different layer count or a different `dspark_target_layer_ids`
could put the aux taps on a rank that has neither the drafter nor `lm_head`, and then the
auxiliary hidden states would need relaying across ranks too.

## Three guards, not one

Worth knowing if you port this further — they were found one at a time, and the third is the
one that costs an afternoon because its message blames the wrong model:

1. `v1/worker/gpu/spec_decode/dspark/utils.py` — `NotImplementedError: DSpark does not support pipeline parallelism.`
2. `v1/worker/gpu/model_runner.py` — `ValueError: {method} with pipeline parallel is not supported.`
3. `config/model.py` — `NotImplementedError: Pipeline parallelism is not supported for this model. Supported models implement the SupportsPP interface.` This fires at **config** time, on the **draft** architecture, and reads like a problem with the target model.

## Scheduler/worker history desync fix (0012) — root cause of the 2026-08-18 rank-3 deaths

0009/0010 truncated the accepted speculative block on the SCHEDULER side
only. The worker's input_batch had already committed the full block, so
every truncation left the two token histories permanently out of sync:
each later engine step re-sent the suffix, the spec window misaligned, and
the DSpark drafter eventually read its anchor from a stale slot of the
preallocated input buffer -- an id >= vocab_size, a device-side assert in
markov_w1/draft embedding on the drafter PP rank, and EngineDead (2x on
2026-08-18; /tmp/dsv4-crash-0818-1213.log lines 947-2330). Both upstream
PRs 0009 ported (#51870, #52452) are still UNMERGED drafts -- no upstream
fix to inherit; this is ours.

0012 removes every scheduler-side truncation/rollback path instead of
fixing the arithmetic:

- grammar block: validate-only via filter_speculative_grammar_tokens,
  then abandon enforcement (0008 salvage semantics) and commit the block
  unchanged. _kept is deliberately unused -- committing it would recreate
  the desync.
- OOV block (0010): keep detection as a tripwire, but finish the request
  with FINISHED_ERROR and commit nothing. Dropping the block is only safe
  because the request ends here.
- speculator.py (new mount): clamp the drafter anchor to
  [0, get_vocab_size()-1] right after the buffer read, the same hardening
  the fused Markov kernel already applies to its outputs. A desynced
  anchor now costs one rejected draft chain, not the worker.

Also worth knowing: the 12:10:46 xgrammar grammar_matcher.cc:612 warnings
and the acceptance-rate collapse (72% -> 32%) right before the crash were
the desync already in progress, not a separate grammar bug.

## Grammar-rejection bifurcation (0013) — closing the unconstrained-tail leak

0012 made grammar rejections survivable by committing the block unchanged and
abandoning enforcement. The canary proved the engine survives — but the cost
surfaced the same night: a Claude Code session (127k input tokens) died when
the model's output degraded into tag soup; 0012's "abandon enforcement" let
the garbage tail stream unconstrained and the client's tool-call parse
failed. Forensics + upstream triage (#43338 reports the same bleed class)
split grammar rejections into two kinds with different correct handling:

- **TYPE-A — FSM terminated inside the block.** The rejected suffix is
  post-completion garbage (the grammar already emitted its full valid
  output). 0013 keeps the valid prefix, commits it, and finishes the request
  (FINISHED_STOPPED). The request leaves the running set in the same
  iteration, so no future spec window can consume the scheduler/worker
  divergence — the same invariant that makes the stock stop-truncation
  (`del new_token_ids[num_new:]`) safe. This is a repair, not just a
  mitigation: the client receives complete schema-valid output.

- **TYPE-B — FSM still live.** Mid-stream violation, exactly what 0008's
  salvage was built for (long-context DSML degradation). Behavior unchanged
  from 0012; the only addition is a distinct "TYPE-B" log line so the real
  prevalence becomes measurable before any further tightening.

Mechanics:

- `backend_xgrammar.py`: new `validate_tokens_ex(tokens) -> (prefix,
  terminated)` — like `validate_tokens`, but breaks at FSM termination and
  reports it. The entry `_is_terminated` guard plus breaking *before*
  offering the next token also removes this path's grammar_matcher.cc:612
  warnings. (The #37506-style `_is_terminated` sync in `accept_tokens`'
  failure path was already present in this tree.)
- `__init__.py`: `filter_speculative_grammar_tokens` now returns a 3-tuple
  `(kept, rejected, terminated)`; backends without `validate_tokens_ex`
  report `terminated=False` and keep 0012 semantics.
- `scheduler.py`: the TYPE-A truncation happens before the commit, but the
  explicit FINISHED_STOPPED is applied *after* `_update_request_with_output`
  returns (stock `check_stop` owns `stopped`/truncation inside that call).
  An empty valid prefix finishes immediately, mirroring the 0012 OOV block.

Note for porters: this fork has **no** stock `is_terminated` → finish hook —
`__init__.py` only stops masking/advancing after termination, so requests
decode unconstrained to EOS/max_tokens. That is why 0013 must finish
explicitly. Upstream faces the same trade (current main kills such requests
via the accept-failure path instead of bleeding prefixes, per #43338); a
clean truncate-and-finish does not exist upstream as of 2026-08-18
(#52452/#51870/#37506 all open, zero merges in this family).

## Post-salvage damage guard (0014) — bounding the TYPE-B unconstrained tail

0013 closed the TYPE-A leak; TYPE-B keeps 0012 salvage semantics (commit
unchanged, abandon enforcement). The remaining cost of that safety choice is
the **unconstrained tail**: after salvage fires, the request generates
grammar-free until EOS/stop/max_tokens. The 2026-08-18 dead-session
post-mortem (14:50-15:05Z window in `dsv4-run-0012canary-final.log`: 16
salvage fires, all TYPE-B "1-2 of 3-6 tokens rejected", 75 salvage / 0
crashes over the full run) showed what that tail looks like under long
context: the model emits hallucinated control-tag soup (`<reference>`,
`<tool_calls>`, `<｜DSML｜invoke … string="false">`, `<text_placeholder>`,
`<dies_cmd_wrapper>` …) and the client-side tool-call parse dies.

0014 bounds exactly that tail, nothing else. Two mechanisms, armed together
at the (single, per-request) salvage point:

- **A — token budget.** `DSV4_SALVAGE_TOKEN_BUDGET` (default 64; negative
  disables). Post-salvage tokens decrement the budget; exhaustion finishes
  the request FINISHED_STOPPED with stock-stop semantics (committed tokens
  stay). Caps the damage window regardless of what the model does.
- **C' — degenerate-signature watch.** After each post-salvage token, the
  last 24 output tokens are decoded and matched against
  `_DSV4_SIG_STRINGS` — only markers that cannot occur in legitimate
  structured output (the DSML invoke special token itself is deliberately
  excluded). On hit, the in-flight signature tokens are trimmed (both
  `_output_token_ids` and `_all_token_ids`, prompt-offset aware) and the
  request finishes. Decode-based matching, not id-sequence matching, so
  context-dependent tokenization cannot evade it. `DSV4_SALVAGE_GUARD=0`
  disables.

Safety argument for the C' trim: the request finishes in the same iteration
(status set inside `_update_request_with_output`, mirroring `check_stop`),
so no later spec window ever observes the cut — the same invariant as the
stock stop truncation and 0013 TYPE-A. `Request` has no `__slots__`, so the
guard state attaches as dynamic attributes; no `request.py` change, no new
mounted file.

New log lines (distinct for measurement): `DSV4 0014 salvage-guard armed`,
`DSV4 0014 salvage-cap hit`, `DSV4 0014 degenerate-signature`.

Honest scope note: this is damage *bounding*, not a repair. Of 75 observed
salvage fires only ~1 produced client-visible failure — most tails
self-recover into valid output. A cuts the garbage volume; C' catches the
degenerate case early. Client-side parse resilience is still the other half
of the dead-session failure mode.

### 0015 — always-on degenerate tag-soup tripwire (2026-08-19, v3)

Trigger: replaying the dead-session context against the 0014 canary
(30 reqs, 80K prompt, real Bash tool, temp 0.6) reproduced the degeneration
NATURALLY — 11 tag-soup leaks and one fatal `finish=length`-no-tool-call —
with **zero** TYPE-B salvage fires and zero guard activations. The dominant
residual failure is in-context soup imitation, not the salvage path, so the
0014 post-salvage watch structurally cannot bound it. The replay also showed
the model emits `<original_output>` (not in the 0014 table).

Changes (scheduler.py only, same dynamic-attribute approach):
- Signature table += `<original_output`. All table entries are tags that
  CANNOT occur in legitimate output: the DSML tool-call wrapper
  (`｜DSML｜tool_calls` open/close) is deliberately NOT a signature —
  v2 of this patch tried a fullwidth `｜tool_calls>` entry and it
  matched every legitimate tool call, firing 40x in soak (each firing
  trimmed 1-2 legit wrapper-close tokens). Removed in v3.
- `_dsv4_sig_first_ids`: first-token id per signature for a cheap
  prefilter (id scan, no tokenizer call in the common case).
- Always-on tripwire after the append loop of
  `_update_request_with_output`, **new-token streak semantics** (v3):
  the checked window is the tokens appended THIS block plus a 16-token
  overlap; per-request per-signature counters track CONSECUTIVE blocks
  whose window contains the signature. Fire when a signature streaks
  across `DSV4_SOUP_STREAK` iterations (default 12) -> trim this
  iteration's tokens (cut floored at `_pre_len`) + FINISHED_STOPPED +
  resumable=False. Same finish-this-iteration truncation invariant as
  0013 TYPE-A / 0014 C'.
- Why v3 semantics: (a) per-window density (v1) false-positives on
  legitimately writing the signature table into patches; (b) a fixed
  tail window (v2) lets a single legit tag parked at the END of the
  output hold the window until the streak fires — the new-token window
  freezes a legit end tag at <=4 while soup (tags in every block for
  hundreds of tokens) reaches 12. `DSV4_SOUP_TRIPWIRE=0` disables.

Validation: sustained-soup induction fires at exactly streak 12 (trimmed
6 tokens, FINISHED_STOPPED, clean prefix delivered); tuple-echo of the
signature table does NOT fire (natural stop, content intact); dead-session
replay post-0015: 0 fatal / 30, tool calls 30/30, residual 2 short
self-recovering bursts correctly left untouched.

New log line: `DSV4 0015 soup-tripwire`.

Scope honesty: converts the fatal burn-the-whole-budget soup case into a
clean early stop and bounds soup leakage to the streak window (~30-60
tokens). It does not stop the model from *starting* to imitate soup in a
poisoned context; that is a context-hygiene problem, not a server bug.

### 0016 — draft-window FSM overfeed elimination (2026-08-19, round-1 fix)

Trigger: the R1 context-size matrix (4.7K -> 285K prompt tokens, 4 arms,
60/60 client-pass) still logged `grammar_matcher.cc:612` ("matcher has
terminated ... trying to accept new token id 1") before nearly every
0013 TYPE-A line. Root cause: the stock draft-filter sites
(`scheduler.py update_draft_token_ids` / `update_draft_token_ids_in_output`)
validate the WHOLE draft block with the old `validate_tokens`, which keeps
offering tokens after the FSM accepted its stop token mid-block; the
backend `accept_tokens` inner loop had the same hole. Harmless to output
(0013 truncates at commit) but one C++ warning + wasted matcher work per
tool-call request.

Changes:
- `backend_xgrammar.py accept_tokens`: break the loop as soon as the FSM
  accepts its stop token — post-stop tokens are definitionally garbage.
- `scheduler.py` both draft-filter sites: probe with `validate_tokens_ex`
  (termination-aware, same accept+rollback semantics) when available,
  falling back to stock `validate_tokens` on other backends.

Expected effect: 612 warnings ~0 on normal tool-call traffic; TYPE-A
truncations unchanged (that is the correct commit-time mechanism).

Validation (0016 canary): ctx matrix 60/60, hammer 200/200, 612 count 0,
TYPE-A still present and correct, induced-soup tripwire still fires,
tuple-echo still passes.

### 0017 — soup signature extension + cap-hit clean boundary (2026-08-19, issue-fix P0)

Trigger: the two 2026-08-19 issue sessions (`issues/01` 73db6fa4 XML pseudo-tag
leak, `issues/02` 1cff1626 meta-tag forge) showed the 0015 signature table
blind to a whole new generation of hallucinated tags — in the issue-1 window
TYPE-B salvage fired 30x with 8 cap-hits, while `degenerate-signature` and
`soup-tripwire` stayed at **0** (`<Write `, `<bash_command`, `<call Bash`,
`<answer>`, `<analyze>`, `<thinking>` all leaked through). The `</Bash>` mid-tag
hard stop was our own 0014 cap-hit landing mid-tag.

Changes (TDD: `tdd_0017.py` U03/U10/U14/U20-U28 RED -> GREEN):
- **P0-a**: 9 new signatures in `_DSV4_SIG_STRINGS` — `<Write `, `<bash_command`,
  `<call `, `<answer>`, `<analyze>`, `<thinking>`, `</assistant>`,
  `<assistant_unitsummary>`, `<system-reminder>`. All output-side-only
  hallucinations, zero intersection with the DSML legit syntax set and
  `<think>`/`</think>` (the 0015-v2 lesson); trailing space on `<call `/`<Write `
  guards `<callable`/`<Writer`; one-shot legit mentions survive streak-12.
- **P0-b**: new `Scheduler._dsv4_clean_cut(out_tail, decode, max_back=16)` —
  when the 0014 salvage-cap hit lands inside a half-written tag, back the cut up
  to the last unclosed `<` (bounded 16 tokens; `a < b` prose/math never
  triggers; closed tags untouched). Wired into the cap-hit branch before
  FINISHED_STOPPED with the same finish-this-iteration trim invariant as
  0013 TYPE-A / 0014 C'.

New log lines: `DSV4 0017 salvage-cap clean-cut`.

Validation: unit suite 19/19 in-container (real Scheduler + real tokenizer);
live poisoned-replay pre-fix tripwire-delta 0 / 3-of-20 requests leaked 5+
pseudo-tags uncut -> post-fix fires + tails cut; legit-literal probes
no-false-kill; hammer regression 200/200.

### 0018 — `<thinking>` hallucinated-variant absorption (2026-08-19, issue-fix P1-a)

Trigger: issue-1 session 73db6fa4 showed `<thinking>`/`</thinking>` 99/96
times in VISIBLE text — the deepseek_v4 reasoning parser only knows
`<think>`/`</think>`, so the hallucinated variant flowed into content.

Changes (TDD: `tdd_0018.py` V1-V4 RED -> GREEN, V5-V7 stay GREEN):
- `vllm/parser/deepseek_v4.py`: new terminals `THINK_START_V`/`THINK_END_V`
  (`<thinking>`/`</thinking>`, text + token-id) with pure-absorb transitions
  in CONTENT and REASONING states (no events, no state change).
- **Strip-only by design**: an unclosed variant inside a legit document
  cannot route the rest of the output into reasoning_content (the trap that
  routing-to-reasoning would create); inner text stays visible, tags vanish.
  Cross-pairs (`<think>` ... `</thinking>`) are handled since both closes
  are recognized.
- The 0017 scheduler-side `<thinking>` signature stays as the soup backstop
  (parser strips in normal traffic; tripwire kills 12-streak soup).

### 0019 — line-repetition tripwire (2026-08-19, issue-fix P1-c)

Trigger: issue-2 session 1cff1626 — "Lets reload."x60 inside ONE message
(msg#750) with zero tool_use. Cross-message loops are client-domain, but
the single-message repetition is server-side stoppable.

Changes (TDD: `tdd_0019.py` R00-R09):
- `scheduler.py`: `_dsv4_rep_lines()` — a short line (<=6 tokens, <=28 chars
  prefilter) appearing >=5x in the new-token decode window; per-request
  consecutive-block streak fires at `DSV4_REP_STREAK=6` -> drop this block's
  tokens (floored at `_pre_len`, same finish-this-iteration invariant) +
  FINISHED_STOPPED. Separator whitelist (`---`, `===`, ...); long lines never
  flagged; 4 repeats never flagged. `DSV4_REPETITION_TRIPWIRE=0` disables.
- New log line: `DSV4 0019 rep-tripwire`.

Residual (honest boundary): cross-message loops (752 announce-only messages
across turns) remain client-domain — the server cannot see cross-request
session state.

### 0017v2 — soup tripwire cumulative totals (2026-08-19, live-TDD finding)

The first live run of the extended table exposed a semantics gap: the issue-1
replay emits pseudo-tags SPARSELY (~1 per 50 tokens), and the 0015
consecutive-streak rule (same signature in the 22-token window 12 blocks in a
row) can never saturate — window residency per occurrence is only ~3 blocks.
Table hits with zero fires.

- `scheduler.py`: per-request `_dsv4_sig_totals` cumulative detection-block
  counters; fire when `streak >= 12 OR total >= DSV4_SOUP_TOTAL (18)`
  (~6-8 sparse occurrences; legit discussion docs with <=4 mentions stay
  far below). Log line now reports `streak N total M`.
- TDD: `tdd_0017.py` U15 (12 sparse occurrences fire) / U16 (4 legit
  mentions never fire) / U17 (one-shot never fires).
- Apply order note: 0017 -> 0018 -> 0019 -> 0017v2 (v2 was written after
  0019 on the live tree; patch boundaries follow the backup chain
  `.bak-0019`/`.bak-0017v2`).

### 0019v2 — repetition window floor (2026-08-19, live-TDD finding)

Same lesson as 0017v1, found in the issue-2 replay: `_dsv4_rep_lines` used
the 0015-style window (`new_token_ids + 16` overlap ~= 21 tokens at spec=5).
A repeated short line is ~4-6 tokens, so 5 occurrences need 25-30 tokens —
and the sliding window boundary truncates one occurrence mid-line, capping
the count at 4. Unit suite was green (big blocks) but the tripwire was DEAD
in production: i2 replay produced dup>=5 responses with zero fires.

- `scheduler.py`: window floor `max(new+16, DSV4_REP_WINDOW=160)` so the
  count sees a stable trailing region; streak-6 rule unchanged. Legit
  parked duplicates stop firing as soon as the line scrolls out (same
  semantics as 0015 soup window).
- TDD: `tdd_0019v2.py` — W02 (exact live shape: 5-token decode blocks,
  consecutive repeats fire) RED on v1, GREEN after; W03-W07 guards (3-4
  sparse/interleaved mentions, separators, long lines, table rows) stay
  green before AND after.

### Issue-replay validation + crash incident (2026-08-19 11:42)

User-directed verbatim replay (`bench/tdd_issue_replay.py`): issue-1
pseudo-tag transcript and issue-2 Lets-reload loop through the live
service at temp 1.0.

Results on the 0017v2+0018+0019 stack:
- think_leak = 0 everywhere (0018 strip works live, incl. tools group).
- 0017v2 cumulative soup fired twice correctly (`</assistant>` total 18,
  `<answer>` total 18; trimmed 2-3 tokens, FINISHED_STOPPED).
- **Crash 11:42:26** during i1 round 1 request #11: `Out-of-vocab token
  129280 (=vocab_size)` — the 0010 guard firing for the FIRST time in
  container history — then PP3 (drafter rank) CUDA device-side assert ->
  EngineDead, container exit. Same class as the 08-18 crash (degraded
  DSpark draft stream surfaces the sentinel token; scheduler guard errors
  the request but the worker-side embedding lookup is already in flight).
  None of the 0017-0019 fire paths executed near the crash; they touch
  neither drafter nor sampler. Full log: 760:/tmp/dsv4-crash-0819-1142.log.
- Reproduction: NOT reproduced — after restart, i1 x3 (36 req incl. the
  same request index), i1t (12), i2 (12) all clean; zero Out-of-vocab,
  zero crash. Classified stochastic residual of the known drafter hole;
  hammer 200/200 re-verified post-restart. Hardening (worker-side clamp)
  stays on the 0012 backlog as 0020 candidate if it recurs.
