# PREDEFINED GOAL：V0.3 Top-3 核心行程查

## Metadata

- Goal ID：`TC-VNEXT-G03-TOP3-AUDIT`
- Program ID：`TC-VNEXT-2026`
- Product version：`V0.3`
- Status：`DRAFT`
- Activation：G02 Map & Stay Gate通过并归档后
- Required gate：`Top-3 Audit Gate`
- Next Goal：`TC-VNEXT-G04-SCREENSHOT`

## Dependencies

- 唯一激活依赖是G02归档且Map & Stay Gate通过；随后G03置为`APPROVED`。
- 首个preflight填写branch/baseline，回读PlanRevisionRef、map/stay pointer和旧TripBrief/Itinerary日期约束；缺失lane标记`NOT_READY`而不伪造确认。

## User Outcome

用户只看到最值得处理的三个“必须调整 / 可以更好 / 需要确认”问题，并能预览、采纳最小修改。采纳后生成新行程版本，地图提示需要更新，完整复检后才显示已解决。

## Scope

- `031_day_index_trip_bridge.sql`、`MaterializationLineage`与正式materialize到首个ItineraryRevision；
- 地点存在/城市归属；
- 营业、闭馆、预约；
- walking/transit路线耗时和日容量；
- 酒店往返、午餐和晚餐空档；
- 有日期时的天气和风险；
- Top-3确定性排序；
- 用户友好Finding/Advice投影；
- Repair preview、EditCommand、新revision和postcheck。

## Pre-approved actions

- v3 materialize和Top-3用户结果；
- 复用现有Evidence/Audit/Advice/Repair；
- 必需的`031_day_index_trip_bridge.sql`，支持`ABSOLUTE|DAY_INDEX_ONLY`、nullable calendar与软人数来源；
- 现有高德/和风开发矩阵；
- 不新增风险搜索Provider。

## Decisions locked

- AuditEngine是Finding唯一权威。
- 内部保留全部MUST_ADJUST；公共页只展示前三项，剩余数量可见并按解决顺序补位。
- 无日期不生成具体天气/闭馆日期HARD。
- 用户结果最多3个，内部报告可保留更多。
- 具体地点候选必须来自冻结CandidateSet。
- LLM只表达已选事实和建议。
- 采纳后地图stale，不自动重算。
- 完整postcheck前不能显示已解决。

## Non-goals

- 截图；
- 时段/热门/夜景知识库；
- 用户记忆与分享；
- 新风险搜索Provider；
- 实时客流、医疗、订票、最低价。

## Acceptance

完全继承Top-3 Audit Gate：

- HARD漏检0；
- route precision/recall≥90%；
- CandidateSet/receipt绑定100%；
- 用户结果≤3；
- Repair后新增BLOCKER/HIGH/UNKNOWN为0；
- postcheck前错误“已解决”为0；
- 无日期的具体时效HARD为0；
- Provider局部失败保留成功事实。
- DAY_INDEX_ONLY可materialize且不伪造日期/确认；lineage、ETag、地图/住宿current pointer一致。

## Verification

- rule/oracle单测；
- Evidence freshness和partial failure；
- frozen CandidateSet；
- PostgreSQL materialize/audit/repair/postcheck；
- map stale联动；
- browser Top-3/preview/adopt/refresh；
- fault matrix和snapshot replay；
- PostgreSQL 031 fresh/existing、ABSOLUTE/DAY_INDEX_ONLY和旧数据兼容；
- H1/生产：`NOT_RUN`。

## Authority

- `AGENTS.md`、Charter、Spec、v3 API、Architecture；
- Program、Roadmap、Release Gates、Provider Admission、Risk Register；
- ADR-007、ADR-008、ADR-011、ADR-012及现有Audit/Repair ADR中未被取代的证据不变量。

## Baseline

- branch/commit/upstream、G02 subject与transition：激活时填写；
- 现场记录旧TripBrief日期/人数硬约束、Audit规则、CandidateSet与Provider版本；
- 历史Audit PASS不适用于新materialization lineage；H1/生产：`NOT_RUN`。

## Invariants

- AuditEngine是Finding唯一权威，LLM只表达；CandidateSet/receipt不可由模型补造；
- 所有HARD内部保留，公共Top-3不把剩余项显示为通过；
- `DAY_INDEX_ONLY`不生成日期天气/临时闭馆HARD，不伪造用户确认；
- materialize/repair使用CAS、幂等、lineage和新revision；postcheck前不得显示已解决；
- 采纳后地图为`NEEDS_UPDATE`，不自动路线调用；partial/UNKNOWN不算PASS。

## Budget

- Audit规则与Provider调用按RunSpec固定deadline/retry；具体地点候选最多来自冻结CandidateSet，不允许模型扩展；
- Top-3表达最多一次LLM调用且不得改变Finding/Repair；所有调用记账；
- 只允许现有无增量费用且已准入高德/和风开发矩阵；每切片checkpoint。

## HITL

新风险Provider、031以外公共schema、费用/账号/数据扩大、sealed oracle、H1/公网/生产/`main`需要批准；普通规则/迁移/测试故障自主修复。

## Checkpoint ledger

| 时间 | 用户结果 | Commit | Verification | Evidence level | Remaining | Risk/failure | Next autonomous action |
|---|---|---|---|---|---|---|---|
| 激活时填写 |  |  |  |  |  |  |  |

## Auto-advance

- Required gate：`Top-3 Audit Gate`；Next template：`TC-VNEXT-G04-SCREENSHOT.md`；
- subject push/readback、Gate PASS、clean tree、无Stop后，最终归档并原子激活G04；
- FUX-03、H1、公网、生产、商业和`main`不自动启动。

## Completion record

- Status / Subject commits / Remote branch：激活后填写；
- Verification / Evidence / Gate result / `structurally_valid`：激活后填写；
- User-visible result / Remaining risks / Goal archived / Next activated：激活后填写；
- Promotion decision：`NOT_REQUESTED`。

## Stop conditions

- 需要新风险Provider或未准入数据；
- 未解决MUST_ADJUST无法进入内部队列或公共剩余数量；
- 现有Audit权威必须被LLM替代；
- 需要降低HARD或receipt门禁；
- 日期缺失无法通过明确桥接保持UNKNOWN且不伪造日期。
