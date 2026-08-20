# PATCH.md — applied patch stack on this checkout (vllm-c3046d1)

Updated: 2026-08-21 02:45 +0800 (prod @5700 on 0027v2, TYPEB=commit)

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
| 0027v2 | PP structured-output drain | v1/core/sched/{interface,scheduler}.py, v1/engine/core.py (+launch mounts for interface/core) | port of buliaoyin 6e959b2 / vllm#45015 queue-drain, adapted to sync Scheduler: in-flight-token criterion, defer+drain grammar bitmask sampling until older batches processed; v1 (ph-based) was live-crashed 0820 and replaced same day — see patches/0027-pp-structured-drain/PATCH.md |

Backups on this tree: *.bak-<patch#> beside each patched file
(scheduler.py.bak-0019v2 / deepseek_v4.py.bak-0018 are the immediate rollback points;
.bak-0016 for 0016; etc. Full rollback: DSV4_NO_MOUNT=1 launches the
baked image instead of this mount).

Env knobs: DSV4_SALVAGE_TOKEN_BUDGET / DSV4_SALVAGE_GUARD / DSV4_SOUP_TRIPWIRE / DSV4_SOUP_STREAK / DSV4_GRAMMAR_SALVAGE / DSV4_REPETITION_TRIPWIRE / DSV4_REP_STREAK / DSV4_SOUP_TOTAL / DSV4_REP_WINDOW

Log signatures (monitoring): "DSV4 0014 salvage-guard armed|salvage-cap hit|degenerate-signature", "DSV4 0015 soup-tripwire", "DSV4 0017 salvage-cap clean-cut", "DSV4 0019 rep-tripwire", "Grammar completed mid-block ... (TYPE-A)", "TYPE-B", grammar_matcher.cc:612 (should stay 0 post-0016).

Validation state (2026-08-19 three-round battery): ctx 4.7K-498K 64/64 arms-pass, 24-turn agentic loop to 162K clean, 20-tool schemas clean, hammer 200/200, soak 1399/1400 (1 client-side transient, server zero non-200), 612=0, TYPE-B=0, salvage=0, crashes=0.

Issue-replay state (2026-08-19 late, 0019v2 stack): verbatim issue-1/2 poison replay (i1 x4/i1t/i2, temp 1.0) zero errors, think_leak=0, soup cumulative fired correctly, rep-tripwire 9/20 induction + 0 false kills on 3/3 probes, hammer 200/200. One stochastic crash 11:42 (0010 OOV sentinel first-fire -> PP3 device assert): not reproduced in 60 poisoned requests, see README incident notes.

0020 stack (2026-08-19 night, post verify-clamp): full replay zero errors zero crashes, rep-tripwire firing correctly, hammer 200/200, probes 3/3, spec acceptance length 3.2-4.2 (healthy), Out-of-vocab=0, EngineDead=0.

0021 stack (2026-08-19 23:10 -> 2026-08-20 18:23, ~20h prod soak incl. real Claude Code traffic): watchdog clean, no crash; suites below re-run after 0026 deploy all PASS.

0026 stack (2026-08-20 19:03, NaN argmax guard — allover326 PR #10 / vllm#50183, direct apply, zero port): launched on 5700 with 19-file mounts. Canary same evening: tdd_0026 live repro (stock emits OOB padded-region id 999999 on all-NaN row; patched emits deterministic in-range 1000; clean rows byte-identical), tdd_consecutive_tools PASS (B 3/3, C 3/3), tdd_consecutive_stream PASS (D 3/3+3/3, E 3/3), tdd_issue_replay zero leaks/tripwires/dead, hammer 200/200 ALL GOOD. Root-cause fix for issue #9-class repetition ("复读机"): complements 0015-0019 tripwires (those stop damage after; this prevents the bad token from ever committing). NOTE: the 19:03 launch carries the 0022-0025 tree but WITHOUT DSV4_TYPEB_POLICY=finish (0025 dormant in default commit mode — the pending finish-mode A/B is still unexecuted; restart-dsv4.sh passes DSV4_TYPEB_POLICY through for when it is). Numbering: 0022 = envelope-tripwire (parallel session); NaN guard = 0026.

2026-08-20 infra notes: machine RAM upgraded 31G->125G (reboot, /tmp wiped — watchdog re-deployed from bench/); launch/run-pp-dspark.sh had lost the 0020 mount + MODEL default had reverted to /models (an earlier edit) — both restored, 0026 mount added (19 files now); launch/restart-dsv4.sh added (one-command recovery, defaults --safetensors-load-strategy prefetch for the 125G-RAM cold-boot path); Windows-side availability alert (vllm-maker poll_5700_alert.py + Startup VBS).

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
| 0025 | typeb-finish (F3b) | v1/core/sched/scheduler.py, .../chat_completion/serving.py | DSV4_TYPEB_POLICY=finish: on TYPE-B keep only the FSM-accepted prefix and finish the request this iteration (TYPE-A semantics — desync-safe, no future spec window); client gets a clean retryable cut instead of a malformed-envelope tail. Default commit = exact 0014 behavior. serving.py: new `typeb_cut` flag (proper `<｜DSML｜tool_calls>` opener without closer) distinguished from `pseudo_tag` garbage | DSV4_TYPEB_POLICY={commit,finish} (default commit — deploy alone is a no-op until set to finish at launch) |

Local validation: py_compile both; _dsv4_0022_flags unit battery 8/8
(typeb_cut vs pseudo_tag vs budget_burn vs closed-envelope); patch -p1 on
pristine live baselines reproduces target md5s (scheduler c4b15fcb…,
serving fac70e0d…); default-policy path is byte-identical behavior.

Deploy: patch -p1 on 760T tree (baselines = live 201120d3… scheduler /
ba11e046… serving), relaunch WITH `DSV4_TYPEB_POLICY=finish` in env
(plus DSV4_PORT=5700 DSV4_MAXLEN=524288), then re-run phase0 L arm —
expect NO_TOOL_STOP+pseudo_tag -> TYPEB_CUT (clean prefix) or TOOL_OK.

Deploy + live validation (2026-08-20 16:40, IN PROGRESS): patched tree
md5 == local validated md5s (c4b15fcb… / fac70e0d…); 0022 tripwire keeps
firing; 0025 marked active via `DSV4_TYPEB_POLICY=finish` env at launch.

**Full launch command (authoritative, single line):**
```
ssh 760T 'cd /mnt/nvme1/dsv4/deepseek-v4-cmp170hx && \
  DSV4_VLLM_SRC=/mnt/nvme1/dsv4/vllm-c3046d1/vllm \
  DSV4_MODEL=/mnt/data/DeepSeek-V4-Flash-0731 \
  DSV4_PORT=5700 DSV4_MAXLEN=524288 \
  DSV4_TYPEB_POLICY=finish \
  bash launch/run-pp-dspark.sh'
```
Required env on 760T: VLLM_SRC + MODEL (script defaults are generic `/opt/...` and
`/models/...`), PORT, MAXLEN, TYPEB_POLICY (default commit preserves 0014).
Rollback = drop `DSV4_TYPEB_POLICY=finish` from the env block.

**LESSON — never scp local launch script over server copy** (16:40 footgun):
my local repo's `launch/run-pp-dspark.sh` was rebuilt from scratch earlier
and silently dropped four mount entries (`dspark/speculator.py`,
`structured_output/__init__.py`, `parser/engine/adapters.py`,
`parser/abstract_parser.py`) plus the `--enable-auto-tool-choice /
--tool-call-parser / --reasoning-parser / --default-chat-template-kwargs`
serve-args. scp'ing it onto 760T at the 16:40 restart silently
disarmed `auto` tool_choice (caller got 400 from `/v1/chat/completions`).
Server's git HEAD (`vllm-c3046d1` fork's authoritative launch) was the
true source of truth and has been re-synced into the local repo
(commit 896839e, md5 1858f0ae…). Single authoritative script now lives
in the repo and the server; future deploys: `scp FROM server` (or just
trust HEAD + repo) — never the other direction.

Post-deploy A/B validation runs automatically via `tmp/0025-relaunch-and-verify.sh`:
poll /v1/models until 200, then smoke (nonstream tools call) + phase0
L arm rerun → tmp/reports/phase0-0025.jsonl. Watchdog at
`/tmp/watchdog_5700.sh` already extended with `env0022` + `tbfin` columns.

## 0027 deploy + validation (2026-08-21, live on 760T :5700)

v1 (AsyncScheduler ph-bookkeeping mirror) crashed live at 17:32 (IndexError
pop-from-empty-deque at the deferred main pop; also empty-content responses
from corrupted scheduling arithmetic — ph is load-bearing in 6 dormant sync
paths). v2 (in-flight-token criterion) applied same evening, tdd 20/20,
all regression suites PASS, hammer 200/200, prod relaunched on v2.

- A/B battery (bench/battery_0027.py, 3x8 concurrent json_schema):
  pre-0027 17/24, 17/24 — v2 18/24. No regression, no measurable gain;
  residual concurrent corruption is the pre-existing TYPE-B/0008-salvage
  path ("abandoning structured-output enforcement and committing the block
  unchanged"), NOT stale-FSM. Follow-up candidate for 0028.
- DSV4_TYPEB_POLICY reverted to commit (default): finish-mode truncates
  concurrent response_format to the `{`-prefix (1/8 valid). The pending
  0025 finish A/B must scope finish to the DSML tool path or reconsider.
- Backups .bak-0027 beside all 3 files; patch.diff regenerated from baks.
- Authoritative launch stays: DSV4_PORT=5700 DSV4_MAXLEN=524288
  bash launch/run-pp-dspark.sh (model default is correct again; the old
  /mnt/data model path in the 16:40 command no longer exists).
