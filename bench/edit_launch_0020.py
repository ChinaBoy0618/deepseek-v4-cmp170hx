#!/usr/bin/env python3
"""Add rejection_sampler.py to the launch script's bind-mount list (0020)."""
P = "/mnt/nvme1/dsv4/deepseek-v4-cmp170hx/launch/run-pp-dspark.sh"
s = open(P, encoding="utf-8").read()
anchor = "v1/worker/gpu/spec_decode/dspark/speculator.py \\\n"
assert s.count(anchor) == 1, ("anchor", s.count(anchor))
s = s.replace(
    anchor,
    anchor + "           v1/worker/gpu/spec_decode/rejection_sampler.py \\\n",
)
open(P, "w", encoding="utf-8").write(s)
print("mount line added")
