# 三 Issue 修复分析报告（2026-08-19）

基于 0012-0016 补丁栈的现场证据（watchdog CSV + 容器日志），对 issues/01-03 逐一归因并给出分层修复方案。

## Issue 1 — 73db6fa4：XML 伪标签泄漏 + `<thinking>` 泄漏 + `</Bash>` 处硬停

### 证据链（新取，本地 15:37-16:29 = 会话 07:37-08:29Z）

| 指标（会话 2h 窗口） | 值 | 含义 |
|---|---|---|
| HTTP / 崩溃 / Traceback | 200 / 0 / 0 | 引擎全程健康（与 issue 结论一致） |
| **TYPE-B salvage 触发** | **30** | 真实 Claude Code 流量首次大规模激活 salvage 路径 |
| **salvage-guard armed** | 30（1:1） | 0014 护栏全部武装 |
| **salvage-cap hit** | **8** | 8 个请求的 post-salvage 无约束尾巴烧满 64 token 预算后才被截停 |
| degenerate-signature | **0** | 旧签名表对新伪标签**全盲** |
| soup-tripwire | **0** | 同上，0015 绊线全盲 |

### 根因三层

1. **入口 = TYPE-B salvage 尾巴**（0012 语义：FSM 存活但草稿违规 → 放弃约束整块提交）。真实流量下 2h 内 30 次，每次产生最长 64 token 的无约束尾巴——这是伪标签的直接产床。
2. **盲区 = 签名表不含本批伪标签**。0015 表只有死会话（93fc20ce）的 10 个签名；本会话的 `<Write `、`<bash_command>`、`<call Bash`、`<answer>`、`<analyze>`、`<thinking>`（带 -ing 的幻觉变体，合法 reasoning 标签是 `<think>`）全部漏网 → watch/绊线零触发。
3. **`</Bash>` 硬停 = 我方 budget 截停的副作用（高概率）**。cap-hit 在第 64 token 处 `FINISHED_STOPPED`，截断点落在伪标签中间 → 流式客户端看到的是「文本突然断在 `</Bash>` 中间 + 无正常收尾」——与 jsonl 现象完全吻合（8 个候选请求）。不是引擎死亡，也不是网络断连。

另：`<thinking>`×99 出现在 text 段 = reasoning parser（deepseek_v4）只认 `<think>`，不认幻觉变体 `<thinking>`，未剥离直接拼进 content。

### 修复方案（分层，P0→P2）

**P0-a 签名表扩展（0017 主体，低风险高收益）**
表加入：`"<Write "`、`"<bash_command"`、`"<call "`、`"<answer>"`、`"<analyze>"`、`"<thinking>"`、`"</assistant>"`、`"<assistant_unitsummary>"`、`"<system-reminder>"`。
安全论证（吸取 0015 v2 血泪教训）：
- 均为输出侧幻觉标签：DSML 合法语法集（`<｜DSML｜invoke/parameter/tool_calls>`、`<think>`、`</think>`）与它们零交集；OpenAI/Anthropic 结构化输出永不以字面 XML 出现在 text。
- `<call ` 带尾空格避免误伤 `<callable` 类代码；`</assistant>`/`<system-reminder>` 在**模型输出**里出现必为泄漏（prompt 侧注入不经过 `_output_token_ids`，不构成误杀源）。
- streak=12 语义天然抗误杀（一次性引用 ≤4 次窗口存活）。
- 但必须跑「合法输出探针」回归：让模型输出含 `<Write ` 的合法代码/文档（如本分析报告本身）不得触发——streak 框架已验证过该性质（元组回显测试）。

**P0-b cap-hit 截点改良：预算耗尽时优先回退到「最后一个干净边界」**
现在 budget=64 耗尽即截 → 落在标签中间。改良：cap-hit 触发时，向前回扫已提交 token 的解码尾部，若末尾处于未闭合 `<...` 标签内则再多截掉该标签起始以来的 token（上限 ≤16）——消除 mid-tag 断尾观感。仍满足同迭代截断不变量。

**P1-a reasoning 变体剥离（parser 层，0007 家族）**
deepseek_v4 reasoning parser 增加 `<thinking>`/`</thinking>`（及流式半标签）识别 → 走 reasoning_content，不进 text。注意与 P0-a 的 `<thinking>` 签名共存：parser 剥离后 text 无该串，签名永不命中——签名只是兜底。

**P2 伪标签 → 真实 tool_use 转换（issue 期望行为 1，复杂，单独立项）**
在 tool parser（0007 宽松解析家族）里把 `<bash_command>cmd</bash_command>`、`<Write file_path=...><content>...` 结构化捕获并转成真实 tool_calls 块。价值：把「假调用」救成「真调用」。风险：内容任意（heredoc/嵌套引号），解析失败路径必须退化为 P0-a 截停而非半转换。建议先收集 10+ 真实样本再定解析子集。

**P1-b 中断语义（issue 期望行为 3）**
确认 cap-hit/TYPE-A 的 FINISHED_STOPPED 在 /v1/messages 流式下带给客户端的 stop_reason 可见性；若 Claude Code transcript 不渲染，考虑在 finish 时附一条 text 尾注（可配置开关）。

## Issue 2 — 1cff1626：「我想做」循环 + 停止符泄漏 + 元标签伪造

### 归因分类（按可修性）

| 现象 | 归因 | 可修层 |
|---|---|---|
| 单消息内 `Lets reload.`×60、`Grep.` 高密度重复 | 上下文中毒后模型退化（5 天会话、多模型切换残留、dsv4s 段爆发）；无重复惩罚 | **服务端可测可截**（见下） |
| `</assistant>`+`nem`、`<assistant_unitsummary>`、`<system-reminder>` 字面泄漏 | 停止符/模板控制标签泄漏 | 签名表硬标签（并入 P0-a） |
| 跨消息「宣告不执行」循环（752 条无工具消息，每条短小正常） | 模型 per-request 输出合法，循环在**会话层** | **客户端域**（Claude Code 连续无工具 N 轮应中断/换策略）；服务端无法看见跨请求状态，诚实边界 |
| `no_repeat_ngram_size` 缺失猜测 | vLLM 采样参数全局默认未设 | **不建议全局开启**（伤正常输出：列表/代码/诗歌的合法重复会被惩罚） |

### 修复方案

**P1-c 行级重复绊线（0017 第二主体，复用 streak 框架）**
检测「短行重复」：解码本块新增+16 重叠窗口（复用 `_dsv4_soup_tags` 管道），若同一条 ≤6 token 的行（去空白）在窗口内出现 ≥5 次 → 计入专用 streak，连续 ≥6 块 → FINISHED_STOPPED。
- 对 `Lets reload.`×60（单消息内）:第 ~25-30 token 处即触发，60 行重复截到 ~5 行。
- 对 `Grep.`×326（跨消息、单条消息里只几次）：消息内不触发——由客户端连续无工具轮计数兜底（Claude Code 自身有类似机制，需用户侧确认）。
- 误杀防御：合法列表/代码行重复（如 `---` 分隔线、`end` 结尾）— ≤6 token 且 ≥5 连续同串的合法场景极少；分隔线 `---`/`===` 需显式白名单。默认 `DSV4_REPETITION_TRIPWIRE=1` 可关。

**P2 意图宣告→工具映射（issue 建议 3）**：与 Issue1-P2 同族（`Grep.`/`Bash.` 单句主语=工具名），同样归入「伪标签→真调用转换」单独立项，先采样后定子集。

## Issue 3 — ghosts_v002 DB 密码处理（工程卫生，独立轨道）

与 vLLM 栈无关，修复面在 **assistant 后端仓库**（本仓之外）：
1. 全仓扫描真实口令字面量（`grep -rniE 'postgres(ql)?[:_]?pass|PGPASSWORD|DB_PASSWORD=\S'` 类模式 + `:5432` 连接串）→ 全部替换 `${DB_PASSWORD}` 占位。
2. `.env.example` 只留占位 + 「到 760 运行时 env 取值」的注释（issue 已给出 docker inspect 命令）。
3. 后端启动逻辑：env 缺失即 fail-fast，禁止任何文件内默认值 fallback。
4. CI：secret 注入 `DB_PASSWORD`。
5. 文档：调试手册同步替换；将来轮换按 issue 的维护窗口流程。
**阻塞项**：需要确认 assistant 后端仓库位置（本机路径或 760 路径）才能落地；vLLM 侧无动作。

## 优先级与执行顺序建议

| 序 | 项 | 改动面 | 风险 | 预期效果 |
|---|---|---|---|---|
| 1 | P0-a 签名表扩展（9 新签名） | scheduler.py 一处元组 | 低（streak 已验证） | Issue1/2 泄漏类 → 截停率从 0 到有效 |
| 2 | P0-b cap-hit 干净边界回退 | scheduler.py cap-hit 分支 | 低 | 消除 mid-tag 硬停观感 |
| 3 | P1-a `<thinking>` parser 剥离 | reasoning parser | 低 | 99 次/会话的泄漏直接消失 |
| 4 | P1-c 行级重复绊线 | scheduler.py | 中（需白名单+探针） | 单消息重复循环截断 |
| 5 | P1-b 中断语义可见性 | 确认+小改 | 低 | 客户端可区分截停 vs 崩溃 |
| 6 | P2 伪标签→真工具调用 | tool parser | 高 | 先采样立项，单独评审 |
| 7 | Issue 3 | 外部仓 | 低 | 待仓库位置确认 |

验证配套：每个签名必须过「合法输出探针」（含该字面量的正常代码/文档输出不得触发）；全套电池（ctx 矩阵/agent loop/wide tools/锤子/浸润）+ watchdog 的 typeB/salvage/tripwire 列直接度量改善。

## 边界与诚实声明

- 服务端能做的是**限损与转换**：把污染尾巴变短、变干净、（P2 后）变成真调用；不能阻止模型在中毒上下文里开始退化。跨消息循环、会话卫生（压缩/重开）是客户端职责。
- issue-2 会话为多模型混跑（glm-5.2/MiniMax-M3/glm-5.3），退化归因 dsv4s 主要基于末段；修复对全部模型生效（服务端行为与模型无关）。
- P0-a 每个新签名都要重新走一遍 0015v2 教训检查：确认它不在任何合法输出语法集里（DSML、think、两协议结构化格式、代码常见形态）。
