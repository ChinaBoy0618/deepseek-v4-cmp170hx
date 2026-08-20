## 概述

2026-08-20 09:56 CST,dsf 驱动的知识库蒸馏 agent 会话(73db6fa4)停摆。模型在部分轮次把工具调用降级为 `<bash>…</bash>` 伪 XML 文本而非结构化 tool_calls,客户端解析失败、无工具可执行。服务端引擎存活,14 小时窗口内 0 个非 200 响应——0007–0016 补丁栈保住了引擎,但未能阻止此类客户端侧工具格式退化。

## 环境

- 服务:760 容器 dsv4-a100(08-19 23:33 CST 起,0007–0016 全量,见 patches/APPLIED.md),DSpark×PP4
- 客户端:数智邮务知识库蒸馏 agent(docx 分类归档/提取验证)
- 会话跨 08-19 16:28 ~ 08-20 09:56,长上下文

## 时间线与证据(客户端逐字摘录)

1. 09:34:53 第一次伪格式(后来恢复过一次):

   > 继续。创建 docx 提取脚本并运行验证。
   > `<analyze>`用 Bash + python-docx 创建提取脚本…`</analyze>`
   > I'll run a python-docx extraction on the training regulation docx to verify text quality.
   > `<bash>`
   > python3 -c "import sys; … import docx; d=docx.Document(…)"
   > `</bash>`

2. 用户"继续"后 09:55 恢复正常 tool_calls([tool:Bash] 正常执行 ×2)
3. 09:56:18 末轮(会话死亡):

   > 提取质量良好。现在验证汇报材料类 docx 的提取效果——这是风格库蒸馏的关键素材。先看汇报汇编类的 docx 有哪些。
   > `<analyze>`…`</analyze>`
   > `<bash>`
   > ls "02_分类归档/汇报汇编材料"/*.docx | head -20
   > echo "--- 周报 ---"
   > ls "02_分类归档/周报例会材料"/*.docx | head
   > echo "--- 方案 ---"
   > ls "02_分类归档/建设方案材料"/*.docx | head -20
   > `</bash>`
   > 查看各高价值类的 docx 代表文件。
   > 我直接运行命令列出各蒸馏来源类的 docx 文件。
   > I'll list the docx files in the high-value distillation source categories.

   ——无 tool_use 块,会话停摆
4. 同期服务端日志:引擎正常;该时段全部 200 OK;另有无关请求的 "TYPE-A Grammar completed mid-block" 正常完成;无 500/无 OOV(129280)/无崩溃

## 根因分析

与 artflow-release-cleanup 会话中断(2026-08-19 16:42,另开 issue 报告)同根因:长上下文 DSML 协议 token 发射退化。本案细节:

- `<analyze>` 简单标签保留、工具信封降级为自创 `<bash>` 标签 + 中英重复意图——协议退化是选择性的,越结构化越先丢
- tool_choice=auto ⇒ 无 grammar 约束 ⇒ 服务端无拦截点(200 OK 畅通)
- 0007 宽容解析只覆盖"信封存在但畸形"(`_tool_calls` 变体);信封缺失时无物可解析,不在其设计范围内
- 间歇性(09:34 出现→09:55 恢复→09:56 再现),与概率性退化特征一致

## 残留缺口与建议

- [ ] 客户端 `<bash>`/裸命令兜底解析或自动重试(最直接)
- [ ] 服务端评估:tool_choice=auto 且带 tools 的请求是否可加轻量信封存在性检测(有损/成本待评估)
- [ ] 运维:会话 ~80k 主动 compact、降温度
- [ ] 蒸馏强化协议发射(本会话正在准备的素材即用于此)
