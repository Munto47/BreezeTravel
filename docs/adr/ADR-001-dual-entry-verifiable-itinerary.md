# ADR-001：双入口可验证行程工作台

- 状态：Accepted
- 日期：2026-08-20
- 决策基线：[BreezeTravel 双入口可验证行程产品与架构重构最终方案](../BreezeTravel_双入口可验证行程产品与架构重构最终方案_2026-08-20.md)
- 适用范围：北京、上海、杭州；2～5 人；2～5 天国内城市自由行

## 背景

现有 BreezeTravel 已有推荐、Planner、约束验证、定向修复、Yjs 协同和证据回执，但产品事实仍分散在客户端行程、Planner 输出和 legacy `VerificationReport` 中。它能生成和检查部分计划，却不能可靠回答“当前权威行程是哪一版、报告基于什么输入、修改是否覆盖旧版本、修复后是否完整复验”。

## 决策

BreezeTravel 转型为面向小团体的可验证行程工作台，保留两个入口：

1. 导入已有 AI/手工行程，经过解析、实体消歧、审计和最小修复；
2. 从城市路线骨架开始，经过候选选择、结构化编辑和持续审计。

两个入口在创建 `ItineraryRevision` 后汇合。`TripWorkspace` 是聚合根，PostgreSQL 中的 revision、EvidenceSnapshot 和 AuditReport 是事实源。所有修改追加新 revision；LLM 只能辅助解析、解释和查询生成，不能决定事实、三态结论或直接覆盖 revision。

实施顺序固定为 P0 → P1 → P2 → P3 → P4 → M1。M1 真人门禁通过前，不进入 P5/P6 的完整模板拖拽建设。

## 结果

### 正向结果

- 行程修改、审计输入、证据和修复形成可回读的版本链；
- `UNKNOWN`、Provider 失败和证据冲突不再被文案掩盖；
- legacy Planner 与接口可以通过 adapter 渐进迁移；
- 导入入口和模板入口共享一个领域模型与 Audit Engine。

### 代价

- 需要新增三批数据库迁移和独立领域模块；
- 在迁移期保留 legacy/new 双读与 adapter，测试面扩大；
- P5/P6 受真人门禁约束，短期不会以拖拽 UI 作为完成标志。

## 不采用的方案

- 继续把自动生成完整攻略作为主产品：无法解决事实、版本和修复可信度问题；
- 只做导入后告警的检查器：缺少可预览、可撤销和复验的闭环；
- 一次性重写 Router、RAG、Planner、Yjs 和前端：回归面过大，且违反渐进迁移约束；
- 用 Yjs、localStorage 或 LLM 输出作为事实源：不能提供事务、权限和版本一致性。

## 验证与回滚

- 每个阶段按最终方案的完成门禁独立验证，不跨证据层级推断；
- legacy `/api/optimize`、`/api/edit`、`Itinerary` 和 `VerificationReport` 在 adapter parity 与调用量证据充足前保留；
- 新模块发生问题时停止新路由写入，旧接口仍可走 legacy 路径；数据库 append-only 记录不通过回滚删除。

