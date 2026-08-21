#!/usr/bin/env python3
"""TEMPORARY diagnostic (0028 RCA stage 6): the unrewritten-window path.

0028v2 (drain fix) left battery at 17/24: every TYPE-B now shows
win=[-1,-1,-1,-1,-1] read from scheduler_output.scheduled_spec_decode_tokens
-- the window was NEVER rewritten. The rewrite
(update_draft_token_ids_in_output) runs ONLY in the deferred branch. Theory:
these batches took the IMMEDIATE path (pending_structured_output_tokens
False -- checked at ph==0, i.e. shallow-queue regime) with placeholder
windows whose rows after the first -1 are UNCONSTRAINED -> greedy drafts
commit -> TYPE-B.

Three env-gated logs (DSV4_TYPEB_DEBUG):
  IMM       structured spec windows reaching the immediate path (the hole)
  TAKENONE  deferred take_draft_token_ids() returned None
  COVMISS   deferred drafts missing some structured req of the deferred so
  DEF       drain-time queue length + predicate (regime evidence)

Revert: run with --revert (restores .bak-0028dbg).
"""
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else "/mnt/nvme1/dsv4/vllm-c3046d1")
REL = "vllm/v1/engine/core.py"

EDITS = [
    (
        # 1) immediate path: structured spec windows sample unrewritten
        """                if not scheduler_output.pending_structured_output_tokens:
                    # We aren't waiting for any tokens, get any grammar output
                    # and sample immediately.
                    grammar_output = self.scheduler.get_grammar_bitmask(
                        scheduler_output
                    )
""",
        """                if not scheduler_output.pending_structured_output_tokens:
                    # DSv4-0028dbg (TEMP): structured spec windows reaching the
                    # IMMEDIATE path are never rewritten by
                    # update_draft_token_ids_in_output (deferred-only), so
                    # their placeholder [-1]*k rows after the first are
                    # unconstrained -> FSM-invalid drafts commit -> TYPE-B.
                    if os.environ.get("DSV4_TYPEB_DEBUG"):
                        _imm = [
                            (rid, tuple(win))
                            for rid, win in (
                                scheduler_output.scheduled_spec_decode_tokens.items()
                            )
                            if getattr(
                                self.scheduler.requests.get(rid),
                                "use_structured_output",
                                False,
                            )
                        ]
                        if _imm:
                            logger.warning("TYPEB-IMM n=%d ex=%s", len(_imm), _imm[:4])
                    # We aren't waiting for any tokens, get any grammar output
                    # and sample immediately.
                    grammar_output = self.scheduler.get_grammar_bitmask(
                        scheduler_output
                    )
""",
        "TYPEB-IMM",
    ),
    (
        # 2) deferred take: None / partial coverage
        """            if self.check_for_draft_tokens:
                draft_token_ids = self.model_executor.take_draft_token_ids()
                if draft_token_ids is not None:
""",
        """            if self.check_for_draft_tokens:
                draft_token_ids = self.model_executor.take_draft_token_ids()
                # DSv4-0028dbg (TEMP): None or partial drafts leave the
                # placeholder window in place -> unconstrained spec rows.
                if os.environ.get("DSV4_TYPEB_DEBUG"):
                    if draft_token_ids is None:
                        logger.warning("TYPEB-TAKENONE")
                    else:
                        _miss = [
                            rid
                            for rid in (
                                deferred_scheduler_output.scheduled_spec_decode_tokens
                            )
                            if getattr(
                                self.scheduler.requests.get(rid),
                                "use_structured_output",
                                False,
                            )
                            and rid not in draft_token_ids.req_ids
                        ]
                        if _miss:
                            logger.warning(
                                "TYPEB-COVMISS n=%d %s", len(_miss), _miss[:4]
                            )
                if draft_token_ids is not None:
""",
        "TYPEB-TAKENONE",
    ),
    (
        # 3) drain regime
        """            while batch_queue and self.scheduler.has_structured_output_in_flight(
                deferred_scheduler_output
            ):
""",
        """            if os.environ.get("DSV4_TYPEB_DEBUG"):
                logger.warning(
                    "TYPEB-DEF qlen=%d inflight=%s",
                    len(batch_queue),
                    self.scheduler.has_structured_output_in_flight(
                        deferred_scheduler_output
                    ),
                )
            while batch_queue and self.scheduler.has_structured_output_in_flight(
                deferred_scheduler_output
            ):
""",
        "TYPEB-DEF",
    ),
]

if "--revert" in sys.argv:
    p = ROOT / REL
    bak = p.with_name(p.name + ".bak-0028dbg")
    if bak.exists():
        bak.replace(p)
        print(f"reverted {REL}")
    sys.exit(0)

p = ROOT / REL
src = p.read_text()
if "DSv4-0028dbg" in src:
    print("SKIP (already applied)")
    sys.exit(0)

cur = src
for anchor, repl, tag in EDITS:
    n = cur.count(anchor)
    if n != 1:
        print(f"FAIL: anchor for {tag} matched {n} times")
        sys.exit(1)
    cur = cur.replace(anchor, repl, 1)

bak = p.with_name(p.name + ".bak-0028dbg")
if not bak.exists():
    bak.write_text(src)
compile(cur, str(p), "exec")  # in-memory syntax check
p.write_text(cur)
print("OK 0028dbg applied (3 edits)")
