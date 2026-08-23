# 「行程查」V1 证据索引

> 当前结论：`D1 PASS / RELIABILITY GATE PASS / CONTROLLED_EVIDENCE_ONLY`

P1 文本纵向闭环已完成 D1；P2 六类故障、领域 Trace、真实 PostgreSQL 接管与本地受控浏览器恢复已完成 Reliability Gate。该结论不等于 V1 候选版放行：OCR、真实 Provider、360 数据、候选 commit 上的 G0～G6、公网和真人证据仍未完成。

## 当前权威证据

- P0 基线：[`governance/BASELINE_2026-08-22.md`](governance/BASELINE_2026-08-22.md)
- P1 D1 manifest：[`../backend/evidence/trip_check_v1/p1/d1_manifest.json`](../backend/evidence/trip_check_v1/p1/d1_manifest.json)，绑定 subject commit `dd70870a817b84f6364804a5701950c754728f4e`
- P1 90 秒演示：[`../backend/evidence/trip_check_v1/p1/DEMO_90_SECONDS.md`](../backend/evidence/trip_check_v1/p1/DEMO_90_SECONDS.md)
- P1 完成档案：[`governance/goals/completed/TC-P1-G01-text-vertical-slice.md`](governance/goals/completed/TC-P1-G01-text-vertical-slice.md)
- P2 Reliability Gate manifest：[`../backend/evidence/trip_check_v1/p2/reliability_gate_manifest.json`](../backend/evidence/trip_check_v1/p2/reliability_gate_manifest.json)，绑定 subject commit `920b165a404219b7f586296a37960920b1d17170`
- P2 六类故障 manifest：[`../backend/evidence/trip_check_v1/p2/reliability/reliability_manifest.json`](../backend/evidence/trip_check_v1/p2/reliability/reliability_manifest.json)
- P2 完成档案：[`governance/goals/completed/TC-P2-G01-reliable-run-and-trace.md`](governance/goals/completed/TC-P2-G01-reliable-run-and-trace.md)
- 能力状态：[`dual-entry/capability-status.md`](dual-entry/capability-status.md)
- Release Gates：[`governance/RELEASE_GATES.md`](governance/RELEASE_GATES.md)
- 当前 Goal：[`governance/CURRENT_GOAL.md`](governance/CURRENT_GOAL.md)

## P1 D1 结果

- 18 条 pilot：`18/18 PASS`，北京/上海/杭州严格 `6/6/6`；
- 错城/错 POI 自动接受：`0`；Repair 后新增 `BLOCKER/HIGH/UNKNOWN`：`0`；
- PostgreSQL migration、事务、并发、lease 接管、重启回读与副作用去重：`PASS`；
- 三城文本主链与 BJ-02 歧义确认：Playwright `4/4 PASS`；
- 后端全量测试、Ruff、前端 build、旧双入口结构回归及 P1 故障合同：`PASS`；
- 敏感信息扫描：`0` 命中；artifact index：`177` 项，均绑定 sha256；
- live Provider、公网 E2E、真人证据：`NOT_RUN`。

## P2 Reliability Gate 结果

- PostgreSQL canonical fault matrix：`6/6 PASS`；
- Provider timeout：3 次有界尝试；字段部分失败：1 次且不重试；两者均保留成功事实和 `UNKNOWN/UNAVAILABLE`：`PASS`；
- Run/Repair 幂等、并发 revision 单胜者、实际子进程终止后 lease 接管、config drift fail-closed：`PASS`；
- Domain Trace 必需字段覆盖率与 Domain/OTel 关联率：`100% / 100%`；敏感属性命中：`0`；
- 浏览器刷新、SSE 重连与重复/乱序事件去重：`4/4 PASS`；
- P1 pilot regression：`18/18 PASS`，三城 `6/6/6`；
- 后端全量：`1256 passed, 27 skipped`；PostgreSQL 集成：`9 passed`；Ruff、前端 build、旧双入口结构回归：`PASS`；
- 顶层 artifact index：`230` 项 sha256，证据提交前 `231` 个 Git index blob 与工作区字节一致；
- live Provider、公网 E2E、真人证据及候选版 G0～G6：`NOT_RUN`。

## V1 artifact 约定

每个 Run 目录至少包含：

```text
runs/<run_id>/
  runspec.json
  stage_events.jsonl
  provider_receipts.jsonl
  trace.jsonl
  snapshot.json
  replay.json
  metrics.json
  manifest.json
```

原始 artifact 保存稳定 schema、hash、生成时间和 commit；对外摘要必须脱敏，不暴露密钥、Authorization、原图、完整 Prompt 或原始用户输入。

## 证据等级

`unit_verified → integration_verified → snapshot_verified → live_verified → publicly_verified → user_validated` 只能逐层由实际运行晋级。fixture、自动 Judge、snapshot、live Provider、公网和真人证据不能互相替代。

## 历史证据

旧 Router/RAG/Planner/RC1 说明归档于 [`archive/evidence-2026-08-22/`](archive/evidence-2026-08-22/)。它们可用于 Legacy A 和历史追溯，不是当前产品声明。
