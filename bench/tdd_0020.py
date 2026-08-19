#!/usr/bin/env python3
"""TDD-0020: verify-output vocab clamp (worker-side defense in depth).

Crash 0819-1142: the DSpark verify kernel's `sampled` (accepted + bonus
tokens) can carry the id == vocab_size sentinel out of a degraded block
(scheduler 0010 guard saw it at position 0 of 1). The worker commits it
to req_state before the scheduler can react, so the NEXT step's embedding
lookup device-asserts and every rank dies. The 0012 anchor clamp covers
only the drafter's anchor read; the verify OUTPUT was unguarded.

Fix contract: RejectionSampler._verify clamps `sampled` in place to
[0, vocab_size-1] immediately after rejection_sample() returns, BEFORE
the logprobs flatten sees it. A leak then degrades one token instead of
killing the engine.

RED on the current stack: V01, V02, V03.
"""
import sys
import types

import numpy as np
import torch

sys.path.insert(0, "/vllm")
import vllm.v1.worker.gpu.spec_decode.rejection_sampler as rsm  # noqa: E402
from vllm.v1.worker.gpu.spec_decode.rejection_sampler import (  # noqa: E402
    RejectionSampler,
)

VOCAB = 128
fails = []


def chk(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ((" | " + detail) if not cond else ""))
    if not cond:
        fails.append(name)


# ---------- fakes ----------
REAL_REJECTION_SAMPLE = rsm.rejection_sample


def fake_rejection_sample(*a, **k):
    """Degraded block: the sentinel id == vocab_size leaks at positions 1
    and 3 (position 0 of 1 crash shape), a negative id at the tail."""
    sampled = torch.tensor([[3, VOCAB, 9, VOCAB], [4, 5, -1, 6]], dtype=torch.int64)
    num_sampled = torch.tensor([4, 4], dtype=torch.int64)
    return sampled, num_sampled


fake_sampler = types.SimpleNamespace(
    apply_sampling_params=lambda logits, *a, **k: logits,
    logprobs_mode="none",
    compute_nans=False,
    use_fp64_gumbel=False,
    sampling_states=types.SimpleNamespace(
        vocab_size=VOCAB, max_num_logprobs=lambda np_arr: -1,
        temperature=types.SimpleNamespace(gpu=None),
        seeds=types.SimpleNamespace(gpu=None),
    ),
    req_states=types.SimpleNamespace(
        prefill_len=types.SimpleNamespace(gpu=None)
    ),
)


def make_rs():
    rs = RejectionSampler.__new__(RejectionSampler)
    rs.sampler = fake_sampler
    rs.num_speculative_steps = 4
    rs.use_block_verification = False
    rs.synthetic_conditional_rates = None
    return rs


rsm.rejection_sample = fake_rejection_sample
rsm.get_num_sampled_and_rejected = lambda ns, seq, cu, idx, pre: (
    torch.tensor([4, 4], dtype=torch.int64),
    torch.tensor([0, 0], dtype=torch.int64),
)

logits = torch.zeros(2, VOCAB, dtype=torch.float32)


def verify_once(rs):
    return rs._verify(
        logits, None,
        torch.tensor([7, 8], dtype=torch.int64),        # draft_sampled
        torch.zeros(2, dtype=torch.int64),              # pos
        torch.tensor([0, 2], dtype=torch.int64),        # cu_num_logits
        torch.arange(2, dtype=torch.int64),             # idx_mapping
        np.arange(2),                                   # idx_mapping_np
        torch.arange(2, dtype=torch.int64),             # expanded_idx_mapping
        torch.zeros(2, dtype=torch.int64),              # expanded_local_pos
    )


# ---------- V01: _verify output clamped [RED] ----------
_, sampled, _ = verify_once(make_rs())
chk("V01 _verify clamps vocab_size sentinel [RED]",
    int(sampled.max()) < VOCAB,
    f"max={int(sampled.max())} (sentinel {VOCAB} passed through)")

# ---------- V02: sentinel positions exactly clamped, clean kept [RED] ----------
chk("V02 sentinel->limit, clean ids untouched [RED]",
    sampled[0].tolist() == [3, VOCAB - 1, 9, VOCAB - 1],
    f"row0={sampled[0].tolist()}")

# ---------- V03: negative garbage clamped to 0 [RED] ----------
chk("V03 negative ids clamped to 0 [RED]",
    int(sampled.min()) >= 0, f"min={int(sampled.min())}")

# ---------- V04: clean block passes through unchanged ----------
def clean_sample(*a, **k):
    return (torch.tensor([[10, 11, 12, 13], [1, 2, 3, 4]], dtype=torch.int64),
            torch.tensor([4, 4], dtype=torch.int64))


rsm.rejection_sample = clean_sample
_, sampled, _ = verify_once(make_rs())
chk("V04 clean ids never over-clamped",
    sampled.tolist() == [[10, 11, 12, 13], [1, 2, 3, 4]])
rsm.rejection_sample = fake_rejection_sample

# ---------- V05: full __call__ plumbing (logprobs path off) ----------
fake_batch = types.SimpleNamespace(
    cu_num_logits_np=np.array([0, 2]),
    cu_num_logits=torch.tensor([0, 2], dtype=torch.int64),
    idx_mapping=torch.arange(2, dtype=torch.int64),
    idx_mapping_np=np.arange(2),
    expanded_idx_mapping=torch.arange(2, dtype=torch.int64),
    expanded_local_pos=torch.zeros(2, dtype=torch.int64),
    input_ids=torch.tensor([[7, 8, 0, 0, 0, 0]], dtype=torch.int64),
    logits_indices=torch.tensor([0]),
    positions=torch.tensor([0], dtype=torch.int64),
    seq_lens=torch.tensor([10], dtype=torch.int64),
    num_reqs=2,
)
out = make_rs().__call__(logits, fake_batch)
toks = out.sampled_token_ids
chk("V05 __call__ output clamped end-to-end [RED]",
    int(toks.max()) < VOCAB and int(toks.min()) >= 0,
    f"max={int(toks.max())}")

rsm.rejection_sample = REAL_REJECTION_SAMPLE
print("\n" + ("ALL GREEN" if not fails else "RED: %d fail(s): %s" % (len(fails), fails)))
sys.exit(1 if fails else 0)
