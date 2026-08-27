# ADR-007：语义行程中间态与严格用户投影

- 状态：Accepted
- 日期：2026-08-27
- Program：`TC-VNEXT-2026`
- 取代范围：ADR-004 中“先确认 TripBrief 再进入用户主链”的产品顺序；保留 Builder 冻结决定

## 背景

当前文本链把分段后的句子直接物化为 `RawStop` 并搜索 POI。长攻略中的 URL、预约说明、游览建议、出入口和描述句因此可能成为地点，随后页面要求用户逐项修复，并暴露原文、offset、hash 和内部流程。

旧 `TripIntakeRevision` 能保存城市、日期、人数和证据，但不能稳定表达每天的原子活动、计划/备选/引用/排除/经过角色，也把用户结果与内部证据结构混在一起。

## 决策

新增内部 `TripUnderstandingRevision`：

- `DestinationHypothesis`；
- `WorkingAssumption`；
- `DayDraft`；
- `ActivityMention`；
- `SourceClaim`；
- `InferenceReceipt`。

`ActivityMention` 必须区分 `PLANNED / OPTIONAL / REFERENCE / EXCLUDED / PASS_THROUGH`。只有带原子地点的 `PLANNED` 提及才能进入 POI 搜索。

新增 `UserFacingTripResultProjector`。公共结果使用字段 allowlist，只返回软假设、逐日卡片、地图准备状态、住宿建议和允许操作。原文、source span、confidence、model、provider、hash、revision、receipt 和内部阶段不得进入普通用户 API/DOM。

软默认不要求前置确认：最高概率城市、无日期时 Day N、无人数字段时默认2。它们保留来源，不成为 HARD。

理解态与正式行程使用统一 `PlanRevisionRef(kind, aggregate_id, revision, stop_set_hash)`。materialize前编辑只创建understanding revision；materialize事务写lineage并创建首个itinerary revision；materialize后编辑只创建itinerary revision。地图、住宿、ETag和current pointer均绑定完整引用，禁止以相同revision数字跨聚合误认当前版本。

## 后果

- 用户先获得可编辑卡片，而不是先填写治理表单。
- 内部证据仍可用于回归和追责，但不污染体验。
- 需要新增v3理解资源和严格序列化测试。
- v2和旧room/workspace保持兼容，直到V1稳定后另立删除Goal。

## 不采用

- 继续优化句子切分：无法表达语义角色，仍会搜索描述句。
- 让LLM直接输出POI：无法绑定Provider事实和错误校准。
- 把原文高亮作为用户解释：增加噪声且泄漏内部流程；解释通过用户友好地点详情提供。
- 强制用户确认所有低置信字段：提高流失；只对真正影响结果的局部歧义确认。

## 验证

以Text Card Gate证明整句/URL地点为0、严重错配为0、自动匹配precision≥99%，并通过公共JSON和DOM禁止字段扫描。
