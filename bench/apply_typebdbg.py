#!/usr/bin/env python3
"""TEMPORARY diagnostic (0028 RCA): dump TYPE-B commit-site context.

Adds an env-gated (DSV4_TYPEB_DEBUG) debug log at the spec-decode TYPE-B
'commit' branch in scheduler.update_from_output, right before enforcement
is abandoned. Dumps: the spec window that shaped the grammar bitmask
(incl. -1 padding), num_invalid_spec_tokens, the committed block, the
FSM-valid prefix, the request's last 6 tokens, and the stale flag.

Revert: cp v1/core/sched/scheduler.py.bak-typebdbg <file>  (or rerun with
--revert). Not for permanent deploy.
"""
import py_compile
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else "/mnt/nvme1/dsv4/vllm-c3046d1")
REL = "vllm/v1/core/sched/scheduler.py"

ANCHOR = """                            # read-only property: the next line flips use_structured_output
                            request.structured_output_request = None
"""

DEBUG = """                            if os.environ.get("DSV4_TYPEB_DEBUG"):
                                _win = scheduler_output.scheduled_spec_decode_tokens.get(
                                    req_id
                                )
                                _ninv = dict(
                                    getattr(
                                        scheduler_output,
                                        "num_invalid_spec_tokens",
                                        {},
                                    )
                                ).get(req_id)
                                logger.warning(
                                    "TYPEB-DBG %s stale=%s win=%s ninv=%s "
                                    "block=%s kept=%s last=%s",
                                    req_id,
                                    output_is_stale,
                                    _win,
                                    _ninv,
                                    new_token_ids,
                                    _kept,
                                    list(request.all_token_ids[-6:]),
                                )
                            # read-only property: the next line flips use_structured_output
                            request.structured_output_request = None
"""

p = ROOT / REL
src = p.read_text()
if "TYPEB-DBG" in src:
    print("SKIP (already applied)")
    sys.exit(0)
bak = p.with_name(p.name + ".bak-typebdbg")
if not bak.exists():
    bak.write_text(src)
n = src.count(ANCHOR)
assert n == 1, f"anchor matched {n} times (want 1)"
p.write_text(src.replace(ANCHOR, DEBUG, 1))
py_compile.compile(str(p), doraise=True)
print("OK diagnostic applied; relaunch with DSV4_TYPEB_DEBUG=1")
