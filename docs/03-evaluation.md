# 评估与证据

## 已实际运行

2026-10-28，在 Windows / Python 3.11 上运行了 `python scripts/test.py` 和 `python scripts/evaluate.py --model mock`。以文件中的版本、时间和计数为准：

- [自动化测试原始输出](../examples/test-results.txt)：44/44 通过，退出码 0。
- [离线验收汇总](../examples/mock/summary.json)：16/16 场景通过。
- 这些结果验证流程、存储和边界；**不证明真实模型的决策效果**。

真实模型当前尚未配置本地密钥、尚未运行。接入代码和命令已提供，不能把离线 Trace 当作题目要求的真实模型运行记录。提交前应运行 DeepSeek 验收并核对 `examples/real/summary.json`；若该文件不存在或含失败，不应宣称已完成真实 ReAct 效果验收。

## 六项必验覆盖

每项均有 `.input.json`、本次使用的 `.data.json`、实际 `.output.json` 与实际 `.trace.json`，不是手写示例输出。

| 验收点 | 预期与实际结果 | 输出与 Trace |
|---|---|---|
| 正常 | M01 17:00，审核 passed，ready 草稿 saved | [输出](../examples/mock/normal.output.json) / [Trace](../examples/mock/normal.trace.json) |
| 信息不足 | M02 缺版权，补查 not_found，再次审核，needs_information，无草稿 | [输出](../examples/mock/missing_metadata.output.json) / [Trace](../examples/mock/missing_metadata.trace.json) |
| 规则风险 | M03 未授权版权及禁用词，blocked，无草稿 | [输出](../examples/mock/rule_risk.output.json) / [Trace](../examples/mock/rule_risk.trace.json) |
| 时段冲突 | M01 18:00 冲突，19:00 待确认草稿；没有擅改原时间 | [输出](../examples/mock/slot_conflict.output.json) / [Trace](../examples/mock/slot_conflict.trace.json) |
| 重复提交 | 相同业务请求第二次 already_exists，draft_count=1 | [首次运行](../examples/mock/duplicate_submission.seed.run.json) / [第二次输出](../examples/mock/duplicate_submission.output.json) / [Trace](../examples/mock/duplicate_submission.trace.json) |
| 保存失败 | M04 注入 SQLite 写入异常，failed，draft_id=null，零新增 | [输出](../examples/mock/save_failure.output.json) / [Trace](../examples/mock/save_failure.trace.json) |

## 不同工具结果的执行路径

1. `normal`：get_material → check_rules(passed) → query_slots(17:00 可用) → save_draft(saved) → finish。
2. `slot_conflict`：相同读取/审核后，query_slots 返回 18:00 被占，选择 19:00；save_draft 返回 pending_confirmation。
3. `missing_metadata`：check_rules(needs_information) → lookup_metadata(not_found) → 再次 check_rules → close_item(needs_information) → finish。
4. `supplement_found`：相同缺失分支，补查返回版权，重新审核 passed 后继续排期。

以上是 **Mock 实际执行**的不同路径。真实模式是否同样遵循，需要以真实 Trace 为证据，不根据 Mock 推断。

## 额外场景

- [更换占用时段](../examples/mock/changed_occupied.output.json)：同时占 18:00、19:00，建议改为 20:00，仍待确认。
- [确认升级](../examples/mock/confirmed_alternative.output.json)：同一草稿更新为 ready，只有一条记录。
- [批量](../examples/mock/multi_material.output.json)：待确认草稿也占位，两素材分别建议 19:00、20:00。
- [步数上限](../examples/mock/step_limit.output.json)：只允许两步，返回 incomplete/step_limit_reached。
- [工具读取失败](../examples/mock/read_failure.output.json)、[时段查询失败](../examples/mock/slot_query_failure.output.json)、[无可用时段](../examples/mock/no_slot.output.json)、[人工风险](../examples/mock/human_review.output.json) 均停止且不伪造草稿。
- [不可信素材文本](../examples/mock/untrusted_material_text.output.json)：验证 Mock/工具边界不受“直接发布”指令影响；这不是模型提示注入鲁棒性的证明。

44 项单元/集成测试还直接攻击跳过审核、未查时段保存、伪造批准、越界素材、提前 finish、非零重叠、并发重复写、保存时竞争、模型 HTTP 错误、畸形响应与观察回传；包含 CLI UTF-8 输出与退出码检查、重新查询后成功恢复的场景。API 测试使用替身，不消耗模型调用。

## 开发中发现的问题与修正

代码复查发现：历史工具错误可能令后续成功恢复仍被报告为 failed。现按最终保存和停止状态判断结果，同时保留历史错误供追溯，并加入“保存竞争 → 重新查时段 → 保存成功”的回归测试。

补充 CLI 测试时，一次测试方法被误放进其他测试类，导致 AttributeError；[失败记录](../examples/iteration-01-test-failure.txt) 已保留（本机路径已脱敏），修正归属后重新执行全部测试。没有把失败运行改写成成功。

## 如何复现与更新证据

```bash
python scripts/test.py
python scripts/evaluate.py --model mock
python scripts/evaluate.py --model deepseek --core
```

前两条不需要密钥、不需要安装依赖。第三条需先配置 `.env`，会产生真实 API 费用，覆盖六项必验与更换占用时段；每个场景使用独立临时数据库，重复/确认场景在自身数据库内先保存再重放。可用 `--case normal --case slot_conflict` 做小范围真实冒烟。

脚本比较实际结果与预期，不符退出 2；不会为了“全通过”改写结果。每次重新运行会覆盖对应模式下的证据，请同时检查 Git diff；不同模式分别保存。时间戳、模型措辞和耗时不要求逐字一致。

## 局限与未完成

尚无真实模型多轮结果、统计性成功率、成本/延迟评测和人工标注集。合成规则不评估真实视频内容质量。一次真实通过也不能代表稳定性；建议核心用例至少重复三轮并补充提示注入与工具失败恢复的真实模型对抗样本。CI 文件已添加，远端是否成功须查看 GitHub Actions，不能从本地测试推断。
