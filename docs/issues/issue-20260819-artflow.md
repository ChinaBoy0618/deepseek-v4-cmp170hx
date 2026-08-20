## 概述

2026-08-19 16:42 CST,运行在 DSV4-Flash(dsf,dsv4-a100 @760, 端口 5700)上的 agent 会话 artflow-release-cleanup 在连续工作 111 分钟、249 条消息后停摆死亡。直接原因:模型末轮响应退化为纯叙述文本(重复意图、中英混杂),没有发出任何 tool_call,客户端无工具可执行,会话无法继续。服务端引擎未崩溃。

## 环境

- 服务:760 服务器容器 dsv4-a100,vllm fork c3046d1 + 补丁栈(0007–0016 演进中,当时为 0012/0013 开发期的 canary 树;现容器 08-19 23:33 重建,当时的实例日志已无法回取,以下时间线来自客户端会话记录)
- 客户端:dsf 驱动的编码 agent(tool_use id 形如 `chatcmpl-tool-*`)
- 负载:Element Plus 前端联调(playwright MCP 反复导航/截图),上下文 ~127k input

## 时间线(客户端证据)

1. 会话长程运行,后期输出质量明显下降
2. 死前数分钟:重复循环——"Let me reload / Lets reload / Let me reload the page…" 连续 20+ 次、同一意图中英文重复陈述
3. 16:42:48 最后一轮:文本

   > Page loaded. Let me right-click a generate node and open 多机位变体, then verify checkboxes now render. / Lets right-click a prompt node and open the variant menu. / Lets right-click a generate node and clic…

   ——只有叙述,无 tool_use 块
4. 会话终止,无 API 错误条目(turn_duration 6685887ms, 249 messages)

## 根因分析

长上下文 DSML 协议 token 发射概率性退化(与 0007 调查的 `_tool_calls` 泄漏、0013 调查的 tag soup 同一根因家族):

- DSV4-Flash 在超长上下文(~80k+)下,特殊/协议 token 的发射可靠性下降
- 本案表现:简单标签 `<analyze>` 保留,但工具调用结构完全丢失,降级为自由文本叙述(重复+语言混杂是高熵退化态的典型特征)
- tool_choice=auto 的请求没有 grammar 约束,伪文本畅通返回(200 OK),服务端无任何拦截点

## 已采取的措施(事后)

此事故直接推动了 0013(grammar 拒收 TYPE-A/TYPE-B 分流)与 0015(soup tripwire)的开发(见 patches/README.md "closing the unconstrained-tail leak")。0013 解决的是"有 grammar 的请求被放弃约束后垃圾尾部流出"的服务端部分;本案暴露的客户端部分(信封完全缺失)仍无服务端解。

## 残留缺口与建议

- [ ] 客户端(agent harness)增加兜底:对 `<bash>…</bash>`/裸命令文本做二次解析或触发重试
- [ ] 会话上下文 ~80k 时主动 compact(运维建议,已验证可显著降低退化概率)
- [ ] 蒸馏强化协议发射(风格库蒸馏计划正在进行)
- [ ] 复现脚本:超长上下文 + playwright 反复导航负载下观察退化概率
