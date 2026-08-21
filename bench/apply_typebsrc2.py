#!/usr/bin/env python3
"""TEMPORARY diagnostic (0028 RCA, stage 3): window -1 writer identification.

Every remaining writer of a -1-containing spec window, env-gated
(DSV4_TYPEB_DEBUG):
  W1) schedule(): window copied from request.spec_token_ids contains -1
  W2) update_draft_token_ids_in_output: rebound window contains -1
  E)  worker-else now logs the FULL req list (no [:8] truncation)

Revert: run with --revert (restores .bak-typebsrc3).
"""
import py_compile
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else "/mnt/nvme1/dsv4/vllm-c3046d1")

# (relpath, anchor, replacement, applied-detect-marker)
EDITS = [
    (
        "vllm/v1/core/sched/scheduler.py",
        """                    scheduled_spec_decode_tokens[request.request_id] = spec_token_ids
""",
        """                    if os.environ.get("DSV4_TYPEB_DEBUG") and any(
                        t < 0 for t in spec_token_ids
                    ):
                        logger.warning(
                            "TYPEB-W1 sched-poison req=%s win=%s",
                            request.request_id,
                            spec_token_ids,
                        )
                    scheduled_spec_decode_tokens[request.request_id] = spec_token_ids
""",
        "TYPEB-W1 sched-poison",
    ),
    (
        "vllm/v1/core/sched/scheduler.py",
        """            sched_spec_tokens[req_id] = spec_token_ids
""",
        """            if os.environ.get("DSV4_TYPEB_DEBUG") and any(
                t < 0 for t in spec_token_ids
            ):
                logger.warning(
                    "TYPEB-W2 inoutput-poison req=%s win=%s orig=%d ninv=%s",
                    req_id,
                    spec_token_ids,
                    orig_num_spec_tokens,
                    num_invalid_spec_tokens.get(req_id),
                )
            sched_spec_tokens[req_id] = spec_token_ids
""",
        "TYPEB-W2 inoutput-poison",
    ),
    (
        "vllm/v1/worker/gpu/spec_decode/utils.py",
        """                    list(self.req_ids)[:8],
""",
        """                    list(self.req_ids),
""",
        "reqs=%s\",\n                    len(self.req_ids),\n                    self.num_draft_tokens,\n                    list(self.req_ids),",
    ),
    # F) np branch: rows containing -1 (direct observation)
    (
        "vllm/v1/worker/gpu/spec_decode/utils.py",
        """        if self.draft_tokens_np is not None:
            self.copy_event.synchronize()
            draft_token_ids = self.draft_tokens_np.tolist()
""",
        """        if self.draft_tokens_np is not None:
            self.copy_event.synchronize()
            draft_token_ids = self.draft_tokens_np.tolist()
            import os as _os

            if _os.environ.get("DSV4_TYPEB_DEBUG") and any(
                t < 0 for row in draft_token_ids for t in row
            ):
                import logging as _lg

                _lg.getLogger("dsv4.typebsrc").warning(
                    "TYPEB-W3 np-mineq reqs=%s rows=%s",
                    list(self.req_ids),
                    draft_token_ids,
                )
""",
        "TYPEB-W3 np-mineq",
    ),
]

if "--revert" in sys.argv:
    for rel, _, _, _ in EDITS:
        p = ROOT / rel
        bak = p.with_name(p.name + ".bak-typebsrc3")
        if bak.exists():
            bak.replace(p)
            print(f"reverted {rel}")
    sys.exit(0)

for rel, anchor, repl, detect in EDITS:
    p = ROOT / rel
    src = p.read_text()
    if detect in src:
        print(f"SKIP {rel} (already applied)")
        continue
    n = src.count(anchor)
    if n != 1:
        print(f"FAIL {rel}: anchor matched {n} times")
        sys.exit(1)
    bak = p.with_name(p.name + ".bak-typebsrc3")
    if not bak.exists():
        bak.write_text(src)
    p.write_text(src.replace(anchor, repl, 1))
    py_compile.compile(str(p), doraise=True)
    print(f"OK {rel}")

print("relaunch with DSV4_TYPEB_DEBUG=1")
