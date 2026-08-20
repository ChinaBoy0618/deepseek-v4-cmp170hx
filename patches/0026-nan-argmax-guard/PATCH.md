# 0026 — rejection sampler NaN→-inf argmax guard

> 直接采用 allover326 仓库 PR #10（作者 ZacharyZcR，分支 `nan-argmax-guard`，
> 上游来源 vllm-project/vllm@47a4e410b / #50183）。本地仅重编号入库，内容零改动。

## 改了什么

`vllm/v1/worker/gpu/spec_decode/rejection_sampler_utils.py` 两处，均在
masked load（`other=float("-inf")`）之后、`tl.argmax` 之前插入 NaN 防护：

- `_compute_global_target_argmax`（greedy 路径，~行 108）
- `_insert_resampled_kernel`（stochastic 路径，~行 851）

```python
local_max = tl.where(local_max != local_max, float("-inf"), local_max)
# 及
resampled_local_max = tl.where(
    resampled_local_max != resampled_local_max,
    float("-inf"),
    resampled_local_max,
)
```

利用 `x != x` 判 NaN，分支无关，无额外访存。

## 为什么

- **根因**：全 NaN（或 NaN-max）的 target logits 行使每块 local max 为 NaN，
  `tl.argmax` 返回 padded 区索引（≥ num_blocks）→ OOB 读 → 任意 token id 被
  **提交为采样输出**。
- **症状**（issue #9，与本项目"长上下文复读机"主诉吻合）：DSML 结构损坏、
  参数内连续 `0` 串（16,890 字符含 4,755 个 `0`、最长连 139）、"调用调用调用"
  文本级重复循环；关 DSpark 成功率 33%→89%。
- c3046d1 基线（2026-08-04）早于上游修复（2026-08-06），故我们的树必缺此修复。
- PR 作者实测：c3046d1 + 此 guard = 56/56 结构化 tool call 干净（含流式）；
  无 guard 时损坏率 35-65%。
- 与本地 0015-0019 绊线互补：那些是**事后止血**（检测到 soup 再截断），本补丁
  是**事前根因**（不让坏 token 进输出）。0020 clamp 管的是另一个面
  （verify 输出的 vocab 越界，改的是 rejection_sampler.py，不冲突）。

## 怎么测的

1. **fresh-apply**（0012 教训）：从 760 宿主树取干净
   `rejection_sampler_utils.py`（此前从未被任何补丁改过，grep 确认无 NaN 防护），
   `patch -p1 --dry-run` 零失败 → 实际应用 → diff 与 PR 原文逐字节一致。
2. **TDD**：`bench/tdd_0026.py` —— NaN 注入（真实 spec=5 块流）：无防护复现
  越界/重复，有防护干净（详见测试输出）。
3. **金丝雀**：@5700 重建后 hammer 200 + 连续工具调用 + issue 毒上下文回放。
4. **浸润**：24h watchdog 对比基线（typeA/绊线触发计数预期下降）。

## 结果

金丝雀 @5700（2026-08-20 19:03 上线，19 文件挂载）当日全绿：

- **tdd_0026 实机复现**（容器内 stock=.bak-0026 vs patched=挂载文件，真实
  spec=5 块流）：
  - stock 在 all-NaN 行输出 `999999`（= padded 区垃圾 id 被提交——issue #9
    复读机注入点的活体演示）
  - patched 输出 `1000`（确定性、界内）
  - 干净行 4/4 逐位一致（零回归）；混合 NaN 行正确选真实最大块
- tdd_consecutive_tools：PASS（A 9 轮 6 工具调用 0 stall；B 3/3；C 3/3）
- tdd_consecutive_stream：PASS（D 3/3+3/3；E 3/3）
- tdd_issue_replay：think_leak=0、soup/rep 绊线 0、TYPE-B 0、EngineDead 0
- hammer 200/200 ALL GOOD（8 臂 336s）

## 结论

**采纳。** 与 0015-0019 绊线互补：绊线在坏 token 已流出后截断止损，本补丁
让坏 token 根本进不了输出。观察期：watchdog 24h（预期长会话复读显著减少）。

## 回滚

- 摘掉挂载清单里的 `rejection_sampler_utils.py` → 回 0021 状态；
- `DSV4_NO_MOUNT=1` 一键回镜像。
