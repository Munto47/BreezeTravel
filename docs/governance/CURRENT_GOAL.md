# IN_PROGRESS GOAL：I1～I4 Trip Intake v2 纵向闭环

## Metadata

- Goal ID：`TC-I1-I4-trip-intake-v2`
- Program ID：`TC-INTAKE-V2-2026`
- Status：`IN_PROGRESS`
- Branch：`codex/trip-intake-v2`
- Baseline：`d51d78fd004d46b105f05134c61d5fbee385c974`
- Approved by / at：User / 2026-08-26

## Outcome

实现 pre-workspace `TripIntakeRevision v2`、字段证据、用户确认、幂等物化和 120 条隔离 NLU 数据集；将当前单城市主链从三城/2～5 人/2～5 天放宽为任意国内城市和正整数人数/天数，同时保持事实、隐私、revision、receipt 和 postcheck 不变量。

## Scope

- v2 Pydantic/schema、LLM extractor 接口和确定性 validator；
- 不可变 PostgreSQL Intake/revision/source/materialization；
- 文本与截图 Intake API、确认、materialize、恢复和前端入口；
- 放宽 workspace/brief/itinerary 的城市、人数、日期与 day index 约束；
- 72 dev / 24 validation / 24 frozen blind 数据、validator、scorer、gate 和 v1 exporter。

## Non-goals

- 跨城 workspace、Builder/Planner/RAG/LoRA/Yjs 扩建、新基础设施；
- 扩大 live Provider 矩阵、公网、H1、production、main merge、release；
- 把文本 NLU Gate 当作 OCR、Provider、真人或全国 live 证据。

## Invariants

- 模糊解析只存在 Intake；权威 Brief 只含用户确认的精确值；
- LLM 不调用工具、不验真地点；确定性 validator 验证 schema、数量关系和逐字 source span；
- 缺失人数不得默认为 2；未提及偏好不得自动成为用户明确 `NO_PREFERENCE`；
- materialize 事务只创建数据库权威资源，Provider 调用在提交后且保持幂等/可恢复；
- 原图隐私、Evidence/Audit 权威、UNKNOWN/UNAVAILABLE、revision/stale/postcheck 合同不变；
- 当前工作区用户的未跟踪 `tests/` 不得读取为正式 oracle、修改或提交。

## Verification

- 新增模型、validator、extractor、repository、API、迁移和 scorer 定向测试；
- PostgreSQL migration/transaction/idempotency/restart readback；
- frontend build 与文本/截图/恢复浏览器链；
- 120 条 schema/distribution/family/span validator；sealed blind 严格 NLU Gate；
- 全量 backend pytest、Ruff、frontend build、dual-entry validator。

## Budget / HITL / Stop

- 外部 live Provider 增量调用：0；新增费用：0；
- LLM 开发验证优先使用 mock/fixture；真实运行模型调用不作为本 Goal release 证据；
- migration/API/schema 已由用户在本 Goal 请求中批准；
- frozen blind 修改、付费、外部数据扩大、公网/H1/production、跨城或降低 Gate 时停止请求批准。
