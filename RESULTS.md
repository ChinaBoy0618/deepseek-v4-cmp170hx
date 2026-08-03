# Measured results

Hardware: **4× NVIDIA CMP 170HX** (GA100, sm_80, VRAM-unlocked to 65,536 MiB, PCIe Gen2 x4,
no P2P, 180 W power cap). Model: `deepseek-ai/DeepSeek-V4-Flash-0731`, original checkpoint
(MXFP4 experts + FP8 e4m3 block-quantised attention, ~284B total / ~13B active).

All numbers are end-to-end wall clock measured from a client, prefix caching **off**, warm-up
request discarded. Harnesses are in [bench/](bench/).

---

## Headline

| | plain | **+ DSpark** |
|---|---|---|
| decode, single stream (3-content aggregate) | 50.8 | **98.1 tok/s** |
| prefill @ 77k context | 5,272 | 5,207 tok/s |
| aggregate decode @ 64 concurrent | 472.0 | **712.8 tok/s** |
| verified context | **1,047,736 tokens** | **1,047,736 tokens** |

---

## Decode by content type

Speculative decoding's benefit is strongly content-dependent — a single prompt is not a
measurement. 400 tokens each, temperature 0.

| prompt type | PP4 plain | PP4 + DSpark | ratio |
|---|---|---|---|
| technical exposition | 44.0 | 86.9 | 1.98× |
| open-ended prose | 55.1 | 93.9 | 1.70× |
| code generation | 55.1 | 118.8 | 2.16× |
| **aggregate** | **50.8** | **98.1** | **1.93×** |

Mean acceptance length 3.03 of a possible 6; per-position acceptance
0.730 / 0.569 / 0.372 / 0.226 / 0.131.

## Concurrency — DSpark keeps winning under load on PP

| concurrent requests | 1 | 4 | 8 | 16 | 32 | 64 |
|---|---|---|---|---|---|---|
| PP4 plain | 50.5 | 133.0 | 173.9 | 288.2 | 393.3 | 472.0 |
| **PP4 + DSpark** | **58.3** | **197.9** | **269.3** | 302.3 | **429.2** | **712.8** |

This is the opposite of the tensor-parallel behaviour, where DSpark went *negative* above
about 8 concurrent (TP, c=16: DSpark 212 vs plain 289). Pipeline parallel leaves bubbles the
drafter can fill; tensor parallel does not.

## Prefill — PP scales with context, TP does not

Single request, warm, `--max-model-len 131072`.

| prompt tokens | 1,544 | 3,082 | 6,159 | 12,313 | 24,621 | 50,006 | 76,929 |
|---|---|---|---|---|---|---|---|
| **PP4** | 1,966 | 2,706 | 3,660 | 4,524 | 5,113 | **5,321** | 5,272 |
| **TP4** | 908 | 774 | 841 | 809 | 804 | 776 | 801 |
| PP/TP | 2.2× | 3.5× | 4.4× | 5.6× | 6.4× | **6.9×** | 6.6× |

TP is flat across a 50× range of context lengths. See [SETTINGS.md](SETTINGS.md) for why.

## Decode vs context — the speedup does not decay

| prompt tokens | 2k | 8k | 32k | 65k | 100k |
|---|---|---|---|---|---|
| PP4 plain | 48.5 | 45.8 | 43.7 | 41.2 | 38.8 |
| **PP4 + DSpark** | **117.8** | **129.3** | **99.7** | **99.5** | **90.0** |
| ratio | 2.4× | 2.8× | 2.3× | 2.4× | 2.3× |

Time to first token is identical between the two (0.78 s → 14.8 s), i.e. DSpark costs
nothing on prefill. Acceptance holds at 3.5–3.7 at long context.

## Speed vs context

Measured to the top of the model's range, after the [context-ceiling fix](#-context-ceiling--solved).
One harness, identical method at every point.

| real context | prefill tok/s | TTFT | decode tok/s |
|---|---|---|---|
| ~7,700 | — | 2.1 s | **88.7** |
| ~100,000 | — | 22.1 s | 79.0 |
| ~200,000 | 4,486 | 50.5 s | 67.7 |
| ~385,000 | 3,425 | 120.4 s | 54.5 |
| ~769,000 | 2,525 | 334–336 s | 39.6 / 43.6 |
| **~1,040,000** | **1,904** | **544–550 s** | **35.6** |

**Decode degrades gracefully: 88.7 → 35.6 tok/s, i.e. it retains 40% of its short-context rate
across a 135× context increase.** Generating at a full million tokens of context is still
faster than most people read.

The 769k row shows two independent runs (39.6 and 43.6) — DSpark is
[not deterministic](#-dspark-output-is-not-reproducible), so treat single decode numbers as
±10%.

**Prefill is the expensive half, not decode.** At 1M you wait ~9 minutes for the first token
and then generate at 35.6 tok/s. Budget accordingly: this is a batch/document tool at the top
of its range, not an interactive one.

⚠️ **These decode absolutes are a worst case.** Every prompt here is a random-word haystack,
and DSpark acceptance on random text is poor — prose and code reach 90–130 tok/s at short
context (see [Decode by content type](#decode-by-content-type)). The *shape* is trustworthy;
the absolute numbers are pessimistic.

### Why prefill DECAYS with context — and why the section above says it RISES

Both are true at different scales. The rising curve (1,966 → 5,321 tok/s) was measured
**1.5k → 77k** and stopped there, because the bug killed everything past ~150k.

- **Rising, ≤25k:** fixed per-chunk and pipeline fill/drain costs amortise. Plateau ~5,300.
- **Falling, ≳200k:** sparse attention keeps *attention* cheap — top-k selects a fixed 512
  blocks at any length — but **the indexer that chooses them scores every compressed key**,
  `M × N` with `N = seq_len / compress_ratio`. Confirmed directly: a 121,582-token prompt logs
  `N = 30,395`, exactly 121,582/4. Per-chunk cost therefore grows linearly with depth, total
  prefill is quadratic-ish, and throughput falls as 1/context.

Fitting *cost per token = fixed + proportional to position* matches within a few percent
(200,044 → 4,486 observed / 4,447 model; 538,505 → 3,017 / 3,062; 769,274 → 2,525 / 2,526)
and puts the crossover at **~550k**. That is a descriptive fit, not a profile.

**The same `M × N` buffer is why prefill decays out there *and* why it used to crash.**

## Time to first token

| context | PP4 | TP4 |
|---|---|---|
| 2k | 0.79 s | 1.67 s |
| 32k | 4.78 s | 26.93 s |
| 100k | **14.6 s** | **87.3 s** |

## num_speculative_tokens

| value | aggregate tok/s | mean acceptance |
|---|---|---|
| **5** (= `dspark_block_size`) | **98.1** | 3.03 |
| 7 | 60.3 | 1.43–2.51 |

4 and below are rejected by vLLM for this checkpoint.

---

## Correctness

- **Needle-in-a-haystack passes at 23k / 77k / 95k tokens with DSpark enabled** — a
  distinctive passphrase buried at 10% depth is retrieved verbatim. This tests that the
  sparse indexer actually selects the right blocks across the whole window, not merely that
  the run completes.
- Reasoning spot-checks correct ("17 sheep, all but 9 run away" → 9).

### ⚠️ DSpark output is not reproducible

At temperature 0, DSpark output differs from non-speculative output on all 6 probe prompts,
**and differs between two runs of the same server**. Each divergence begins at an obviously
low-confidence branch point and every substantive answer was correct.

Controls run to interpret this:
- plain PP4 **is** self-deterministic (two identical runs, byte-identical output);
- **TP4 + DSpark, on the stock upstream path with none of the patches in this repo, is
  also non-deterministic** — so this is a property of DSpark, not of the pipeline-parallel
  patches here.

If you need bit-reproducible output, run without `--speculative-config`.

---

## Limits

### ★ Context ceiling — SOLVED

**The full 1,047,736-token context runs.** That is `--max-model-len 1048576` minus the 24
generated tokens, i.e. the config cap, with no bug wall below it. Needle-in-haystack verified
(passphrase at 10% depth, so a PASS means the sparse indexer really selected the right blocks
across the whole window).

Settings: `DSV4_LOGITS_ROW_CHUNK=128`, `VLLM_PP_LAYER_PARTITION=12,12,12,7`,
`--gpu-memory-utilization 0.85`. KV pool 4,991,054 tokens (4.76× concurrency at 1M).

| real prompt tokens | TTFT | prefill tok/s | needle |
|---|---|---|---|
| 134,659 | 29.8 s | 4,524 | ✅ ← the old failure point |
| 292,351 | 77.3 s | 3,781 | ✅ |
| 538,505 | 178.5 s | 3,017 | ✅ |
| 769,274 | 304.6 s | 2,525 | ✅ |
| **1,047,736** | **550.2 s** | **1,904** | ✅ |

Also 4 concurrent 153,891-token prompts, 4/4 correct with no cross-request bleed.

**Everything the previous version of this document said about the ceiling scaling inversely
with `--max-model-len` was a symptom of this bug and is withdrawn.** Set `--max-model-len` to
what you need.

#### The cause

`fp8_mqa_logits_triton` allocates `logits = torch.empty((M, N), torch.float32)` — `M` = tokens
in the prefill chunk, `N = seq_len / compress_ratio` — and passes the whole buffer to the
top-k. It grows with context and is the largest allocation on the Triton fallback path (the
one sm_80 takes because DeepGEMM is unavailable). Above ~134k the worker dies with
`Xid 31 — MMU Fault ... ACCESS_TYPE_VIRT_WRITE`; the surviving ranks then emit misleading
`gloo ... Connection closed by peer` — **chase the Xid, not the gloo message.**

**`CUDA_LAUNCH_BLOCKING=1` is what located it**, in one run, after three sessions of code
reading produced three confident and wrong theories:

```
attention.py:496                 execute_in_parallel(lambda: indexer(...))
attention.py:893                 self.indexer_op(hidden_states, q_quant, k, weights)
sparse_attn_indexer.py:965/592   fp8_mqa_logits_triton(...)
mqa_logits_triton.py:389         _fp8_mqa_logits_kernel[grid](...)
RuntimeError: Triton Error [CUDA]: an illegal memory access was encountered
```

#### The fix — [patch 0006](patches/0006-logits-row-chunk.patch)

Each row's top-k reads only its own `[ks, ke)` window, so **rows are independent** — computing
them in blocks is exact, not an approximation. `DSV4_LOGITS_ROW_CHUNK=256` reaches ~957,600;
**1M needs 128**. That the wall moves with the block size confirms the same allocation is still
the limiter — the chunk is a dial on it, not a cure, and a properly bounded buffer is the right
upstream fix.

**Cost: none measurable.** Prefill 1,456 vs 1,448 tok/s at 4k. Decode aggregate over 4 runs
each on the same live server: 82.7 / 83.1 / 98.9 / 110.2 with the fix, 97.9 / 100.7 / 102.3 /
102.7 without — overlapping, and the patch sits inside `if has_prefill:` so decode is untouched
by construction.

### Root-cause attempts that did NOT work — don't repeat these

| tried | result |
|---|---|
| PR **#49897** — torch fallback for `top_k_per_row_prefill` (Python) | no change to the ceiling; cost ~10% prefill |
| PR **#49139** — radix histogram ring fix (CUDA, needs full rebuild) | no change |
| PR **#50201** — harden `top_k_per_row` against NaN/under-fill (CUDA) | no change |
| `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` | **unusable on these cards** — hard fail at model load: cannot map 20 MB with **28.6 GiB free**. CUDA VMM appears broken on GA100 CMP parts. |
| `VLLM_PP_LAYER_PARTITION=12,12,12,7` | **did not move the ceiling** — but worth setting anyway: **+85% KV pool** and it removes a real 8.7 GiB rank imbalance |

Two theories that fit the evidence beautifully and were both wrong:

**The radix threshold.** `RADIX_THRESHOLD = 32768` in `persistent_topk.cuh`; with
`compress_ratio 4` that is exactly 131,072 context tokens, matching the original boundary to
the token. Applying both radix PRs (full CUDA rebuild) changed nothing. It is **not** an
integer-width bug either — the largest offset the faulting kernel computes at these sizes is
~7.5e7, three orders of magnitude below 2³¹.

**The starved pipeline rank.** The last PP rank carries `lm_head` *and* the DSpark drafter
while vLLM sizes the KV pool uniformly, so it ran at **0.09 GiB free versus 8–9 GiB on its
peers** — and the ceiling correlated cleanly and monotonically with `--gpu-memory-utilization`
across three values. Rebalancing with `VLLM_PP_LAYER_PARTITION` brought every rank to 6–8 GiB
free and grew the KV pool 85%, and **the ceiling did not move by one token** — the fault simply
migrated to PP0, the rank with the *most* free memory, because under pipeline parallelism the
leading rank reaches the critical `N` first. Memory pressure only decided which rank noticed
first. **A clean monotonic correlation is not a cause.**

### ⚠️ Patch 0001 is precautionary, not a fix for an observed bug

Earlier versions of this repo stated as fact that without patch 0001, sm_80 emits
fluent-looking degenerate text at prompt lengths 2049–4096. **That is withdrawn.** The
original reporter has retracted it for `dsv4-flash-a100` after building and running the
branch, and **we could not reproduce it either.**

A/B on this hardware (4× 170HX, PP4 + DSpark, `dsv4-0731-orig`), toggling only the
`has_device_capability(90)` term, with a one-shot probe confirming which path was actually
selected each time:

```
gate ON  → [topk-probe] persistent=False cooperative=False topk_tokens=512
gate OFF → [topk-probe] persistent=True  cooperative=False topk_tokens=512
```

Needle retrieval with **`persistent_topk` active** (gate OFF — the allegedly broken path),
fresh KV cache, candidate counts spanning the claimed band:

| real prompt tokens | candidate count | in band (512–1024)? | needle |
|---|---|---|---|
| 1,967 | 491 | no | ✅ |
| 2,351 | 588 | **yes** | ✅ |
| 2,813 | 703 | **yes** | ✅ |
| 3,274 | 818 | **yes** | ✅ |
| 3,736 | 934 | **yes** | ✅ |
| 4,120 | 1,030 | no | ✅ |
| 4,505 | 1,126 | no | ✅ |

Clean throughout, and the answers are coherent — which is what a needle test detects, since
wrong indices would prevent retrieval.

**And the gate costs nothing either way.** Decode aggregate over 3 runs each: gate ON
107.1 / 99.3 / 90.8, gate OFF 104.7 / 92.5 / 85.5 — overlapping, inside DSpark's
[non-determinism](#-dspark-output-is-not-reproducible). Prefill in the band is identical
(2,399–2,992 tok/s either way).

**So we keep patch 0001 as a free guard** — the failure may well be real on older bases,
where the reporter first saw it — but nobody should apply it believing this branch is broken
without it. If you want to check on your own hardware, flip the `has_device_capability(90)`
term and log `use_persistent_topk` at the selection site; a code read is not sufficient, which
is the whole lesson here.

### Four cards required
| configuration | result |
|---|---|
| 4 cards | works |
| 3 cards + DSpark, util 0.90 | illegal memory access in Marlin MXFP4 expert repack |
| 3 cards, **no** DSpark, util 0.85 | **same failure, same place** |
| 2 cards | 140 GB of weights does not fit in 127 GB of VRAM |

The 3-card failure is in `marlin_utils_fp4.py:332 _repack_marlin_experts`, during the
*target* model's load — so it is neither DSpark-related nor a GPU-memory-utilization
setting. Note pipeline parallel imposes no divisibility requirement (43 layers split fine
over 3), unlike tensor parallel where 64 heads and 256 experts genuinely cannot divide by 3.

Untested lead: the older INT4 compressed-tensors repack of this model *did* run on 3 cards
on an earlier stack. The MXFP4 Marlin path is what faults, so an INT4 checkpoint may avoid it.

---

## Measurement pitfalls found the hard way

Each of these produced a wrong number before it was caught:

1. **A streaming harness must not count SSE chunks.** Under speculative decoding one chunk
   carries several tokens (≈ the acceptance length). Counting chunks reported 24 tok/s where
   the true figure was 79.5. Rate against the server's `completion_tokens`.
2. **Compared configs must generate the same number of tokens** — pass `ignore_eos`, or
   speculative output diverges, hits EOS early, and you compare 50 tokens against 192.
3. **Discard the first request after boot** — Triton JIT makes it read ~4× low. A cold
   reading of 514 tok/s was really 1,966.
4. **Disable prefix caching for benchmarks**, or repeated prompts skip prefill entirely.
   The tell was a 100k-token prompt appearing to reach first token in 0.5 s.
5. **Do not derive decode rate by subtracting two calls** (`max_tokens=1` vs `max_tokens=N`).
   At long context prefill varies by seconds between runs and the subtraction produced
   135 tok/s sitting between neighbours of 43.8 and 38.6.
6. **Assert what is actually running** — `docker inspect NAME --format '{{join .Args " "}}'`.
   A stray launcher invocation once won the container name and served a whole sweep from a
   different configuration.
7. **A best steady-state window is not a benchmark.** Reporting the fastest 10-second
   logging window gave 3.6×; the honest end-to-end figure over mixed content was 1.9×.
