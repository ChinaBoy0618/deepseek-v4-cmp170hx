#!/usr/bin/env python3
"""TEMPORARY diagnostic (0028 RCA, stage 2): sentinel-window ORIGIN.

Two env-gated (DSV4_TYPEB_DEBUG) logs:
  A) worker get_draft_tokens else-branch: when [-1] placeholders are
     manufactured (this is the only -1 producer besides in_output padding)
  B) scheduler update_draft_token_ids: when a structured, non-prefill-chunk
     request stores a window containing -1 (the poisoning moment)

Revert: cp <file>.bak-typebsrc <file>  (or rerun with --revert).
"""
import py_compile
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else "/mnt/nvme1/dsv4/vllm-c3046d1")

EDITS = [
    # (relpath, anchor, replacement, marker)
    (
        "vllm/v1/worker/gpu/spec_decode/utils.py",
        """        else:
            # This case only happens when async scheduling is disabled.
            draft_token_ids = [[-1] * self.num_draft_tokens for _ in self.req_ids]
""",
        """        else:
            # This case only happens when async scheduling is disabled.
            draft_token_ids = [[-1] * self.num_draft_tokens for _ in self.req_ids]
            import os as _os

            if _os.environ.get("DSV4_TYPEB_DEBUG"):
                import logging as _lg

                _lg.getLogger("dsv4.typebsrc").warning(
                    "TYPEB-SRC worker-else nreqs=%d ndraft=%d reqs=%s",
                    len(self.req_ids),
                    self.num_draft_tokens,
                    list(self.req_ids)[:8],
                )
""",
        "TYPEB-SRC worker-else",
    ),
    (
        "vllm/v1/core/sched/scheduler.py",
        """            request.spec_token_ids = spec_token_ids
""",
        """            if (
                os.environ.get("DSV4_TYPEB_DEBUG")
                and request.use_structured_output
                and not request.is_prefill_chunk
                and any(t < 0 for t in spec_token_ids)
            ):
                logger.warning(
                    "TYPEB-SRC store-poison req=%s win=%s ntok=%d computed=%d",
                    req_id,
                    spec_token_ids,
                    request.num_tokens,
                    request.num_computed_tokens,
                )
            request.spec_token_ids = spec_token_ids
""",
        "TYPEB-SRC store-poison",
    ),
]

if "--revert" in sys.argv:
    for rel, _, _, _ in EDITS:
        p = ROOT / rel
        bak = p.with_name(p.name + ".bak-typebsrc")
        if bak.exists():
            bak.replace(p)
            print(f"reverted {rel}")
        else:
            print(f"no backup for {rel}")
    sys.exit(0)

for rel, anchor, repl, marker in EDITS:
    p = ROOT / rel
    src = p.read_text()
    if marker in src:
        print(f"SKIP {rel} (already applied)")
        continue
    # exact anchor: scheduler anchor has tricky indentation; normalize check
    n = src.count(anchor)
    if n != 1:
        print(f"FAIL {rel}: anchor matched {n} times")
        sys.exit(1)
    bak = p.with_name(p.name + ".bak-typebsrc")
    if not bak.exists():
        bak.write_text(src)
    p.write_text(src.replace(anchor, repl, 1))
    py_compile.compile(str(p), doraise=True)
    print(f"OK {rel}: {marker}")

print("relaunch with DSV4_TYPEB_DEBUG=1")
