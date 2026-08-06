# Patches

Against [haosdent/vllm@dsv4-flash-a100](https://github.com/haosdent/vllm/tree/dsv4-flash-a100)
(commit `f8ea5bb`). Apply with `patch -p1` from the vLLM checkout root.

> ⚠️ **Check out `f8ea5bb` explicitly — do not use the branch tip.** That branch has been
> force-pushed since these patches were generated, and the hunks will not apply to the new
> tip. `git checkout f8ea5bb` first. (Reported in
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
| **0006** | `model_executor/layers/sparse_attn_indexer.py` **(stacks on 0001)** | row-chunk the `[M, N]` float32 logits transient, gated by `DSV4_LOGITS_ROW_CHUNK` | ★ **The context-ceiling fix — ~134k → 1,047,736 tokens.** `fp8_mqa_logits_triton` allocates `logits = torch.empty((M, N), float32)` (`M` = prefill-chunk tokens, `N = seq_len / compress_ratio`) and hands the whole buffer to the top-k; it grows with context and is the largest allocation on the Triton fallback path. **Each row's top-k reads only its own `[ks, ke)`, so rows are independent and blocking them is exact, not approximate.** Default-OFF (`0` reproduces upstream byte-for-byte) because it is the same file as 0001 and you may want to bisect them. `256` reaches ~957,600; `128` reaches the full 1M. Costs nothing measurable — prefill 1,456 vs 1,448 tok/s at 4k, and the change is inside `if has_prefill:` so decode cannot be affected. |
| **0007** | `model_executor/layers/sparse_attn_indexer.py` **(required by 0006)** | define `_prefill_topk_needs_torch_fallback()` and `_top_k_per_row_prefill_torch()` | ★ **Patches 0001-0006 as first published were INCOMPLETE — 0006 calls these two functions at four sites and nothing defined them.** Applying the series to `f8ea5bb` therefore failed, and a tree forced past the rejects raises `NameError` on the first prefill that reaches the top-k. Reported by @fouvy, diagnosed by @snoby in [#1](https://github.com/allover326/deepseek-v4-cmp170hx/issues/1). **The fallback is ACTIVE on sm_80 by design — see below.** |

## ⚠️ The sm_80 prefill top-k fallback is load-bearing — do not stub it out

`_prefill_topk_needs_torch_fallback()` returns **True on sm_80 deliberately**, and patch 0007
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
originating report was retracted; **that retraction does not apply to 0007**, which fixes a
different bug in a different kernel that we did reproduce on our own cards.

Overrides: `VLLM_DSV4_PREFILL_TOPK_TORCH=0` forces the CUDA kernel back on, `=1` forces the
fallback on any architecture.

**Ordering contract:** the downstream sparse-attention kernels iterate selected KV positions
in **ascending position order**, whereas `torch.topk` returns *score* order.
`_top_k_per_row_prefill_torch` sorts accordingly and pads short rows with `-1` at the tail. A
naive mask-then-`torch.topk` reimplementation compiles and runs but feeds wrongly ordered
indices downstream.

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
