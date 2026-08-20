#!/usr/bin/env python3
"""DSV4 0020: clamp the verify kernel's sampled tokens to the vocab.

Crash 0819-1142 root path: rejection_sample's `sampled` (accepted+bonus)
carried id==vocab_size out of a degraded block (scheduler 0010 guard saw
it at position 0 of 1); the worker commits tokens to req_state before
the scheduler reacts, so the next step's embedding device-asserts and
all PP ranks die. The 0012 clamp covers the drafter anchor only. Clamp
here = one-token degradation instead of engine death (same philosophy as
markov_argmax's output clamp)."""
import py_compile
import shutil

P = "/mnt/nvme1/dsv4/vllm-c3046d1/vllm/v1/worker/gpu/spec_decode/rejection_sampler.py"
src = open(P, encoding="utf-8").read()
shutil.copy(P, P + ".bak-0020")


def rep(old, new, cnt=1):
    global src
    assert src.count(old) == cnt, ("anchor broken", old[:70], src.count(old))
    src = src.replace(old, new)


# 1. vocab limit in __init__
rep('''        self.sampler = sampler
        self.num_speculative_steps = spec_config.num_speculative_tokens''',
    '''        self.sampler = sampler
        self.num_speculative_steps = spec_config.num_speculative_tokens
        # DSV4 0020: bound for clamping the verify kernel's sampled ids.
        self._dsv4_vocab_limit = sampler.sampling_states.vocab_size - 1''')

# 2. clamp right after rejection_sample, before the logprobs flatten
rep('''        return processed_logits, sampled, num_sampled''',
    '''        # DSV4 0020: a degraded verify block can carry the vocab_size
        # sentinel (or garbage) out of the kernel; the worker commits
        # sampled tokens to req_state before the scheduler can react, so
        # the next embedding lookup device-asserts (2026-08-19 11:42
        # engine death). Clamp in place BEFORE the logprobs flatten reads
        # it: a leak degrades this one token instead of killing all ranks.
        _lim = getattr(self, "_dsv4_vocab_limit", None)
        if _lim is None:
            _lim = self.sampler.sampling_states.vocab_size - 1
            self._dsv4_vocab_limit = _lim
        sampled = sampled.clamp_(min=0, max=_lim)
        return processed_logits, sampled, num_sampled''')

open(P, "w", encoding="utf-8").write(src)
py_compile.compile(P, doraise=True)
print("0020 applied + py_compile OK")
