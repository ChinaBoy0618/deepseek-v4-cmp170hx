# Issue 1 — dsf 模型在 Claude 工具协议下泄漏整段 XML 工具调用伪标签：73db6fa4 会话分析

## 摘要

Claude Code 会话 `73db6fa4-e3bf-404f-b67d-67ee626eae14`（数智邮务文件整理/蒸馏工作，
后端模型 `dsv4s`）在工具调用频繁阶段，**整段原始 tool-call 协议的 XML 伪标签**
直接出现在模型对外可见的文本输出里，并最终**被截断在一个未闭合的 `</Bash>` 标签之后**，
会话终止时**没有产生任何真实 `tool_use` 块、也没有正常 stop reason**。

## 异常现象分类（全部来自 `dsv4s` 模型的可视输出）

### A. 整段 XML 工具调用协议泄到用户可见文本

会话尾部若干条 assistant 消息**没有发起任何真实 `tool_use` 块**（87 条 assistant 消息里
**33 条完全没有真实工具调用，占 38%**），只输出伪装的 XML：

```xml
<Write file_path="/tmp/extract_docx.py">
<content>

<analyze>Let me emit a real Write tool call with the actual content of the extractor script.</analyze>

I'll write the docx text extractor script now.
</content>
</Write>

<bash_command>
cat > /tmp/extract_docx.py << 'PYEOF'
...
PYEOF
python3 /tmp/extract_docx.py "..." 2>&1 | head -80
</bash_command>

<bash_command>python3 /tmp/extract_docx.py "..." 2>&1 | head -80</bash_command>

<answer>让我正确地写出提取脚本并运行。</answer>
```

在 Claude 协议里这些应作为 `<tool_use>` 块由后端转给客户端，本机跑不了。结果：客户端既
没拿到工具调用（也就无法执行），用户屏幕又被一堆伪 XML 污染。

伪标签出现量（统计自 87 条 assistant 消息的可视文本）：

| 伪标签                    | 出现次数 |
|---------------------------|---------:|
| `<thinking>` / `</thinking>` 嵌套 | 99 / 96  |
| `<analyze>` / `</analyze>` 嵌套 | 10 / 11  |
| `<Write `                  | 2        |
| `<call Bash: …>` / `<call Basi>` | 3（含被截断的 `<call Basi>`） |
| `<Bash command>` / `</Bash>`    | 1 / 1    |
| `<bash_command>` / `</bash_command>` | 多条 |
| `<answer>`                | 多次     |

> `<thinking>` 出现在最终给用户的 text 段是协议违规 —— 它本应走 `reasoning_content` 字段或
> 受控的 thinking content block，而不是被原样拼到 `content[].text`。

### B. 自身思考 / 元推理被写成可视文本

```xml
<analyze>Write is not producing a real Write tool call. I need to use Bash + python-docx
instead, which earlier worked fine. I'll write the script via a heredoc in Bash, then run it.</analyze>

<analyze>之前用 `cat > /tmp/xxx.py << EOF` 创建脚本 + `python3 xxx.py` 运行都成功
（collect_step1、classify、manifest 都正常）。我沿用同样方式创建 extract 脚本并运行验证一个 docx。</analyze>
```

`<analyze>` 是模型内部元推理，但同样以 XML 形式泄出来。

### C. 工具调用**写到一半就放弃**，下一轮还重复同一段

模型反复在同一段里"我应该发 Write"、"但 Write 没生效"、"那我换成 Bash heredoc" —— 同一段
伪标签文本被打字多次，且每次都没有真的工具块：

```xml
<call Bash: create script via heredoc + run>
<call Basi>   ← 此处连伪标签都没写完（"Bash" 被截成 "Basi"）
```

`<call Basi>` 这种连标签名都没写完的产物说明模型对自己的输出已经失去校验。

### D. 会话终止在 `</Bash>` 标签中间 —— **没有任何 tool_result**

最后一条 assistant 消息的尾部：

```
<Bash command>
cd "/Users/zhanguowang/Documents/数智邮务/我的输出/知识库蒸馏" && cat > /tmp/extract_docx.py << 'PYEOF'
...
PYEOF
python3 /tmp/extract_docx.py "02_分类归档/规章制度/4.培训规程_培训服务规范书_V1.0.docx" 2>&1 | head -60
</Bash>     ← 文件到这里就断了
```

后续 jsonl 里**没有任何 user 侧 `tool_result`、没有 `[Request interrupted by user]`、
没有正常的 assistant stop reason**。模型在写到 `</Bash>` 那一行时，dsv4s 服务侧的回复被
中断了 —— **是 hard stop，不是模型"决定停在这里"**。

## 模型 / 环境

- **会话时间**：2026-08-19T07:37:47Z → 08:29:20Z（约 51 分钟，模型纯 `dsv4s`，无回退）
- **容器侧证据**（只读 ssh 760T）：`dsv4-a100` 在 08-19 全天**无** EngineCore 死亡 /
  `Failed to advance FSM` / `Terminating` / `Traceback` / `OOMKilled`；`RestartCount=0`、
  `StartedAt=2026-08-18T20:20:30Z`。
- **结论**：本次异常**不是**引擎崩溃，**是 dsv4s 模型在响应中段退化**（伪标签协议漏出 +
  thinking 泄漏 + 半截标签），最后输出到 `</Bash>` 处时请求侧死亡。

## 期望行为

1. `<Write>`、`<Bash command>`、`<call Bash:>`、`<bash_command>`、`<answer>`、`<analyze>`、
   `<thinking>` 这些伪标签本应**全部不出现在最终给用户的 text 段**。它们应作为模型私有推理
   （thinking）或转成真实 `tool_use` 块再下发客户端。
2. 当模型判定"自己发不出有效 tool_use"时，应**显式停下来**，而不是把失败反复写到输出里。
3. 服务中断时客户端应收到 `[Request interrupted by user]` / 明确 stop reason / 至少一条带
   `error` 的 user 侧消息，而不是让 transcript 字面断在标签里。

## 建议排查 / 改进

1. dsv4s 后处理（SFT / 推理后格式化）里漏掉了 XML 工具调用解析路径：内部生成工具调用的
   streaming 输出未走 `<tool_call>…</tool_call>`（或对应平台 tool_call 通道）的提取分支，
   整段 XML 直接走到了 text 输出。
2. thinking 标签没有走 chat template 的 `reasoning_content` 字段，被原样拼回了 `content[].text`。
3. 截断处理：上层应在收到中断时给客户端一条 `error: {type: "interrupted"}` 的 user 消息并
   保留 stop_reason，而不是默默结束。