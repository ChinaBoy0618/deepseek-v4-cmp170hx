# dsv4s 工具调用协议发射退化 — 根因分析与执行计划

- 输入：`docs/issues/issue-20260819-artflow.md`、`docs/issues/issue-20260820-bash-format.md`、
  `tmp/reports/tool_test_report.md`（双格式测试 356 调用）、`bench/depth_degrade.py`（本计划定向测试）、
  参照模板 `chat_template-fix.jinja`（qwen3.8-froggeric-v22.1）
- 日期：2026-08-20 | 作者：RCA 会话 | 状态：分析定稿，待评审执行

## 0. 问题陈述

两起 agent 会话死亡（artflow 127k / 数智邮务 ~80k+），直接死因相同：**模型末轮未发出任何结构化
tool_call**——artflow 退化为重复叙述（"Let me reload" ×20+，中英混杂），数智邮务退化为自创
`<bash>…</bash>` 伪 XML。服务端引擎全程存活、0 非 200。间歇性（09:34 出现 → 09:55 恢复 →
09:56 复发）。

## 1. 五维取证结果

### 1.1 Chat template（线上接线已确认）

`docker inspect dsv4-a100`（760T，只读）：

```
--enable-auto-tool-choice --tool-call-parser deepseek_v4 --reasoning-parser deepseek_v4
--default-chat-template-kwargs {"thinking": true}
--max-model-len 524288 --kv-cache-dtype fp8 --block-size 256 --max-num-seqs 8
--speculative-config {"method":"dspark","num_speculative_tokens":5}  (PP4)
```

- thinking **全局默认开启**，所有客户端的工具轮次都先经过 `<think>` 段再进协议段。
- 模型自带 `encoding/encoding_dsv4.py` 参考实现；官方 README 明言：
  *"does not attempt to correct or recover from malformed output"* —— 健壮性是服务端补丁的责任。
- **词表检查**：dsv4 vocab 只有单个 `｜DSML｜` 特殊 token（信封 = 该 token + 普通结构化文本
  `tool_calls>` / `invoke name="…"` / `parameter …`）；**没有** `<|im_start|>`、`<tool_call>`、
  `<tool_response>`、`<|think_*|>` 等 Qwen 系 token → 参照模板**机制可移植、协议不可移植**。
- ⚠️ 线上 vllm 树（/mnt/nvme1/dsv4/vllm-c3046d1）已含 **0018**（`<thinking>` 变体 strip），
  repo `patches/` 只同步到 0016 —— 补丁栈存在漂移，需回同步。

### 1.2 工具解析器（parser/deepseek_v4.py DSML 状态机）

状态：CONTENT → REASONING → TOOL_PREAMBLE → TOOL_NAME → TOOL_ARGS → TOOL_BETWEEN。

- 0007 已宽容 `_tool_calls` 下划线变体（信封在、拼写畸变）。
- 0018 已吸收 `<thinking>` 幻觉变体（strip-only，不误路由）。
- **覆盖缺口（两案死因）**：信封整体缺失时（伪标签 / 纯叙述）→ 语义上无物可解析，按设计透传为
  content。这不是 bug 而是边界：解析器只对"发了信封"的输出负责。

### 1.3 思考解析器

- `<think>`/`</think>` 与 DSML 工具状态同机（REASONING 态下 TOOL_START 直接进 TOOL_PREAMBLE）。
- 关键失败模式：**think 未闭合**（长上下文 + temp 1.0 下推理膨胀/重复循环烧尽 max_tokens）→
  整个输出滞留 REASONING → 无 content、无 tool_call、finish=length。artflow 的重复叙述与
  本模式吻合（叙述若发生在 think 段内则客户端只见空转）。
- thinking 全局默认开启放大了预算暴露面：每个工具轮次都要先付 think 的 token 税。

### 1.4 服务端 grammar 路径（无约束尾部的两条通路）

- **auto 请求（两案客户端均为 auto）**：从第 0 个 token 起就没有 FSM 约束 → 发射退化畅通无阻。
- **required 请求**：grammar 约束生效，但 0013 分流下：
  - TYPE-A（FSM 块内完成）：截断垃圾尾 + 正常收尾 = 修复 ✅
  - TYPE-B（FSM 仍存活时违规）：**放弃后续约束、原样提交**（0012 存活性设计），0014 仅 64-token
    预算看护、0015 仅签名记录 → 该请求剩余部分等同 auto。
- 事故窗口（08-20 09:56:14，即停摆分钟）实测日志含 TYPE-B + salvage-guard armed —— 机制在实战中触发。
- 结论：**无论 auto 还是 required，深上下文请求都可能运行在无约束尾部**；差别只是概率。

### 1.5 模型能力（退化阶梯）

跨会话证据拼出单调的"结构丢失"阶梯（越结构化越先丢）：

| 级 | 形态 | 出处 | 已有防护 |
|---|------|------|----------|
| 0 | `<｜DSML｜tool_calls>` 完整信封 | 正常态 | — |
| 1 | `_tool_calls` 下划线变体 | 0007 调查 | 0007 宽容 ✅ |
| 2 | `<thinking>` 幻觉 think 变体 | issue-01（99 次） | 0018 strip ✅ |
| 3 | 自创 `<bash>`/`<Bash command>` 伪信封 | 20260820 案 | ❌ 无 |
| 4 | 纯叙述无任何结构（重复+中英混杂） | 20260819 案 | ❌ 无 |

双格式测试（08-20）：JSON 原生 required@64k 持续循环 93.8%/100%，XML 文本协议 56.2% + 大量
finish=length 截断 —— 结构发射可靠性随上下文深度与协议复杂度衰减，特殊 token 锚定的 DSML
介于两者之间。

## 2. 定向测试（进行中，bench/depth_degrade.py）

矩阵：深度 {77k, 115k, 154k 实测} × tool_choice {auto, required} × n=5 + XML 通道@115k +
污染模仿@30k + 预算 {2048, 8192}@115k。判据：TOOL_OK / NO_TOOL_STOP（发射退化）/
NO_TOOL_LENGTH（预算烧尽）/ PSEUDO_TAG（伪信封）/ ARGS_BAD / DUP。

### 2.1 深度 × tool_choice 矩阵（单轮，小参数，55 次调用，35 分钟）

| 臂 | n | 结果 | 判读 |
|---|---|------|------|
| 77k auto | 5 | TOOL_OK 5/5 | 单轮深度不退化 |
| 77k required | 5 | TOOL_OK 5/5 | 〃 |
| 115k auto | 5 | TOOL_OK 5/5 | 〃 |
| 115k required | 5 | TOOL_OK 5/5 | 〃 |
| 155k auto | 5 | TOOL_OK 5/5 | 〃 |
| 155k required | 5 | **4/5**（1× ARGS_BAD，重复 Bash×2，ctok=131） | TYPE-B 放弃后签名（重复+参数损伤） |
| XML 通道@115k | 5 | 无效（脚手架替换了填充消息，ptok=47） | 弃用；XML 通道已有 08-20 双格式数据（64k 持续循环 56.2%） |
| 污染@38k | 5 | TOOL_OK 5/5 | H4 单独不成立 |
| 对照@38k | 5 | TOOL_OK 5/5 | — |
| 预算 2048@115k | 5 | TOOL_OK 5/5 | H3 单独不成立（信封早发时预算无关） |
| 预算 8192@115k | 5 | TOOL_OK 5/5 | 〃 |

原始数据：`tmp/reports/depth_degrade.jsonl`。

### 2.2 活体事故（08-20 下午，与矩阵并发）

dsf agent（artflow 项目）发起**超长参数** Bash 调用（整个 .vue 文件进 heredoc 参数）→
参数发射中投机块被 FSM 拒收 → **TYPE-B 放弃约束** → 0014 salvage **64-token 预算强制收尾** →
参数 JSON 硬截于 `<el-d` → 客户端 `invalid args: unexpected end of JSON input`。
15 分钟窗口内 3 次 TYPE-B（03:10:38 / 03:11:36 / 03:11:49），机制高频活跃。

### 2.3 假设判定汇总

| 假设 | 判定 | 证据 |
|------|------|------|
| H1 单轮深度×auto 即退化 | **否定**（155k auto 仍 5/5） | 矩阵；事故需复合条件 |
| H2 TYPE-B→无约束尾部/截断 | **确认，且高频** | 3 次/15min + 活体截断 + 155k ARGS_BAD 样本 |
| H3 预算烧尽为主因 | 单独否定；在累积会话中与 think 膨胀叠加 | D 臂 10/10；双格式 XML finish=length |
| H4 污染模仿为主因 | 单独否定（38k 5/5） | C 臂；深度下贡献待累积复现器量化 |
| 复合条件（深度×多轮累积×真实工具历史） | **最强嫌疑，未复现** | 双格式累积循环 XML 56% vs 单轮矩阵 ~100% |

**关键推论**：复现两案必须用**累积多轮会话**形态（assistant/tool 交错 + thinking 保留 +
80k-130k），单轮合成填充已证明不触发。下一步复现器 = `bench_chat_accumulate`/`agent_loop`
混合形态（见 Phase 0）。

## 3. 根因结论

**主因（模型 × 采样条件）**：DSV4-Flash 在**累积多轮会话**深上下文（80k+）+ temp 1.0 +
thinking 开启下，DSML 协议段的发射概率衰减；`｜DSML｜` 特殊 token 后的结构化文本（带引号
属性语法）最先丢失。单轮深上下文不触发（155k auto 5/5）——退化与会话形态强耦合。

**直接截断器（服务端，已实锤）**：TYPE-B 放弃 + 0014 的 64-token salvage 预算，对**长参数
工具调用**是硬截断（08-20 活体事故）；对短参数则产生无约束尾部（重复下发/参数损伤）。
auto 请求从第 0 token 起即无约束；解析器对"信封缺失"按设计透传。

**放大器（客户端）**：thinking 默认开启拉长发射路径（先 think 后协议）；工具结果含
`<bash>` 文本的历史（数智邮务处理 shell 日志/docx）可能提供模仿源；~80k+ 不 compact。

**待定量**：深度阈值曲线、预算烧尽 vs 发射退化占比、污染模仿贡献率 —— 见第 2 节数据。

## 4. 执行计划

### Phase 0 — 复现器（先行，1-3 天，其余各 Phase 的度量基线）

1. **累积多轮矩阵**：`agent_loop.py` × `bench_chat_accumulate.py` 混合形态——真实工具集、
   assistant/tool 交错回填、thinking 保留、每轮注入真实感工具结果，跑到 80k/130k；对比
   auto/required、temp {0.7, 1.0}、thinking {on, off}。度量：PSEUDO/NO_TOOL/ARGS_BAD/DUP
   率 vs 深度曲线。
2. 修复 `bench/depth_degrade.py` B 臂 bug（xml 模式应前置 system 而非替换 messages[0]）。
3. 长参数专项：args 2k/8k/32k 字节 × required，量化 TYPE-B→0014 截断率（复现活体事故）。

### Phase 1 — 客户端立即缓解（0-2 天，无服务端变更）

1. **兜底解析**：对 content 中的 `<bash>…</bash>` / `<Bash command>…</Bash>` /
   `<bash_command>…</bash_command>` 块做二次解析→合成 tool_call，或触发一轮
   "请用工具通道重发"重试。命中即救活两案的死局形态。
2. **主动 compact**：会话上下文 ~80k 阈值触发压缩（issue 建议已验证可显著降退化概率）。
3. **采样降档**：深上下文轮次 temp 降至 ≤0.7 或 reasoning effort 降档（配合模板旋钮，见
   Phase 3）。
4. **长参数规避**：单工具调用参数 > ~2KB 时改用分段写（Write 首段 + Edit 追加）或引用
   文件路径而非内联内容——直接绕开 TYPE-B→0014 截断通路（08-20 活体事故的即时规避）。

### Phase 2 — 服务端防护补丁 0019（2-5 天）

1. **信封存在性 tripwire（先 log-only）**：请求带 tools 且响应完成时 0 个 tool_call 且
   content 含伪标签签名 → 日志 + response 扩展字段标记（如 `finish_reason` 附注），不改变
   行为。跑一周拿概率分布，再决定是否升级为自动 finish + 重试提示。
2. **0014 长参数豁免（08-20 活体事故的直接对策）**：TYPE-B 发生在参数中段时，64-token
   salvage 预算必然截断长参数 → 客户端拿到半截 JSON。选项评估：
   (a) 参数发射阶段（FSM 在 string 态）放宽/免除 salvage 预算，改为完整提交后校验；
   (b) TYPE-B 时直接拒绝该块并重试投机，而非放弃约束；
   (c) 保持现状 + response 标记 `finish_reason: "length"` 类显式信号，让客户端确定性重试。
   以 Phase 0.3 的截断率数据做取舍依据。
3. **TYPE-B 后预算收紧评估**：0014 的 64-token 预算降为 0/直接 FINISHED_STOPPED 的取舍
   （避免放弃约束后的长尾垃圾），用 wide_tools + depth 矩阵回归。
4. **流式早停**：streaming 模式下前 N token 内出现伪标签签名即停止生成（节省烧尽的
   finish=length 轮次）。

### Phase 3 — Chat template 强化（参照 chat_template-fix.jinja，1-2 周）

移植其**机制**到 DSML 原生模板（协议 token 不可移植，词表无 `<|im_start|>` 系）：

1. **协议 CORRECT/INCORRECT 范例**进 system 段（针对性：MISSING_ARG / ARGS_BAD /
   参数截断类失败——双格式测试实测的失败类别）。
2. **think 控制**：`auto_disable_thinking_with_tools`（工具轮次预填空 `<think></think>`，
   砍掉发射路径上的 think 税）；effort 分档。
3. **tool_response 降噪**：`max_tool_response_chars` 截断 + `<bash>` 类文本的转义/包裹，
   切断 H4 污染模仿源。
4. **连续失败警告**：连续 ≥2 次工具错误注入 SYSTEM WARNING（防死循环烧预算）。
5. **推理保留策略**：仅保留末轮 query 后的 reasoning（参照模板 `_preserve_thinking` 分界），
   降低历史 think 噪声对协议发射的干扰。
- A/B 验证：depth 矩阵对拍（模板改前/改后同矩阵同 seed）。

### Phase 4 — 模型侧蒸馏强化（2-6 周，长期根治）

1. 退化样本集：两案会话 + depth 矩阵失败输出（含 PSEUDO/NO_TOOL/ARGS_BAD 分类）。
2. 协议发射强化蒸馏（风格库蒸馏计划已在途），目标：深上下文协议 token 发射概率。
3. 回归门槛：depth 矩阵 TOOL_OK 率 ≥ 99%@154k auto。

### Phase 5 — 监测与回归固化

1. watchdog_5700 扩展指标：伪标签率、无工具轮率、TYPE-B 率、finish=length 率（每窗口 CSV）。
2. `bench/depth_degrade.py` 入 canary 常规项（weekly，n=5 全矩阵）。
3. **补丁栈回同步**：线上 0017/0018 回传 repo `patches/` + APPLIED.md 更新（防漂移）。

## 5. 风险与开放问题

- TYPE-B → 无约束尾部的发生率未长期量化（Phase 2.1 的 tripwire 兼做测量）。
- 参照模板 thinking 相关状态（`<|think_*>`）在 dsv4 上无对应 token，需用 chat_template_kwargs
  等价实现；行为等价性待验证。
- pollution 模仿（H4）在 38k 单独不显著；在累积深会话中的贡献率待 Phase 0 复现器量化，
  不显著则 Phase 3.3 降级为可选。
- 深度矩阵已证明单轮 155k auto 仍 100% —— 退化需"深度 × 多轮累积 × 真实工具历史"复合，
  Phase 0 是所有后续决策的度量前提；在此之前 Phase 1.2（compact）与 Phase 2.2（长参数
  豁免）为最高性价比先行项。

## 6. 深度扩展 300k 最终数据（2026-08-20，bench/depth300k.py）

**Arm A 单轮矩阵**（20 发，230k/300k × auto/required × 5）：全部 TOOL_OK；
唯一异常 = 300k-required 第 4 发 `TOOL_OK+DUP`（同一 Bash 调用重复 2 次）——
单轮极端深度的首个信号是重复发射，不是信封退化。

**Arm E 累积多轮**（auto + temp 1.0 + thinking，~9.1k tok/轮）：
| 运行 | 栈 | 终点 | 结果 |
|------|----|------|------|
| E1 | 0021 旧栈 | 107k / 12 轮 | 全 TOOL_OK（进程被 stash 误杀） |
| E2 | 0021 旧栈 | 152k / 17 轮 | 全 TOOL_OK（为部署重启主动停） |
| E3 | **0022-0024 新栈** | **301k / 33 轮** | 24 TOOL_OK + 9 TOOL_WRONGNAME（Write5/Read3/Grep1，均为 prompt 指令的 write-read-grep-bash 循环内合法选型）；**信封失败 0**（无 PSEUDO / NO_TOOL / ARGS_BAD / DUP） |

**结论**：
1. 累积深度 301k 内未复现信封退化 —— 本 harness 的工具结果是 exec_fake 精简假结果，
   真实事故会话带大体积真实工具输出（污染源）与真实 agent 指令；复现需 Phase 0 复现器
   引入真实工具输出回放，"纯深度累积"不足以触发。
2. 新栈（0022-0024，旋钮全 OFF）与旧栈行为一致 —— 无回归（E3 33 轮零信封失败）。
3. 0022 线上已验证触发路径（budget_burn 探针 + 日志/响应字段双确认）；
   服务器 2h 窗口仅 1 次 0022 事件（即探针自身），E3 期间 0 误报。

## 7. Phase 0 复现器最终数据（2026-08-20，bench/phase0_repro.py，0022-0024 栈）

harness 形态对齐 cc-haha 真实客户端（代理省略 max_tokens、单 system、tool_result→role:tool、
thinking→reasoning 映射）；L 臂内嵌 reasonix 事故 .vue 代码块 + CC Bash schema。

**Arm L 长参数（Write .vue / Bash heredoc，n=2×5 档）**：
| 档 | 结果 | 定性 |
|----|------|------|
| L-W2k / L-W8k（Write 事故块 2k/8k 字符） | 3× NO_TOOL_STOP + `dsv4_flags=["pseudo_tag"]` | **事故链复现**：TYPE-B→无约束尾→畸形信封（`<｜DSML｜tool_calls<invoke` 缺 ｜DSML｜）→salvage-cap 截断→原文泄漏；0022 正确标记 |
| L-W32k（66k 字符参数 > max_tokens） | 2× FAIL_0023（截断 args + finish=tool_calls，两轮 arglen 66629/66654 不等） | 参数预算类失败：模型产出≠请求内容，客户端应分块写；非 0025 目标 |
| L-B4k / L-B12k（Bash heredoc 事故形态） | 4× TOOL_OK | — |

**Arm R 真实累积循环**（模拟 FS + 真实工具输出回放 + 14 步 checklist）：
213k / 48 轮，**信封失败 0**（14 TOOL_OK 完成全部步骤；34 次 NO_TOOL_STOP 均为
"checklist 已完成"的合法收尾叙述，flags=-、无伪标签）——真实工具输出 + 深度累积
仍不足以触发；触发要素是 **单发超长参数（Write 大文件）**。

**Arm X 边界矩阵**（10 边 × n=3）：30/30 全清（TOOL_OK 27 + OK_EMPTY_ARGS 3；
32k 超大结果注入、DSML 汤投毒、伪标签历史、5 连失败、raw-DSML 历史、空参工具、
并行调用、转义、混合叙述均无退化）。

**根因闭环**（L 臂 + 单发探针 + 服务器日志三方一致）：
spec 投机块含 FSM 拒绝 token 且 FSM 未终止（TYPE-B）→ 0013 提交整块并放弃约束 →
0014 64-token salvage 预算内模型重发**畸形**信封 → salvage-cap FINISHED_STOPPED
截在参数中段 → 解析失败 → finish=stop + 原文泄漏（0022 pseudo_tag）。
0023 无法覆盖（畸形形态不开槽）。

**修复 = 0025 typeb-finish**（F3b，patches/0025-typeb-finish.patch，已 staged）：
`DSV4_TYPEB_POLICY=finish` 时 TYPE-B 只保留 FSM 已接受前缀并当轮完成请求
（TYPE-A 同款 desync-safe 路径）；客户端得到"干净的可重试截断"
（`dsv4_flags=["typeb_cut"]` 新标志）而非畸形尾。默认 commit = 0014 原行为。
待部署 A/B：期望 L-W2k/W8k 的 NO_TOOL_STOP+pseudo_tag → TYPEB_CUT / TOOL_OK。
