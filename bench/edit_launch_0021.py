#!/usr/bin/env python3
"""Add tokenizers/deepseek_v4_encoding.py to the launch mount list (0021)."""
P = "/mnt/nvme1/dsv4/deepseek-v4-cmp170hx/launch/run-pp-dspark.sh"
s = open(P, encoding="utf-8").read()
anchor = "           parser/deepseek_v4.py \\\n"
assert s.count(anchor) == 1, ("anchor", s.count(anchor))
s = s.replace(
    anchor,
    anchor + "           tokenizers/deepseek_v4_encoding.py \\\n",
)
open(P, "w", encoding="utf-8").write(s)
print("mount line added")
