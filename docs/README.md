# BreezeTravel 文档索引

本文只说明文档位置和效力，不定义新的产品目标。

## 当前权威顺序

1. [`../AGENTS.md`](../AGENTS.md)：高频硬约束、不可变量、HITL 与停止项；
2. [`product/PROJECT_CHARTER.md`](product/PROJECT_CHARTER.md)：用户、价值、范围、非目标与长期方向；
3. [`product/TRIP_CHECK_SPEC.md`](product/TRIP_CHECK_SPEC.md)：完整流程、输入输出、失败行为与 Advice 合同；
4. [`product/TRIP_CHECK_API_CONTRACT.md`](product/TRIP_CHECK_API_CONTRACT.md)：附加式v3 API和持久化目标；
5. [`ARCHITECTURE.md`](ARCHITECTURE.md)与[`adr/`](adr/)中仍生效的Accepted ADR：目标架构与决策；
6. [`governance/PORTFOLIO_MISSION.md`](governance/PORTFOLIO_MISSION.md)：面试证据目标与禁止包装边界；
7. [`governance/PROGRAM.md`](governance/PROGRAM.md)：预批准阶段、自动推进和仍需现场HITL的事项；
8. [`governance/AGENT_GATE_PROTOCOL.md`](governance/AGENT_GATE_PROTOCOL.md)：G01～G07隔离审查、裁决、sealed blind和证据命名；
9. [`governance/PRODUCT_MAINLINE_EXECUTION_GUIDE.md`](governance/PRODUCT_MAINLINE_EXECUTION_GUIDE.md)：主线优先、P2延期与两轮复审上限；
10. [`governance/CURRENT_GOAL.md`](governance/CURRENT_GOAL.md) / [`governance/current_work_packages.json`](governance/current_work_packages.json) / [`governance/ROADMAP.md`](governance/ROADMAP.md) / [`governance/RELEASE_GATES.md`](governance/RELEASE_GATES.md)：唯一开发切片、并行工作包、阶段顺序和放行合同；
11. 当前commit/config/dataset对应的evidence。

[`dual-entry/capability-status.md`](dual-entry/capability-status.md) 只报告证据状态，不改变产品范围。根 [`README.md`](../README.md) 是项目入口，不高于产品权威文件。

## 目录用途

- `product/`：当前产品章程与可执行规格；
- `governance/`：Portfolio Mission、Program、Roadmap、Release Gates、单一 Goal 合同和基线；
- `adr/`：仍生效或已被新 ADR 取代的架构决策；
- `dual-entry/`：历史开发协议、阶段报告与证据记录，日期报告不自动成为当前状态；
- `research/`：产品研究输入，不是完成证明；
- `runbooks/`：操作手册，使用前必须现场验证；
- `archive/`：被取代的方案和 Review，只供追溯，不驱动当前开发；
- `interview-review/`：面试复习资料，不参与产品门禁。

分支与worktree的统一边界见[`governance/BRANCH_CONSOLIDATION.md`](governance/BRANCH_CONSOLIDATION.md)。只有从当前登记的exact baseline创建、携带同一份`AGENTS.md`、Goal binding和work-package registry，且通过路径所有权校验的分支可以继续写入；不一致时只读。

## 历史方案

Final 2.0 已归档到 [`archive/plans/BreezeTravel_双入口可验证行程产品与架构重构最终方案_2026-08-20.md`](archive/plans/BreezeTravel_双入口可验证行程产品与架构重构最终方案_2026-08-20.md)。原路径保留迁移说明，避免历史链接失效，但不再具有权威效力。

旧版Evaluation、Evidence、Reliability、Security、Demo和RAG入口保持历史文件名以避免链接失效，但必须带`LEGACY_BASELINE / NOT_VNEXT_AUTHORITY`标记；它们不能宣称Blueprint/V0.x当前完成状态。旧版快照另存于[`archive/evidence-2026-08-22/`](archive/evidence-2026-08-22/)。

文档结论冲突时必须先修正权威文件或新增 ADR，不得按对当前实现更有利的口径解释。

G00的只读结构检查见[`governance/BLUEPRINT_VALIDATION.md`](governance/BLUEPRINT_VALIDATION.md)，独立审查处置见[`governance/BLUEPRINT_REVIEW_RESOLUTION.md`](governance/BLUEPRINT_REVIEW_RESOLUTION.md)；Agent Gate治理过渡的反方审查和修复边界见[`governance/AGENT_GATE_TRANSITION_REVIEW_RESOLUTION.md`](governance/AGENT_GATE_TRANSITION_REVIEW_RESOLUTION.md)。这些文件是治理证据，不是产品、真人或生产证据。
