#!/usr/bin/env python3
"""Fix the launch script's default MODEL path (the /models dir is empty)."""
P = "/mnt/nvme1/dsv4/deepseek-v4-cmp170hx/launch/run-pp-dspark.sh"
s = open(P, encoding="utf-8").read()
old = 'MODEL="${DSV4_MODEL:-/models/DeepSeek-V4-Flash-0731}"'
new = 'MODEL="${DSV4_MODEL:-/mnt/data/DeepSeek-V4-Flash-0731}"'
assert s.count(old) == 1, ("anchor", s.count(old))
open(P, "w", encoding="utf-8").write(s.replace(old, new))
print("default model path fixed")
