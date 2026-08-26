# BreezeTravel 文档索引

本文只说明文档位置和效力，不定义新的产品目标。

## 当前权威顺序

1. [`../AGENTS.md`](../AGENTS.md)：高频硬约束、不可变量、HITL 与停止项；
2. [`product/PROJECT_CHARTER.md`](product/PROJECT_CHARTER.md)：用户、价值、范围、非目标与长期方向；
3. [`product/TRIP_CHECK_SPEC.md`](product/TRIP_CHECK_SPEC.md)：完整流程、输入输出、失败行为与 Advice 合同；
4. [`governance/PORTFOLIO_MISSION.md`](governance/PORTFOLIO_MISSION.md)：面试证据目标与禁止包装边界；
5. [`governance/PROGRAM.md`](governance/PROGRAM.md)：预批准阶段、自动推进和仍需现场 HITL 的事项；
6. [`governance/CURRENT_GOAL.md`](governance/CURRENT_GOAL.md) / [`governance/ROADMAP.md`](governance/ROADMAP.md) / [`governance/RELEASE_GATES.md`](governance/RELEASE_GATES.md)：唯一开发切片、阶段顺序和放行合同；
7. [`adr/`](adr/)：已接受的架构决策；
8. 当前 commit/config 对应的 evidence。

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

## 历史方案

Final 2.0 已归档到 [`archive/plans/BreezeTravel_双入口可验证行程产品与架构重构最终方案_2026-08-20.md`](archive/plans/BreezeTravel_双入口可验证行程产品与架构重构最终方案_2026-08-20.md)。原路径保留迁移说明，避免历史链接失效，但不再具有权威效力。

旧版 Evaluation、Evidence 和 Reliability 说明已归档到 [`archive/evidence-2026-08-22/`](archive/evidence-2026-08-22/)。同名当前路径只定义 V1 合同与实时索引。

文档结论冲突时必须先修正权威文件或新增 ADR，不得按对当前实现更有利的口径解释。
