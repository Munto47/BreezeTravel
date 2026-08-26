# COMPLETED GOAL：P0-G02 纵向 Program 指导与合同重构

## Metadata

- Goal ID：`TC-P0-G02`
- Program ID：`TC-V1-INTERVIEW-2026`
- Phase：`P0`
- Status：`COMPLETED`
- Branch：`codex/trip-check-p0-vertical-program`
- Baseline commit：`d0f786744f0f011c2e8bbca7bdf8ffc5037f7558`
- Approved by / at：用户批准完整实施方案 / 2026-08-22
- Predecessor gate：`TC-P0-G01 COMPLETED`

## Outcome

仓库形成一套可直接驱动 P1～P6 和 H1 的面试优先 Program：第 6 周前交付文本纵向闭环，pilot 从首个切片开始，架构/API/数据/Gate/自动推进均无需实现者临场决定。

## Scope

- 重写仓库指导、架构、产品规格、Roadmap、Release Gates 和 Goal 模板；
- 新增 Portfolio Mission、Program、模块化单体 ADR 和固定 API 合同；
- 归档已完成 Goal 和漂移的 Evaluation/Evidence/Reliability 说明；
- 更新 README、文档索引与能力状态口径；
- 仅修改文档，不实现 API、schema、migration、依赖或运行代码。

## Acceptance result

- 权威文件的主链、阶段、完成状态与 API 合同已收口：`PASS`；
- Roadmap 已将文本闭环和 18 pilot 前置到 P1：`PASS`；
- D1、Reliability、Solver、Evaluation、Candidate、H1 Gate 已定义：`PASS`；
- 旧文档已归档且当前证据未晋级：`PASS`；
- P1 Goal 可由 Program 固定模板生成：`PASS`。

## Verification result

- 权威 Markdown 链接审计：`PASS`；
- 冲突术语扫描：`PASS`；
- 文档常见凭据模式扫描：`PASS`；
- `git diff --check`：`PASS`；
- `python -m pytest tests/ -q`：`1227 passed, 25 skipped, 38 warnings`；
- `python -m ruff check app evals scripts tests`：`PASS`；
- `npm run build`：`PASS`；
- `python backend/scripts/validate_dual_entry_testset.py`：`structurally_valid=true`、`release_ready=false`；
- PostgreSQL、V1 snapshot、live Provider、浏览器恢复/性能、V1 release manifest、公网、真人：`NOT_RUN`。

## Completion record

- Commits：`40530ce`、`fc5beff`、`4fb16d8`，以及包含本完成档案的最终 P0 commit；
- Remote branch：`origin/codex/trip-check-p0-vertical-program`；
- Evidence：权威文档、当前 V1 Evidence 索引、命令原始终端输出和远端提交；
- Next Goal：`TC-P1-G01-text-vertical-slice`；
- Promotion：仅允许 fast-forward 到 `codex/trip-check-v1-program`，不合并 `main`。
