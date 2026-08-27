# 「行程查」V1 评测合同

> `LEGACY_BASELINE / NOT_VNEXT_AUTHORITY`：本文件冻结旧V1评测设计，只供历史复现。`TC-VNEXT-2026`的数据、模型选择和Gate以[`governance/PROGRAM.md`](governance/PROGRAM.md)与[`governance/RELEASE_GATES.md`](governance/RELEASE_GATES.md)为准；下列状态不得晋级Blueprint/V0.x。

> 状态：`ACCEPTED`
>
> 当前 V1 数据与候选运行：`NOT_RUN`

历史 Router、RAG、Planner、三城 RC1 和旧双入口结果已归档到 [`archive/evidence-2026-08-22/`](archive/evidence-2026-08-22/)，不得作为 V1 当前指标。

## 数据分层

| split | 数量 | 用途 | 规则 |
|---|---:|---|---|
| pilot | 18 | P1 合同、runner、oracle | 可版本化重建 |
| dev | 180 | 解析、规则、Advice、Solver 优化 | 允许开发读取 |
| regression | 72 | 已修复真实故障 | 只追加或审计更正 |
| frozen blind | 90 | 最终独立评估 | 开发 Agent、运行模型和 Judge 不读标签 |

北京、上海、杭州各 120。同源和变异案例保持同一 split。blind 失败只能形成 dev/regression 复现，不能修改 blind/oracle 消除失败。

## 固定变体

- Legacy A：旧 ReAct/Critic/RAG；
- Core B：TripCheck 固定 Workflow；
- Solver C：Core 加通过准入的 Repair 策略。

三组使用同一 RunSpec、输入、snapshot 和可执行 oracle。比较完整任务成功、错 POI/错城、HARD 漏检、UNKNOWN 保留、postcheck、unsupported claim、P95、token、成本和 replay hash。

## 运行绑定

每次评测保存 commit、config、model、Prompt、rules、provider、dataset、snapshot、fault profile、seed、预算和原始 case 输出。摘要不得脱离原始 artifact；不同 dirty tree 或不同绑定结果不得拼接。

## Judge

自动语义 Judge 使用隔离、无 API 流程，标记为 `automated_proxy_judge`。运行时模型不得评价自己；模型 Judge、真人标签、live Provider 和公网 E2E 是不同证据。
