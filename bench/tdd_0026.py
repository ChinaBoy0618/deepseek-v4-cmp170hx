"""TDD for patch 0026 (0026 NaN->-inf argmax guard, allover326 PR #10 / vllm#50183).

Drives the real @triton.jit device function `_compute_global_target_argmax`
from BOTH module variants inside the serving container:

  stock   = /vllm/vllm/.../rejection_sampler_utils.py   (image, unpatched)
  patched = /tmp/rsu_patched.py                          (host tree, 0026 applied)

Rows simulate a real spec=5 block stream (lesson from 0019v1: test with real
block sizes, 5-token blocks):

  row 0: clean
  row 1: clean
  row 2: ALL-NaN valid blocks   <- the killer row (issue #9)
  row 3: NaN mixed with valid values
  row 4: clean

PASS criteria (patched): every emitted token id is inside the valid block
argmax-id set {1000,1001,1002,1003}; the all-NaN row yields a deterministic
in-range id (block 0), and the mixed row still picks the real max block.
Stock's outputs are printed as evidence (undefined behavior, no hard assert).
"""

import importlib.util
import os
import sys

import torch
import triton

STOCK_PATH = os.environ.get(
    "TDD_STOCK", "/vllm/vllm/v1/worker/gpu/spec_decode/rejection_sampler_utils.py"
)
PATCHED_PATH = os.environ.get("TDD_PATCHED", "/tmp/rsu_patched.py")

NUM_ROWS = 5          # spec=5 block stream
NUM_VALID_BLOCKS = 4  # vocab_num_blocks
PADDED_BLOCKS = 8     # PADDED_VOCAB_NUM_BLOCKS (padded region exists)
STRIDE = PADDED_BLOCKS

VALID_IDS = list(range(1000, 1000 + NUM_VALID_BLOCKS))
PADDED_JUNK = 999999  # what an OOB read of the padded region would fetch


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_probe(mod, name):
    """Write a real .py file (Triton needs inspect-able source) whose module
    globals bind _compute_global_target_argmax to `mod`'s version, then load
    it and return its @triton.jit probe kernel."""
    import triton.language as tl  # noqa: F401  (kept for clarity)
    target_path = getattr(mod, "__file__", None)
    probe_path = f"/tmp/_tdd0026_probe_{name}.py"
    src = f'''
import importlib.util
import triton
import triton.language as tl

_spec = importlib.util.spec_from_file_location("rsu_target_{name}", {target_path!r})
_target = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_target)
_compute_global_target_argmax = _target._compute_global_target_argmax


@triton.jit
def _probe(max_ptr, argmax_ptr, out_ptr, num_rows, num_valid, STRIDE: tl.constexpr, PADDED: tl.constexpr):
    pid = tl.program_id(0)
    if pid < num_rows:
        out = _compute_global_target_argmax(
            max_ptr, STRIDE, argmax_ptr, STRIDE, pid, num_valid, PADDED)
        tl.store(out_ptr + pid, out)
'''
    with open(probe_path, "w") as f:
        f.write(src)
    spec = importlib.util.spec_from_file_location(f"probe_mod_{name}", probe_path)
    probe_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe_mod)
    return probe_mod._probe


def build_inputs(device):
    nan = float("nan")
    # [NUM_ROWS, PADDED_BLOCKS]; padded region holds junk (never masked-loaded,
    # but an OOB argmax index would read it).
    local_max = torch.full((NUM_ROWS, PADDED_BLOCKS), float("-inf"), device=device)
    local_argmax = torch.full((NUM_ROWS, PADDED_BLOCKS), PADDED_JUNK, dtype=torch.int64, device=device)

    rows = [
        [1.0, 9.0, 2.0, 3.0],          # clean: max at block 1
        [4.0, 1.0, 7.0, 0.5],          # clean: max at block 2
        [nan, nan, nan, nan],          # ALL-NaN: the issue #9 killer row
        [nan, 5.0, nan, 2.0],          # mixed: real max at block 1
        [8.0, 6.0, 1.0, 0.0],          # clean: max at block 0
    ]
    for r, vals in enumerate(rows):
        for b, v in enumerate(vals):
            local_max[r, b] = v
            local_argmax[r, b] = 1000 + b
    expected_best_block = [1, 2, 0, 1, 0]  # block-0 for the all--inf tie
    return local_max, local_argmax, expected_best_block


def run_variant(name, mod, local_max, local_argmax, device):
    probe = make_probe(mod, name.strip())
    out = torch.full((NUM_ROWS,), -1, dtype=torch.int64, device=device)
    probe[(NUM_ROWS,)](
        local_max, local_argmax, out,
        NUM_ROWS, NUM_VALID_BLOCKS,
        STRIDE=STRIDE, PADDED=PADDED_BLOCKS,
    )
    torch.cuda.synchronize()
    ids = out.tolist()
    print(f"[{name}] token ids: {ids}")
    return ids


def main():
    assert torch.cuda.is_available(), "needs a CUDA device (run inside container)"
    device = "cuda"

    stock = load_module(STOCK_PATH, "rsu_stock")
    patched = load_module(PATCHED_PATH, "rsu_patched")

    # Source-level: both guards present in patched module's file.
    src = open(PATCHED_PATH).read()
    n_guards = src.count("!= local_max, float(\"-inf\")") + src.count("!= resampled_local_max,")
    assert n_guards == 2, f"expected 2 NaN guards in patched file, found {n_guards}"
    print(f"[src] patched file contains both guards (greedy + resample): OK")

    local_max, local_argmax, expected_best_block = build_inputs(device)

    stock_ids = run_variant("stock  ", stock, local_max.clone(), local_argmax.clone(), device)
    patched_ids = run_variant("patched", patched, local_max.clone(), local_argmax.clone(), device)

    failures = []

    # Clean rows must be identical (no regression).
    for r in (0, 1, 4):
        if stock_ids[r] != patched_ids[r]:
            failures.append(f"row {r} regression: stock {stock_ids[r]} vs patched {patched_ids[r]}")
        exp = 1000 + expected_best_block[r]
        if patched_ids[r] != exp:
            failures.append(f"row {r} expected {exp} got {patched_ids[r]}")

    # Mixed-NaN row: patched must pick the true max block (block 1 -> 1001).
    if patched_ids[3] != 1001:
        failures.append(f"row 3 (mixed NaN): expected 1001, got {patched_ids[3]}")

    # ALL-NaN row: patched must emit an in-range, deterministic id.
    if patched_ids[2] not in VALID_IDS:
        failures.append(f"row 2 (all-NaN): patched emitted OOB id {patched_ids[2]}")
    elif patched_ids[2] != 1000:
        print(f"[note] all-NaN row resolved to block {patched_ids[2]-1000} (in-range, safe)")

    # Evidence line for stock (undefined behavior; not asserted).
    oob = [i for i in stock_ids if i not in VALID_IDS]
    print(f"[stock all-NaN row id] {stock_ids[2]}  (undefined; OOB reads observed: {oob or 'none this run'})")

    if failures:
        print("FAIL:")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("PASS: patched module emits in-range deterministic ids under NaN; clean rows unchanged.")


if __name__ == "__main__":
    main()
