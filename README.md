# DeepSeek-V4-Flash on 4× CMP 170HX

Running **DeepSeek-V4-Flash-0731** (~284B total / ~13B active) on four **NVIDIA CMP 170HX**
mining cards — GA100 silicon, sm_80, VRAM-unlocked to 64 GB, PCIe Gen2 x4, no P2P.

**98 tok/s decode · ~5,300 tok/s prefill · 123k verified context · ~$1k of hardware.**

For scale: a single DGX Spark does ~14 tok/s on this class of model, and a dual-Spark setup
with speculative decoding reports 55–67.

This repo contains the patches, container build, launch scripts and benchmark harnesses —
plus [every setting and why it has that value](SETTINGS.md), and the
[full measured results](RESULTS.md) including the things that *don't* work.

---

## What is actually new here

**DSpark speculative decoding running under pipeline parallelism.** vLLM refuses this
combination in three separate places. Enabling it is worth **1.93×** and — unlike on tensor
parallel, where speculation goes *negative* above ~8 concurrent requests — it keeps winning
all the way to 64 concurrent on PP.

Everything else in this repo is packaging: a working container build, the launch settings,
and honest benchmarks.

Credit where it's due: the SM8x DeepSeek-V4 backend is
[haosdent/vllm@dsv4-flash-a100](https://github.com/haosdent/vllm/tree/dsv4-flash-a100),
built on work discussed in [vllm#50576](https://github.com/vllm-project/vllm/issues/50576).
This repo sits on top of that branch.

---

## Requirements

- **Exactly 4 CMP 170HX** (or other 64 GB sm_80 cards). **3 cards does not work** — it fails
  in the Marlin MXFP4 expert repack, independent of speculation and memory settings. 2 cards
  cannot hold 140 GB of weights. See [RESULTS](RESULTS.md#four-cards-required).
- Cards must be **VRAM-unlocked** (65,536 MiB) and, if they are 170HX, have **B30 taped** —
  an untaped card asserts `PWRBRK#` and runs at ~88 W / 1140 MHz, roughly 4× slow.
- The original `deepseek-ai/DeepSeek-V4-Flash-0731` checkpoint (~140 GB on disk). The
  INT4/compressed-tensors repack is **not** needed; this branch reads the native
  MXFP4+FP8 weights.
- Docker with the NVIDIA runtime.

## Quick start

```bash
git clone --branch dsv4-flash-a100 --single-branch --depth 50 \
    https://github.com/haosdent/vllm.git
cd vllm
for p in ../deepseek-v4-cmp170hx/patches/*.patch; do patch -p1 < "$p"; done

cp ../deepseek-v4-cmp170hx/docker/Dockerfile.devel .
cp ../deepseek-v4-cmp170hx/docker/dockerignore.txt .dockerignore
docker build -f Dockerfile.devel -t dsv4-a100:devel .    # ~10 min, mostly download

../deepseek-v4-cmp170hx/launch/run-pp-dspark.sh
```

Two build traps worth knowing before you start:

- **Build from inside the vLLM checkout.** If your model weights live in a parent directory,
  a build rooted there will try to ship hundreds of GB to the Docker daemon as build context.
- **The base image needs a real CUDA toolkit.** `python:3.12-slim` plus pip CUDA wheels gives
  `nvcc` 13.3 against FlashInfer's bundled headers for 13.0, and FlashInfer's JIT is a hard
  requirement at engine init. `Dockerfile.devel` uses `nvidia/cuda:13.0.2-cudnn-devel` with a
  venv, which is why it works.

The branch's SM8x commit touches only Python and Triton — no `csrc/`, no CMake — so
`VLLM_USE_PRECOMPILED=1` turns what would be a multi-hour CUDA build into a download.

---

## Results at a glance

| | plain | **+ DSpark** |
|---|---|---|
| decode, single stream | 50.8 | **98.1 tok/s** |
| decode @ 64 concurrent | 472.0 | **712.8 tok/s** |
| decode @ 100k context | 38.8 | **90.0 tok/s** |
| prefill (25k–77k context) | ~5,300 | ~5,200 tok/s |
| time to first token @ 100k | 14.6 s | 14.6 s |

**Use pipeline parallel, not tensor parallel.** On this hardware PP beats TP by **6.6× on
prefill** — TP measures flat at ~800 tok/s from 1.5k to 77k tokens, because it performs 86
all-reduces per forward pass on a PCIe Gen2 x4 link with no P2P. PP moves the same data 3
times. Full reasoning in [SETTINGS.md](SETTINGS.md#--pipeline-parallel-size-4--not-tensor-parallel).

Full tables, correctness testing and limits: **[RESULTS.md](RESULTS.md)**.

---

## Known limits

- **Context ceiling ~128k.** Verified at 123,120 tokens; ~154k kills a worker with
  `Xid 31 — MMU Fault`. This is a kernel bug, not memory — the KV pool reports 6.9M tokens.
  The ceiling sitting near 2¹⁷ suggests a fixed buffer or index width in the sparse indexer.
- **DSpark output is not reproducible** at temperature 0, including run-to-run on the same
  server. Verified to be a property of DSpark itself, not of these patches (the stock
  upstream tensor-parallel path behaves the same way). Quality is unaffected in every test
  here. Run without `--speculative-config` if you need determinism.
- **Never use `--enforce-eager`** — 8–10 tok/s, worse than no speculation at all.

## Repo layout

```
patches/     5 patches against haosdent/vllm@dsv4-flash-a100 — see patches/README.md
docker/      container build (CUDA-devel base + venv, precompiled vLLM wheel)
launch/      run-pp-dspark.sh (best config) and run-a100.sh (tensor-parallel variant)
bench/       the 8 harnesses every number in RESULTS.md came from
SETTINGS.md  every flag and why it has that value
RESULTS.md   measured results, correctness testing, limits, measurement pitfalls
```

## Contributing back

Patches 2–5 are small and general. Pipeline parallel was flagged as *the* uncovered
configuration in [vllm#50576](https://github.com/vllm-project/vllm/issues/50576), and patch 3
is essentially vLLM PR #46994, which upstream still needs. If you have hardware to test on —
especially a non-170HX sm_80 box such as an A100 or A800 — results either way are useful.

## License

Apache-2.0, matching vLLM. The patches are derivative of vLLM and of
haosdent/vllm@dsv4-flash-a100.
