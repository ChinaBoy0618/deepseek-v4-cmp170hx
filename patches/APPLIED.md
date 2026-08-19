# PATCH.md — applied patch stack on this checkout (vllm-c3046d1)

Updated: 2026-08-19 20:05 +0800 (canary @5700 running this tree through 0019)

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
| 0017 | soup signature extension + cap-hit clean boundary | scheduler.py | 9 new pseudo-tag signatures (issue-1/2 sessions); _dsv4_clean_cut backtracks cap-hit to unclosed-'<' |
| 0018 | thinking-variant absorption | parser/deepseek_v4.py | <thinking>/</thinking> strip-only terminals (issue-1, 99x leak) |
| 0019 | line-repetition tripwire | scheduler.py | short-line >=5x streak-6 -> FINISHED_STOPPED (issue-2 Lets reload.x60); DSV4_REPETITION_TRIPWIRE / DSV4_REP_STREAK / DSV4_SOUP_TOTAL knobs |
| 0017v2 | soup cumulative totals | scheduler.py | fire on streak>=12 OR total>=DSV4_SOUP_TOTAL(18); sparse pseudo-tag leakage (live-TDD finding) |
| 0019v2 | repetition window floor | scheduler.py | max(new+16, DSV4_REP_WINDOW=160) — 21-token window could never hold 5 occurrences at spec=5 block sizes (live-TDD finding) |
| 0020 | verify-output vocab clamp | rejection_sampler.py (+launch mount) | sampled.clamp_ after rejection_sample — OOB sentinel degrades one token instead of PP-wide embedding assert; closes the 0819-1142 crash path |
| 0021 | raw-DSML history normalization | tokenizers/deepseek_v4_encoding.py (+launch mount) | extract COMPLETE DSML tool-call blocks from assistant content into structured tool_calls; raw-echo history renders canonically -> consecutive tool calls keep working (user-reported loop stall) |

Backups on this tree: *.bak-<patch#> beside each patched file
(scheduler.py.bak-0019v2 / deepseek_v4.py.bak-0018 are the immediate rollback points;
.bak-0016 for 0016; etc. Full rollback: DSV4_NO_MOUNT=1 launches the
baked image instead of this mount).

Env knobs: DSV4_SALVAGE_TOKEN_BUDGET / DSV4_SALVAGE_GUARD / DSV4_SOUP_TRIPWIRE / DSV4_SOUP_STREAK / DSV4_GRAMMAR_SALVAGE / DSV4_REPETITION_TRIPWIRE / DSV4_REP_STREAK / DSV4_SOUP_TOTAL / DSV4_REP_WINDOW

Log signatures (monitoring): "DSV4 0014 salvage-guard armed|salvage-cap hit|degenerate-signature", "DSV4 0015 soup-tripwire", "DSV4 0017 salvage-cap clean-cut", "DSV4 0019 rep-tripwire", "Grammar completed mid-block ... (TYPE-A)", "TYPE-B", grammar_matcher.cc:612 (should stay 0 post-0016).

Validation state (2026-08-19 three-round battery): ctx 4.7K-498K 64/64 arms-pass, 24-turn agentic loop to 162K clean, 20-tool schemas clean, hammer 200/200, soak 1399/1400 (1 client-side transient, server zero non-200), 612=0, TYPE-B=0, salvage=0, crashes=0.

Issue-replay state (2026-08-19 late, 0019v2 stack): verbatim issue-1/2 poison replay (i1 x4/i1t/i2, temp 1.0) zero errors, think_leak=0, soup cumulative fired correctly, rep-tripwire 9/20 induction + 0 false kills on 3/3 probes, hammer 200/200. One stochastic crash 11:42 (0010 OOV sentinel first-fire -> PP3 device assert): not reproduced in 60 poisoned requests, see README incident notes.

0020 stack (2026-08-19 night, post verify-clamp): full replay zero errors zero crashes, rep-tripwire firing correctly, hammer 200/200, probes 3/3, spec acceptance length 3.2-4.2 (healthy), Out-of-vocab=0, EngineDead=0.
