# 「行程查」V1 证据索引

> 当前结论：`REJECT / BASELINE_ONLY`

P0 只证明指导与基线完成。TripBriefRevision、TripCheckRun、OCR、Advice、360 数据、G1～G6 候选复跑、公网和真人证据尚未完成；任何历史结果都不能让该状态晋级。

## 当前权威证据

- P0 基线：[`governance/BASELINE_2026-08-22.md`](governance/BASELINE_2026-08-22.md)
- 能力状态：[`dual-entry/capability-status.md`](dual-entry/capability-status.md)
- Release Gates：[`governance/RELEASE_GATES.md`](governance/RELEASE_GATES.md)
- 当前 Goal：[`governance/CURRENT_GOAL.md`](governance/CURRENT_GOAL.md)

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
