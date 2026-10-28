# 素材审核与发布排期 Agent

小影校招 AI Agent 笔试 · **第 1 题**。帮助内容运营审核素材、检查排期冲突、补查资料并保存可确认的草稿。**不接真实发布，所有输出 `published=false`。**

真实模式使用 DeepSeek 原生函数调用：模型选择工具 → 执行 → 返回观察 → 模型再次决策。离线模式使用明确标记的确定性 Mock。

**当前验证：44 项测试、16 个离线场景通过；真实 DeepSeek 尚未配置密钥和执行。Mock 仅证明流程，不能证明真实模型决策效果。**

## 评审入口

- [需求与成功标准](docs/01-requirements.md)
- [方案、工具、动态决策与取舍](docs/02-solution.md)
- [评估与逐项证据索引](docs/03-evaluation.md)
- [AI 使用范围与个人贡献](docs/04-ai-usage.md)
- [复盘、用时与未完成项](docs/05-retrospective.md)
- [测试结果](examples/test-results.txt) / [离线验收汇总](examples/mock/summary.json)

## 环境与一键验证

Python **3.11+**，运行和测试均无第三方依赖。先进入仓库根目录：

```bash
git clone https://github.com/yuhang-824/Material-review-and-release-Scheduling-agent.git
cd Material-review-and-release-Scheduling-agent
python scripts/test.py
python scripts/evaluate.py --model mock
```

第一条测试命令写入 `examples/test-results.txt`；验收命令按场景生成输入、数据快照、实际输出和 Trace。每个验收场景使用独立临时数据库，防止上次运行污染结果。

## CLI 运行

可选安装入口，无第三方运行依赖：

```bash
python -m pip install -e .
python -m review_scheduler --input examples/mock/slot_conflict.input.json --model mock --output runtime/conflict.run.json
```

完全离线、不安装的 PowerShell 方式：

```powershell
$env:PYTHONPATH = "src"
python -m review_scheduler --input examples/mock/normal.input.json --model mock --db runtime/demo.sqlite3 --output runtime/normal.run.json
```

macOS/Linux：

```bash
PYTHONPATH=src python -m review_scheduler --input examples/mock/normal.input.json --model mock --db runtime/demo.sqlite3 --output runtime/normal.run.json
```

同一命令重复运行时复用数据库，第二次为 `already_exists`；要独立演示可指定另一个 `--db` 文件。默认数据 `data/fixtures.json` 包含 M04 的保存失败注入。

## 配置 DeepSeek 并验证真实决策

将 `.env.example` 复制为 `.env`，只在本地填写 `DEEPSEEK_API_KEY`。PowerShell：

```powershell
Copy-Item .env.example .env
```

默认 `MODEL_BASE_URL=https://api.deepseek.com`、`MODEL_NAME=deepseek-flash`；如账号支持的模型不同，修改本地变量。`.env` 已忽略，不提交密钥。

```bash
python scripts/evaluate.py --model deepseek --case normal --case slot_conflict
python scripts/evaluate.py --model deepseek --core
```

小范围命令先验证两条真实路径；`--core` 覆盖六项必验和更换占用时段，会进行实际 API 调用。结果保存在 `examples/real/`，必须检查 `summary.json` 的实际通过率及各 Trace。真实模式异常会明确返回 `incomplete`，不会偷偷切换为 Mock。

已安装入口后也可运行单个真实请求：

```bash
python -m review_scheduler --input examples/mock/slot_conflict.input.json --model deepseek --db runtime/real.sqlite3 --output runtime/real.run.json --max-steps 32
```

## 输入与确认

```json
{
  "request_id": "example-001",
  "material_ids": ["M01"],
  "requested_at": "2030-06-01T18:00+08:00",
  "channel": "main",
  "hard_time": true,
  "confirmed_alternatives": {}
}
```

合成既有排期占用 18:00，19:00 可用时，结果为建议 19:00、`pending_items=["confirm_alternative_time"]`、草稿 `pending_confirmation`，不会把原要求改成 19:00 或声称已发布。

运营确认后用**新的 request_id**提交同一请求，增加 `"confirmed_alternatives": {"M01": "2030-06-01T19:00+08:00"}`，复用相同数据库；原草稿升级为 `ready`。确认只对精确时间与对应素材生效。模型不能自行添加确认。

## 状态与退出码

| 字段/状态 | 含义 |
|---|---|
| review_status | passed / needs_information / blocked / human_review / not_checked |
| draft_save_status | saved / already_exists / updated / failed / not_saved |
| draft_status | ready / pending_confirmation；未保存时 null |
| status=completed | 全部处理完成且无待确认/失败 |
| status=needs_attention | 信息/规则/时间待人工处理；流程已正常结束 |
| status=failed | 已观察到工具失败，未伪报成功 |
| status=incomplete | 达到上限、模型异常或未正常结束；可能保留部分草稿 |

CLI 退出码：0=completed 或 needs_attention；2=failed 或 incomplete；1=输入/配置/存储错误。验收脚本根据预期判断，例如“保存失败”用例正确返回 failed 时算验收通过。

默认最多 32 步，每步一次模型选择及至多一次工具调用；允许 `--max-steps 1..200`。模型请求默认 45 秒超时。Trace 记录参数、实际返回、后续动作和一句依据，不保存模型内部思维过程。

## 目录

```text
src/review_scheduler/   模型接入、循环、工具、验证、SQLite、CLI
tests/                 可执行单元与集成测试
scripts/               测试与验收证据生成
data/                  合成素材、补查库、规则、既有排期与失败注入
examples/mock/         已执行离线输入、输出、Trace、数据库计数
examples/real/         运行真实验收后生成，不以 Mock 替代
docs/                  需求、方案、评估、AI 使用、复盘
.github/workflows/     Linux/Windows 自动验证
.env.example           本地密钥配置示例
```

边界：单机 SQLite、合成文本审核、固定一小时占位、四个候选时段；没有视频识别、鉴权、草稿取消/改版或生产部署。详细限制和真实验收缺口见评估文档。

提交信息：第 1 题；[GitHub 仓库](https://github.com/yuhang-824/Material-review-and-release-Scheduling-agent)；最终 commit SHA 用 `git rev-parse HEAD` 获取。提交前须由候选人补充真实个人用时、亲自修改与验证记录。
