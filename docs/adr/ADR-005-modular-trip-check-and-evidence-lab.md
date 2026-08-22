# ADR-005：模块化行程核验单体与证据实验台

- 状态：Accepted
- 日期：2026-08-22
- Program：`TC-V1-INTERVIEW-2026`
- 取代范围：`docs/ARCHITECTURE.md` 中旧 Router/Planner/RAG 权威主链

## 背景

仓库已经拥有旧 ReAct/Critic、RAG、LoRA Router、Planner、Yjs 和大量历史评测资产，也拥有新的 TripWorkspace、ItineraryRevision、EvidenceSnapshot、AuditEngine 与 Repair 主干。两组资产同时出现在入口和说明中，容易把“代码存在”误写成当前产品架构。

「行程查」需要固定阶段、用户确认、三态事实、不可变证据、可恢复执行和采纳后的完整复验。它不需要无边界 Agent 自主决定事实，也没有拆分微服务、消息队列或 Kubernetes 的规模理由。

## 决策

1. 采用 FastAPI 模块化单体。PostgreSQL 是业务、Run、lease、幂等命令和 lineage 的唯一权威。
2. LangGraph 只编排固定 Workflow、HITL、SSE 和恢复；Provider 与数据库副作用仍使用稳定幂等键、事务和 receipt。
3. 旧 Agent/RAG/Planner 作为 Legacy A 保留最低回归，并只在 P5 消融中运行。
4. AuditEngine 是 Finding 唯一权威。LLM 只做解析、解释、风险归纳和 Advice 表达。
5. OR-Tools 先作为实验 RepairEngine：RoutingModel 处理固定地点和时间窗，CP-SAT 处理选择、锁定、软约束和最小修改成本；未通过 Solver Admission Gate 不进入默认运行时。
6. OpenTelemetry 记录脱敏领域 Trace；RunSpec、receipt、snapshot、replay 和 manifest 构成旁路证据链。
7. 拒绝新 Multi-Agent、微服务、Kafka、Temporal、Kubernetes、GraphRAG 和重新微调。

## 状态所有权

- PostgreSQL：TripBrief、ItineraryRevision、EvidenceSnapshot、AuditReport、Advice、Run、lease、幂等、receipt 和 manifest 索引。
- Redis：缓存、限流、可重建锁提示和短期去重；丢失不能改变业务结论。
- 临时文件系统：原始截图；任何终态都删除，数据库只保留 hash、OCR box、版本和清理回执。
- LangGraph checkpoint：可恢复的计算进度，不是业务事实或副作用提交记录。

## 后果

- P1 必须先交付文本纵向闭环，不能先完成所有横向基础设施。
- API、migration 和 Goal 只能追加到现有 revision/evidence/repair 主干。
- Legacy/Core/Solver 的结果必须使用同一 RunSpec 和可执行 oracle 比较。
- 新技术的简历价值来自准入实验和失败边界，而不是框架名称。
