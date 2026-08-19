# PATCH.md — applied patch stack on this checkout (vllm-c3046d1)

Updated: 2026-08-19 06:50 +0800 (canary @5700 running this tree)

Current applied stack (per patches/ in deepseek-v4-cmp170hx repo):

| # | patch | files | purpose |
|---|-------|-------|---------|
| 0007 | lenient DSML tool-call parsing | parser/engine/adapters.py, parser/abstract_parser.py, entrypoints protocol.py | tolerate DSML variant tool-call syntax |
| 0008 | grammar salvage | scheduler.py, structured_output | commit rejected blocks unchanged instead of truncating |
| 0009 | spec-decode grammar prefix | scheduler.py, __init__.py | validate accepted spec blocks against FSM |
| 0010 | DSpark OOV sentinel guard | scheduler.py | detect OOV ids in scheduled blocks; get_vocab_size() fix |
| 0011 | PR46149 port | parser trio | reasoning/grammar tool-parser fixes |
| 0012 | scheduler-worker desync fix | scheduler.py, __init__.py, speculator.py | root-cause fix of 2026-08-18 double crash (indexSelectSmallIndex) |
| 0013 | grammar bifurcation | backend_xgrammar.py, __init__.py, scheduler.py | TYPE-A truncate+finish on FSM mid-block termination; TYPE-B observe |
| 0014 | post-salvage damage guard | scheduler.py | DSV4_SALVAGE_TOKEN_BUDGET=64 + degenerate-signature watch |
| 0015 | always-on soup tripwire (v3) | scheduler.py | DSV4_SOUP_STREAK=12 new-token streak; sig table (NO DSML wrapper strings) |
| 0016 | draft-window FSM overfeed | scheduler.py, backend_xgrammar.py | accept_tokens break on stop; draft filter -> validate_tokens_ex; 612 warnings 39->0 |

Backups on this tree: *.bak-<patch#> beside each patched file
(scheduler.py.bak-0016 is the immediate rollback point for 0016;
.bak-0015v3 for 0015; etc. Full rollback: DSV4_NO_MOUNT=1 launches the
baked image instead of this mount).

Env knobs: DSV4_SALVAGE_TOKEN_BUDGET / DSV4_SALVAGE_GUARD / DSV4_SOUP_TRIPWIRE / DSV4_SOUP_STREAK / DSV4_GRAMMAR_SALVAGE

Log signatures (monitoring): "DSV4 0014 salvage-guard armed|salvage-cap hit|degenerate-signature", "DSV4 0015 soup-tripwire", "Grammar completed mid-block ... (TYPE-A)", "TYPE-B", grammar_matcher.cc:612 (should stay 0 post-0016).

Validation state (2026-08-19 three-round battery): ctx 4.7K-498K 64/64 arms-pass, 24-turn agentic loop to 162K clean, 20-tool schemas clean, hammer 200/200, soak 1399/1400 (1 client-side transient, server zero non-200), 612=0, TYPE-B=0, salvage=0, crashes=0.
