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

0021 stack (2026-08-20): consecutive-tool replay B 2/2, E 3/3, A pass, hammer 200/200 (live validation, see commit 9396b68).

## Applied on 760T container 2026-08-20 13:2x (deployed + validated)

Implemented for the 20260820 tool-call degradation RCA (fix-plan F2/F3/F4/F5).
Base verified byte-identical to live container (md5 of all 5 target files
checked before patching). Stack order: 0022 -> 0023 -> 0024.

| # | patch | files | purpose | env knobs |
|---|-------|-------|---------|-----------|
| 0022 | envelope tripwire (F2) | entrypoints/openai/chat_completion/serving.py, .../protocol.py | request has tools + response has 0 tool_calls -> log `DSV4 0022 envelope-missing` + response/chunk gains `dsv4_flags: ["pseudo_tag"|"budget_burn"]`; log-only, no generation change | DSV4_ENVELOPE_TRIPWIRE (default on, 0=off) |
| 0023 | args-truncated finish (F3c) | parser/engine/parser_engine.py, .../serving.py | ToolCallSlot.closed; finish_streaming sets truncated_tool_args when a streamed slot never closed AND args are not valid JSON; streaming finish then reports finish_reason="length" instead of "tool_calls" so clients deterministically retry (kills silent truncated-JSON args from TYPE-B + 0014 64-token cap) | none (behavior: finish_reason semantics only) |
| 0024 | template hardening (F4+F5) | tokenizers/deepseek_v4_encoding.py, tokenizers/deepseek_v4.py | F4: protocol CORRECT/INCORRECT examples in tools section; tool_result truncation + pseudo-tag backtick escape; consecutive-failure SYSTEM WARNING; history-think placeholder (keep only post-last-user reasoning). F5: auto-disable thinking on tool turns. ALL default OFF | DSV4_TPL_EXAMPLES=1, DSV4_TOOL_RESP_MAX=<chars> (0=off), DSV4_TPL_FAILWARN=<n>, DSV4_TPL_THINKING_KEEP=last, DSV4_AUTO_DISABLE_THINKING_TOOLS=1; per-request: chat_template_kwargs {"auto_disable_thinking_with_tools": bool} |

Local validation (2026-08-20): ast.parse all 5 files; encode_messages
functional battery PASS (default-off no-op vs live base behavior identical;
each knob exercised; 0021 raw-DSML lift regression PASS; stack applies
cleanly via patch -p1 in order and reproduces target md5s).

Deploy: add 5 files to launch/run-pp-dspark.sh MOUNTS (done in repo),
sync patched checkout to 760T, relaunch. NOT deployed — awaiting user go.

Watchdogs: add "DSV4 0022 envelope-missing" count + finish=length rate to
watchdog_5700 CSV after deploy.

Note (resolved 2026-08-20): 0017v2-0021 were synced back to the repo via
pull (commits ec71531..9396b68); the "live ahead of repo" drift is closed.
0022+ layer cleanly on top of the 0021 stack (base md5s re-verified against
the live container before patching).

Deploy + live validation (2026-08-20 13:25): patched tree on 760T
(/mnt/nvme1/dsv4/vllm-c3046d1) md5 == local validated md5s; mounts extended
(serving.py / parser_engine.py / tokenizers/deepseek_v4.py added); relaunched
with DSV4_PORT=5700 DSV4_MAXLEN=524288 (script defaults 8098/32768 — do not
relaunch bare). Smoke: nonstream tools -> tool_calls clean; stream tools ->
delta + finish=tool_calls; no-tools -> no flags. Tripwire live-fired:
budget-cut probe (tools+auto, max_tokens=12) -> response dsv4_flags=
["budget_burn"] + WARNING "DSV4 0022 envelope-missing" in server log. All
0024 knobs remain OFF pending A/B. Rollback: cp *.bak-002N back + relaunch.

## 0025 typeb-finish (STAGED, not deployed — 2026-08-20)

Implemented for the live reasonix incident (F3b). Root cause chain reproduced
deterministically (phase0 L arm + single-probe): TYPE-B (grammar rejects a
spec token, FSM still live) -> 0013 commits block unconstrained -> 0014 arms
64-token salvage -> unconstrained tail re-emits MALFORMED DSML envelope
(`<｜DSML｜tool_calls<invoke` missing ｜DSML｜) -> salvage-cap FINISHED_STOPPED
mid-args -> raw content leaks, finish=stop, 0022 pseudo_tag. Phase0 L on the
0022-0024 stack: L-W2k/L-W8k 3x NO_TOOL_STOP+pseudo_tag; L-W32k 2x FAIL_0023
(args > max_tokens — client chunking, out of scope); Bash arms all OK.

| # | patch | files | purpose | env knobs |
|---|-------|-------|---------|-----------|
| 0025 | typeb-finish (F3b) | v1/core/scheduler.py, .../chat_completion/serving.py | DSV4_TYPEB_POLICY=finish: on TYPE-B keep only the FSM-accepted prefix and finish the request this iteration (TYPE-A semantics — desync-safe, no future spec window); client gets a clean retryable cut instead of a malformed-envelope tail. Default commit = exact 0014 behavior. serving.py: new `typeb_cut` flag (proper `<｜DSML｜tool_calls>` opener without closer) distinguished from `pseudo_tag` garbage | DSV4_TYPEB_POLICY={commit,finish} (default commit — deploy alone is a no-op until set to finish at launch) |

Local validation: py_compile both; _dsv4_0022_flags unit battery 8/8
(typeb_cut vs pseudo_tag vs budget_burn vs closed-envelope); patch -p1 on
pristine live baselines reproduces target md5s (scheduler c4b15fcb…,
serving fac70e0d…); default-policy path is byte-identical behavior.

Deploy: patch -p1 on 760T tree (baselines = live 201120d3… scheduler /
ba11e046… serving), relaunch WITH `DSV4_TYPEB_POLICY=finish` in env
(plus DSV4_PORT=5700 DSV4_MAXLEN=524288), then re-run phase0 L arm —
expect NO_TOOL_STOP+pseudo_tag -> TYPEB_CUT (clean prefix) or TOOL_OK.
