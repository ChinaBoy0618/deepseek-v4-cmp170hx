# Issue 2 — dsf 模型陷入"我想做"循环：artflow-release-cleanup 会话（1cff1626）分析

## 摘要

Claude Code 会话 `1cff1626-09ba-4df9-a617-425a2654fb3d`（artflow v0.0.1 release cleanup +
后续 UI 调试；后端在 08-14 → 08-19 多次切换模型，最终落到 `dsv4s`）在用 Playwright 做
storyboard 浏览器复测阶段，dsf 模型**陷入「想执行某动作但永不发出真实 `tool_use` 块」的
重复循环**，会话语义上是被截断，但模型本身的输出已经持续数十次只输出同一句话。

## 异常现象分类

### A. 显著的退化单行循环（按出现次数排序）

| 重复字符串            | 在可视文本中出现次数 |
|-----------------------|---------------------:|
| `Grep.`               | **326**              |
| `Let me do it.`       | **104**              |
| `Lets do it.`         | **65**               |
| `Let me grep.`        | **39**               |
| `Lets reload.`        | **27**               |
| `Let me call Grep.`   | **26**               |
| `Let me use Grep.`    | **21**               |
| `I'll grep.`          | **19**               |
| `I'll invoke Grep.`   | **17**               |
| `Let me reload.`      | **16**               |
| `I'll use Grep.`      | **15**               |

> "Grep." 不是工具调用 —— 模型把"调用 Grep 工具"这件事**只写成一句自然语言**而**没有
> 发出真实 `tool_use` 块**。这是该会话最大的退化特征：模型一直在"宣告自己要做什么"，
> 但 2717 条 assistant 消息里 **752 条没有任何真实 tool_use 块（占 28%）**，且越接近
> 会话末尾这个比例越恶化。

### B. 最长一连串重复（一段消息内部）

以下一段出自会话末尾（msg#750，单条消息内部就出现 60+ 行重复）：

```
vue-tsc passes (no output, clean exit). The fix is just a template binding name, and code
compiles. Let me reload the storyboard and retest the dialog. First accept any beforeunload.

Lets reload.

Let me reload the page.

Let me reload. The dialog state may have a beforeunload. Let me handle it.

Let me reload.

Lets navigate and handle the dialog.

Let me reload the storyboard.

Lets reload.

... （中间省略 50+ 行 Let me reload. / Lets reload.）

Lets reload.
```

从这一行开始，到本条消息结束，模型**没有发出任何 `tool_use` 块**，整段都是「让我重载」/
「让我执行」的重复宣告。**共 60+ 行 `Lets reload.` / `Let me reload.`**夹杂个别不同的前缀
说明（"the dialog state may have a beforeunload"），但**没有任何一次真的发出 browser reload
工具调用**。

### C. 会话尾部的 "do it" 末次循环 + 半截模型元数据

最后一段的退化更极端：

```
Let me do it.

Lets do it.

Let me do it.

...

Let me do the flow in one snippet.

Let me do it.

Let me do the flow now.

Let me do it.

...

Let me do the flow.

Let me do it.

Let me do it in one snippet once and for all.

Let me do it now.

Lets do the flow in one snippet.

Let me simply run the flow.

Let me do the flow.nem</assistant> I'll execute the flow in one code snippet.

Let me run the full flow: right-click a prompt node, click 多机位变体, then dump dialog
checkbox states.Let me run the full flow in one snippet: right-click a prompt node, click the
menu item, then dump dialog checkbox states.

<assistant_unitsummary>Running full F4 flow test</assistant_unitsummary>
```

异常点：

1. `Let me do the flow.nem</assistant>` —— 模型把**关闭 assistant 文本的 `</assistant>`
   标签字面打到了可见输出里**，还自带一个捏造的 `nem`（无意义截断）。这是 stop token
   校验完全失败。
2. 紧跟着又冒出一个 `</assistant>` —— **又是 token 泄漏**。
3. `I'll execute the flow in one code snippet.` 之后**只有承诺，从未兑现** —— 没有任何
   `tool_use` 块随之而来。
4. `<assistant_unitsummary>Running full F4 flow test</assistant_unitsummary>` —— 又一个
   **捏造的元标签**出现在用户可见文本里，Claude 协议里根本没有这个标签。
5. 紧接的那段文本"重复声明我要做 flow"，仍没有 tool_use。

### D. `<system-reminder>` 字面出现在可视文本里

artflow 会话可视文本里 `<system-reminder>` 出现 **24 次**，`</system-reminder>` / 其它 XML
痕迹若干 —— 与 Issue 1 同类型的 XML 协议泄漏，但本会话里更轻、更隐晦。

## 模型 / 环境

- **会话时间**：2026-08-14T06:29Z → 2026-08-19T08:42:48Z（约 5.1 天，多次 resume；后端模型
  在 `glm-5.2` / `dsv4s` / `MiniMax-M3` / `glm-5.3` 间多次切换）
- 退化集中在 08-19 早段（截断前约 2 小时），落到 `dsv4s` 后爆发。
- **容器侧证据**（只读 ssh 760T）：`dsv4-a100` 在 08-19 全天日志**无** EngineCore 死亡 /
  `OOM` / `Terminating` 记录；容器连续运行中，`RestartCount=0`。**即本次退化与引擎无关，
> 是 dsv4s 推理本身在长上下文 + 频繁工具调用后段的退化。**

## 期望行为

1. 模型"宣告下一步动作"的句子**不应取代实际的工具调用**。当模型在同一条 message 里重复
   `Lets reload.` 超过 ~3 次而没有工具块时，推理层应停止采样并提示"无法继续"。
2. 推理终止符（`</assistant>` / `<|end|>` / ``）不应泄漏到 text 输出。
3. 内部 `<system-reminder>` / `<assistant_unitsummary>` / `<parameter name="...">` 等元字段
   不应出现在最终 text 段。

## 建议排查 / 改进

1. dsv4s 长上下文（会话积累到数 MB / 数十万 token）下的**重复惩罚 / `no_repeat_ngram_size`**
   配置是否被关掉？`Grep.` 单 token 重复 326 次、`Lets reload.` 重复 60+ 次的现象与重复
   惩罚缺失高度相关。
2. 推理停止条件：是否只判断 `` token 出现，没判断"上一条 message 已无 tool_use 且整段是
   同一句话的复制"？
3. tool_call 提取与 text 输出的分流：本会话里 `Grep.` / `Bash.` / `Read.` 这种"以工具名
   做主语的自然语言短句"高频出现，提示 SFT/template 把"工具调用意图"识别为纯文本而不是
   触发 tool_use —— 后处理需要把这类"以工具名为动词开头的单句"映射回真实 tool_use 块，
   或在不能映射时显式终止而非循环。
4. 截断处理同 Issue 1：客户端应能区分"模型自己停在这里（`stop_reason: end_turn`）"和
   "服务中断（`stop_reason: error` + user 侧 error 消息）"。