#!/usr/bin/env python3
"""TEMPORARY diagnostic (0028 RCA, stage 4): stack-trace the -1 writer.

Makes Request.spec_token_ids a property whose setter prints a stack trace
when a -1-containing list is stored (DSV4_TYPEB_DEBUG=1). Definitive
identification of the placeholder writer.

Revert: cp vllm/v1/request.py.bak-typebsrc4 <file>  (or --revert).
"""
import py_compile
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else "/mnt/nvme1/dsv4/vllm-c3046d1")
REL = "vllm/v1/request.py"

ANCHOR = """        self.spec_token_ids: list[int] = []
"""
REPL = """        self.spec_token_ids: list[int] = []
"""

# property injected at class level: anchor on num_tokens property
PROP_ANCHOR = """    @property
    def num_tokens(self) -> int:
        return len(self._all_token_ids)
"""
PROP_REPL = '''    @property
    def spec_token_ids(self) -> list[int]:
        return self._spec_token_ids

    @spec_token_ids.setter
    def spec_token_ids(self, value: list[int]) -> None:
        if value and any(t < 0 for t in value):
            import os as _os

            if _os.environ.get("DSV4_TYPEB_DEBUG"):
                import sys as _sys
                import traceback as _tb

                print("TYPEB-W4 req-setter -1 write:", flush=True)
                _tb.print_stack(file=_sys.stderr)
        self._spec_token_ids = value

    @property
    def num_tokens(self) -> int:
        return len(self._all_token_ids)
'''

if "--revert" in sys.argv:
    p = ROOT / REL
    bak = p.with_name(p.name + ".bak-typebsrc4")
    if bak.exists():
        bak.replace(p)
        print("reverted")
    sys.exit(0)

p = ROOT / REL
src = p.read_text()
if "TYPEB-W4" in src:
    print("SKIP (already applied)")
    sys.exit(0)
n = src.count(PROP_ANCHOR)
assert n == 1, f"anchor matched {n} times"
bak = p.with_name(p.name + ".bak-typebsrc4")
if not bak.exists():
    bak.write_text(src)
# init line must set the underlying slot (avoid trace on every init)
src = src.replace(
    "        self.spec_token_ids: list[int] = []\n",
    "        self._spec_token_ids: list[int] = []\n",
    1,
)
p.write_text(src.replace(PROP_ANCHOR, PROP_REPL, 1))
py_compile.compile(str(p), doraise=True)
print("OK W4 setter-trace applied")
