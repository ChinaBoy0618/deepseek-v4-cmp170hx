# DSpark paper — optimization-space analysis for this box

Read 2026-08-21, full 33 pages. The paper is **arXiv 2607.05147** (the
`github.com/deepseek-ai/DeepSpec/.../DSpark_paper.pdf` URL 404s — the repo
ships code + README only; README links the arXiv version). Distilled here
because the PDF is deliberately not kept in-tree. All "measured" numbers
below are from this deployment (`dsv4-a100` @5700, 4× CMP 170HX, PP4 +
DSpark spec=5, 2026-08-21 tree = 0027v2 + 0028).

## TL;DR

1. **No config change.** γ=5 fixed full verification *is* the paper's own
   production optimum for this checkpoint under light load (DSpark-5, the
   production config, is γ=5; under light load their scheduler expands the
   budget to 4–6 tokens — we are pinned at 6 = anchor+5 = that ceiling).
2. **The one unused paper mechanism:** the confidence head + hardware-aware
   prefix scheduler (Algorithm 1; §5.2 async top-K + ZOS form). The
   checkpoint ships `mtp.2.confidence_head.proj.weight` — **vLLM never loads
   it** (no `confidence` match anywhere under
   `vllm/v1/worker/gpu/spec_decode/`). It only pays off at c≳128
   (compute-saturating regime). At c≤64 the box is memory-bandwidth-bound:
   extra verify rows cost ≈0, so truncating them is a net token loss.
3. **Larger γ is a dead end with this checkpoint — do not revisit.**
   Measured 60.3 vs 98.1 tok/s at γ=7 (RESULTS.md); acceptance never
   extends past ~3 tokens. The paper's γ=16 numbers (+15–30% accepted
   length at ≤1.3% latency) require a drafter *trained* at that block size.

## Measured facts (this box)

| fact | value | source |
|---|---|---|
| spec config | `{"method":"dspark","num_speculative_tokens":5}` | SETTINGS.md |
| per-position acceptance | **0.833 / 0.637 / 0.462 / 0.323 / 0.228** | live SpecDecoding metrics, 2026-08-21 |
| mean accepted length | 3.02–3.48 of 6; avg draft acceptance 41.7–50.3% | same |
| decode, single stream | 98.1 tok/s (50.8 without DSpark) | RESULTS.md |
| aggregate @ c64 | 712.8 tok/s | RESULTS.md |
| γ=7 | 60.3 tok/s, mean acceptance 1.43–2.51 | RESULTS.md |
| draft weights in checkpoint | `mtp.{0,1,2}.*` (3 MoE layers), `mtp.2.markov_head.{markov_w1,markov_w2}`, **`mtp.2.confidence_head.proj.weight` (never loaded)** | safetensors index, /model |
| spec telemetry | engine already logs SpecDecoding metrics (metrics.py:120) — acceptance monitoring is free | docker logs |

Our acceptance-decay curve (0.83→0.23) matches the paper's **chat domain**
(Fig. 2/5), not math/code (accepted ≈5.5/5.1 there). Agent/tool-call/JSON
traffic is high-entropy like chat — relevant when choosing thresholds.

## Paper mechanism → this deployment

| paper mechanism | paper claim | our status | verdict |
|---|---|---|---|
| larger γ (§4.3.2 Fig 4) | γ 4→16: +15–30% accepted length at +0.2–1.3% round latency | needs γ-trained checkpoint; γ=7 measured worse | **dead end** |
| confidence head + prefix scheduler (§3.2, Alg 1) | +51% throughput / +60–85% TPS vs MTP-1 in production; budget 2→4–6 light, contracts under load | confidence head in checkpoint, never loaded; fixed verify=6 | **only unused lever; pays only c≳128** |
| static threshold sweep (§4.3.3 Fig 5) | chat acceptance 45.7%→95.7% as τ→0.8 | not implemented; our curve = chat domain | diagnostic stage of the lever above |
| STS calibration (§3.2.1 Fig 6) | raw ECE 3–8% (overconfident) → ~1% after STS | n/a | needed only if the scheduler is built |
| varlen execution (§5.3 flatten + marker tensor) | removes padding waste | vLLM v1 is already varlen-flattened; accepted lengths already vary per req | already have it |
| async/ZOS adaptation (§5.2) | schedule from 2-steps-prior confidences, rank-preserving top-K | our batch_queue=4 async scheduler is the same problem class | engineering path if implemented |
| RNN head (§4.3.2) | marginal over Markov, worse deployability | checkpoint is Markov-only | n/a |
| training-side (§5.1 hidden-state comm, anchor packing) | HAI-LLM internals | serving only | n/a |
| limitation: fixed draft-block cost (§5) | future difficulty-aware early exit | — | n/a |

Why dynamic budget ≈ 0 at current scale: the scheduler's win is *contracting*
the budget when verification rows compete for compute. Their fleet triggers
that at c≳150–200. Here c32×6=192 rows, c64=712.8 tok/s aggregate —
weight-bandwidth-bound (MoE fp8 + Marlin FP4), marginal row cost ≈ 0. Paper
Fig 8's light-load behavior is *expand to max* — we are already at max.
Their +51% headline is vs MTP-1; we already banked the bulk of that by
running fixed γ=5 (+1.93× vs no-spec).

## If high-concurrency serving ever matters — implementation path

Stage 0 (measure, cheap): SPS-curve knee scan at c128/c256 (RESULTS.md has
c1–c64 only). If throughput hasn't flattened by then, stop here — the lever
never pays on this hardware.

Stage 1 (diagnostic): load `confidence_head.proj` (4096→1) in the dspark
speculator's Markov loop; log predicted-vs-actual acceptance on real
traffic; pick τ by the Fig-5 method. Negligible compute.

Stage 2 (live truncation): per-req prefix cut at survival product
`a_k = ∏ c_i`, skipping low-confidence suffix rows at schedule time.
Engineering notes: the vLLM dspark path has **no per-req variable draft
count** (eagle trees do; we don't) — needs the per-req variable-length
machinery wired through `scheduled_spec_decode_tokens`, and interacts with
PP4 + batch_queue=4 exactly the way §5.2's ZOS conflict describes (schedule
from stale-but-rank-preserving confidences to keep CUDA graphs valid).
TDD-first, same workflow as 0027/0028. Include STS calibration — raw head
is overconfident (ECE 3–8%), uncalibrated thresholds mis-truncate.

Secondary synergy (minor post-0028): pruning low-confidence suffixes also
drops FSM-invalid drafts before grammar verification — fewer grammar
rejections. TYPE-B is already 0, so this is a bonus, not a driver.

## Non-paper items surfaced during the read

- Engine warns `max_num_scheduled_tokens=1920` (32 seqs × 60) vs
  `--max-num-batched-tokens 2048` — covered but with zero slack; revisit
  together with any max-num-seqs change.
- `--enforce-eager` remains forbidden (8–10 tok/s; CUDA graphs ≈12×).
