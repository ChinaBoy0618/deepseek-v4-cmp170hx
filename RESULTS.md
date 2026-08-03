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
| verified context | 123,120 tokens | 123,120 tokens |

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

### ★ Context ceiling scales INVERSELY with `--max-model-len`

**Setting `--max-model-len` high costs you usable context.** Measured, needle-in-haystack
verified at each point:

| `--max-model-len` | highest verified prompt | first failure |
|---|---|---|
| 1,048,576 | 130,813 | 133,890 |
| 262,144 | 135,428 | 153,880 |
| **163,840** | **150,044** | ~157,700 |

**Set `--max-model-len` to roughly 10% above the context you actually need.** Setting it to
the model's 1,048,576 maximum reduces usable context to ~131k; setting it to 163,840 gets you
~150k. This is the single highest-leverage knob for long-context work and costs nothing.

Above the ceiling a worker dies with `Xid 31 — MMU Fault ... ACCESS_TYPE_VIRT_WRITE`. The
fault surfaces in `combine_topk_swa_indices` (`vllm/models/deepseek_v4/amd/rocm.py` — note
sm_80 runs the **ROCm** Triton path, which the branch author flagged as "more targeted for
ROCm instead of CUDA"). The surviving pipeline ranks then emit misleading
`gloo ... Connection closed by peer` errors — **chase the Xid, not the gloo message.**

Not simple memory exhaustion: at `--max-model-len 1048576` the KV pool reports **6,921,586
tokens** and the host had 156 GB of RAM free. But it is memory-*pressure* related — the
indexer prefill buffer is sized `max_model_len * 40 * 132` bytes, i.e. **5.5 GB/rank at 1M
versus 1.4 GB at 262k**, which is consistent with the inverse relationship above.

### Root-cause attempts that did NOT work — don't repeat these

| tried | result |
|---|---|
| PR **#49897** — torch fallback for `top_k_per_row_prefill` (Python) | no change to the ceiling; cost ~10% prefill |
| PR **#49139** — radix histogram ring fix (CUDA, needs full rebuild) | no change |
| PR **#50201** — harden `top_k_per_row` against NaN/under-fill (CUDA) | no change |

The radix-threshold theory was seductive and wrong: `RADIX_THRESHOLD = 32768` in
`csrc/libtorch_stable/persistent_topk.cuh`, and with `compress_ratio 4` that lands at exactly
131,072 context tokens — matching the original boundary to the token. But applying both radix
PRs (full CUDA rebuild, `TORCH_CUDA_ARCH_LIST=8.0`) changed nothing, and the later
`max-model-len` sweep showed the boundary is not fixed at 2¹⁷ at all. **An exact numerical
coincidence is not a diagnosis.**

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
