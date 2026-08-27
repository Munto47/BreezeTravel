# 「行程查」v3 API 与持久化目标合同

> 状态：`ACCEPTED_TARGET`
>
> 版本：`trip-check-api-v3`
>
> 实现状态：`G01_DEMO_VERTICAL_SLICE_IMPLEMENTED / FULL_NOT_YET_EXPOSED`
>
> 日期：2026-08-27

## 1. 兼容与安全原则

- v3 是附加式合同；现有 room、workspace、import、revision、repair、suggestion 和 evidence 数据保持可读。
- 新首页只调用v3；现有未版本化 `/api/trip-intakes`、workspace/room/import/revision/repair等路径保持兼容。G01以基线OpenAPI snapshot冻结实际端点清单，不能用模糊“v2”称谓代替兼容测试；删除另立Goal并需人工批准。
- v3 不创建第二套“已解决”权威；正式 materialize 后仍使用 `ItineraryRevision → EvidenceSnapshot → AuditEngine → EditCommand → postcheck`。
- 所有创建和命令要求 `Idempotency-Key`；基于 revision 的写入要求 `If-Match`。
- 公共结果是严格用户投影，不返回内部证据、模型或流程。
- 服务端生成 canonical POI、路线事实、Finding 和 resolved 状态；客户端不能提交它们作为权威值。

## 2. 内部资源

### 2.1 TripUnderstandingRevision

必须包含：

- understanding ID、revision、parent revision、content hash；
- SourceDocument refs 与原始内容 hash；
- DestinationHypothesis、WorkingAssumption；
- DayDraft、ActivityMention、SourceClaim、ExcludedMention；
- inference binding、fallback、内部 source evidence；
- `DRAFT / PROCESSING / PARTIAL / READY / FAILED`。

内部 source span 以 Unicode code point 半开区间解释并由服务端验证。该资源不直接作为公共结果返回。

### 2.2 UserFacingTripResult

只包含：

- `assumptions: AssumptionChipView[]`；
- `days: TripDayView[]`；
- `map: MapReadinessView`；
- `stay: StaySuggestionView`；
- `available_actions`；
- 用户可理解的状态和消息。

`MapReadinessView.status` 只允许用户语义：

- `PREPARING`：正在准备；
- `AVAILABLE`：可查看且与当前计划一致；
- `NEEDS_UPDATE`：行程已修改，仍可看上次结果；
- `LIMITED`：部分路线可用；
- `UNAVAILABLE`：暂时无法生成。

公共结果不得返回 `QUEUED / BUILDING / READY / PARTIAL / STALE` 等内部生命周期枚举。

`StayCandidateView` 只返回地点名称、类别、地址/区域、到各过夜日首末站的通勤摘要、最差单程、总换乘、证据缺口、简短推荐理由和允许动作。不得返回内部评分、价格、房态、星级或质量承诺。

禁止字段：

- 原文、quote、source ID、offset；
- confidence 数字、model、prompt、provider；
- UUID/hash/revision/receipt/run/stage 的展示文案；
- Evidence、Audit、Repair 或 Postcheck 内部结构。

命令 token 可以作为不展示的 opaque 字段返回。

### 2.3 PlanRevisionRef、MapRenderJob 与 MapRenderSnapshot

所有commands、map和stay资源绑定：

```text
PlanRevisionRef {
  kind: UNDERSTANDING | ITINERARY
  aggregate_id
  revision
  stop_set_hash
}
```

materialize前v3命令只写understanding revision；materialize原子写 `MaterializationLineage` 并切换current pointer；之后只写itinerary revision。ETag是不可逆、不透明的CAS validator，服务端绑定完整引用；不得把kind、aggregate ID、revision或stop hash编码成可恢复JSON/Base64，也不得写入payload或DOM。

`MapRenderJob` 是可变任务，状态为 `QUEUED / BUILDING / READY / PARTIAL / UNAVAILABLE`，保存lease、attempt、event和失败。终态生成不可变 `MapRenderSnapshot`。`STALE`不是资源状态，而是snapshot引用与current pointer比较得到的内部freshness，并投影为公共 `NEEDS_UPDATE`。

绑定：

- 完整 `PlanRevisionRef`；
- canonical stops 与 coordinate binding；
- 每日颜色、顺序和缺失地点；
- walking/transit alternatives、selected mode；
- normalized route facts、response hash、短期 geometry ref；
- started/finished/observed/expires 时间；
- provider failures 与 idempotency receipt。

除请求幂等键外，逻辑唯一键为 `(understanding_id, revision_kind, revision, stop_set_hash, route_config_hash)`；不同请求键命中同一逻辑任务也必须复用，不能重复调用路线Provider。

### 2.4 StayRecommendationSnapshot

绑定同一 revision 和 MapRenderSnapshot，包含：

- overnight anchors；
- 2/4/8 km 或 citywide 搜索阶段；
- `HotelBrandRegistry` 版本；
- 最多 12 个内部 shortlist；
- 最多 3 个用户候选；
- 路线评分、缺失证据和 Provider 回执。

### 2.5 InferenceReceipt

绑定 task、schema、model snapshot、prompt hash、input hash、output hash、token、latency、repair call、fallback、error category 和估算费用，不保存密钥、完整原文或未脱敏响应。

## 3. v3 API

所有路径使用现有 `/api` 前缀。

### 3.0 G01 首个纵向切片边界

S0固定以下可公开实现范围，未列出的目标合同仍保持`NOT_IMPLEMENTED`：

- create请求在首切片只接受严格对象`{"mode":"DEMO"}`，未知字段拒绝；`FULL`保留为后续discriminated union分支，真实文本链完成前不得加入OpenAPI或返回fixture伪实现；
- create响应为`202 TripUnderstandingAcceptedView`：`public_resource_id`、`status`、`message`、`result_url`、`events_url`；资源ID是路由值，不承担授权，也不得渲染到DOM或分析事件；
- 匿名capability是独立随机秘密，经服务端签名后只写`HttpOnly`、`SameSite=Lax`、`Path=/` cookie；数据库只保存不可逆摘要，公共响应和日志均不含秘密；public profile额外要求`Secure`；
- result处理中返回`202 TripUnderstandingProgressView`，只含`status / message / retry_after_ms`；卡片可用后返回`200 UserFacingTripResult`和不可逆opaque ETag；
- `UserFacingTripResult`顶层严格为`status / assumptions / days / map / stay / available_actions`；activity严格为`activity_token / name / category / area_or_address / time_hint / status / available_actions`；
- events持久化单调游标并接受`Last-Event-ID`，首切片事件类型allowlist为`progress / result_available`，文案allowlist为“正在整理每天行程”“正在核对地点”“卡片已可用”；
- 首切片的`map.status`与`stay.status`均可诚实返回`UNAVAILABLE`，不得返回内部job/freshness枚举，也不得伪装成Provider正在执行。

首切片不改变后续commands union：写命令仍要求`If-Match + Idempotency-Key`，成功创建新revision和新ETag，并把已有地图公共投影改为`NEEDS_UPDATE`；旧API不成为v3权威。

### 3.1 创建与结果

- `POST /api/v3/trip-understandings`
  - 当前首切片只接受严格对象`{"mode":"DEMO"}`并使用固定北京示例；未来文本来源使用独立`FULL`分支，真实文本链完成前不进入OpenAPI；
  - 要求 `Idempotency-Key`；
  - 返回 `202`、随机非秘密`public_resource_id`、用户状态与events URL；它不含内部UID且不承担授权，不能进入用户文案或分析事件。访问日志必须记录路由模板或脱敏值，不能记录实际路径ID。
- `GET /api/v3/trip-understandings/{id}/result`
  - 返回 `UserFacingTripResult` 和 ETag；
  - 不返回内部理解资源。
- `GET /api/v3/trip-understandings/{id}/events`
  - SSE，支持 `Last-Event-ID`；
  - 只发送“正在整理每天行程 / 正在核对地点 / 卡片已可用 / 地图准备中”等用户事件；
  - 不发送模型、Provider、Run stage 或错误详情。

`POST 202 + SSE` 必须由持久化 `TripUnderstandingJob`、lease、attempt、event游标和终态结果指针支撑；不得依赖进程内临时任务。`DEMO`绑定HttpOnly、SameSite匿名capability，秘密值永不进入URL/JSON/日志；result/events/commands仍做资源级授权，未claim内容24小时后清除。G01必须实现一次性 `POST /api/v3/trip-understandings/{id}/claim`：用户登录后原子转移体验内容所有权、轮换resource ID并废止匿名capability；成功`200`返回新`public_resource_id`和不透明ETag，并用`Location`指向新资源，旧ID随后返回`410`。materialize、audit和share必须登录。

### 3.2 卡片命令

`POST /api/v3/trip-understandings/{id}/commands` 要求 `If-Match` 和 `Idempotency-Key`。

命令 union：

- `ACTIVITY_INSERT`；
- `ACTIVITY_DELETE`；
- `ACTIVITY_MOVE`；
- `ACTIVITY_TEXT_EDIT`；
- `PLACE_REPLACE`；
- `ASSUMPTION_SET`。

成功返回新的用户结果 ETag、changed days 和 `map_readiness=NEEDS_UPDATE`；不得返回内部freshness，也不得自动调用路线 Provider。

### 3.3 地图

- `GET /api/v3/trip-understandings/{id}/map-renders/latest`：返回最新快照的用户投影；旧revision只返回 `NEEDS_UPDATE` 和用户提示。
- `POST /api/v3/trip-understandings/{id}/map-renders`：为当前 revision 手动重绘，要求 `If-Match` 和 `Idempotency-Key`。
- 初次卡片 READY 后，服务端可使用内部命令自动创建一次地图任务；后续编辑不得自动创建。
- 相同请求幂等键重放返回原资源并增加 `Idempotency-Replayed: true`；不同键但逻辑唯一键相同仍复用同一job/snapshot。
- 迟到任务只能完成其绑定 revision，不更新 current pointer。

### 3.4 住宿

- `GET /api/v3/trip-understandings/{id}/stay-suggestions`：返回最多 3 个用户候选和搜索区域说明。
- `POST /api/v3/trip-understandings/{id}/stay-selection`：选择一个冻结候选，要求 `If-Match` 和 `Idempotency-Key`；按current `PlanRevisionRef.kind`创建新revision，并把地图投影为 `NEEDS_UPDATE`。
- 没有候选返回 `200` 和空列表、已搜索范围及下一动作，不返回红色错误。

### 3.5 正式物化与核验

- `POST /api/v3/trip-understandings/{id}/materialize`：在用户开始正式核验时，幂等创建或绑定 TripWorkspace/ItineraryRevision/lineage。
- 原子创建首个 `ItineraryRevision`、`MaterializationLineage`并切换current pointer；旧understanding地图只能作为旧版本查看，不能成为current。
- 日期允许为空；G03使用 `calendar_basis=ABSOLUTE|DAY_INDEX_ONLY` 的兼容结构，日期相关事实保持 UNKNOWN，不伪造TripBrief确认。
- 人数软默认可以进入非人数相关核验，但不能成为 HARD 来源。
- 正式 Audit、Advice 和 postcheck 复用现有权威主干，公共输出再投影为用户友好 Top-3。

### 3.6 Source、行程与账号旅行数据删除

- `DELETE /api/v3/trip-understandings/{id}/source`：仅登录owner；事务删除原始文本、可还原quote和PII映射，保留卡片、不可逆hash、版本与内部删除回执；成功`204`，之后source读取永久不可用。
- `DELETE /api/v3/trip-understandings/{id}`：登录owner或持有该DEMO HttpOnly session；事务删除understanding、source、匿名编辑和其map/stay派生资源，成功`204`；之后result/events/commands均返回`410`，不能用删除回执恢复业务值。
- `DELETE /api/v3/me/travel-data`：已重新验证身份的登录用户；级联删除所有v3 source、行程、map/stay派生数据和G01范围内的关联值，返回`202`与用户友好删除状态URL。
- `GET /api/v3/me/travel-data-deletion`：只返回`IN_PROGRESS / COMPLETED / RETRY_REQUIRED`及稳定下一动作，不返回内部表、job或receipt。完成后fresh readback不得出现业务值；最小审计tombstone不可包含原文、地点或可还原身份内容。

三类删除都必须做资源级授权、幂等、失败重试和缓存清理。用户看到“已删除”只允许在事务/级联完成后；内部删除receipt不进入公共API。

## 4. 通用响应与错误

- 创建任务：`202`。
- 幂等重放：原状态码或 `200`，响应头 `Idempotency-Replayed: true`。
- 缺少 `If-Match`：`428 IF_MATCH_REQUIRED`。
- revision 冲突：`409 REVISION_CONFLICT`，客户端回读最新结果。
- schema 无效且修复失败：固定 `200` + `PARTIAL_RESULT` 业务状态，返回可用卡片，不泄漏模型错误；不使用HTTP 206。
- Provider不可用：`200 PARTIAL_RESULT`，受影响字段为用户友好的“暂不可用”。
- 深核验范围外：`200 BASIC_ONLY`，不得以 422 阻断基础卡片。
- 隐私清理失败：内部 `PRIVACY_BLOCKED`，用户收到稳定删除/重试动作。

内部错误码与用户文案必须通过 allowlist 映射；未知异常返回通用可恢复消息和不展示的关联 token。

## 5. 持久化与 migration 路线

已执行 migration 001～027 保持不可变。

Program 预批准的附加式目标：

- `028_trip_understanding_v3.sql`：内部理解revision、持久job/lease/event、活动、主张、用户结果pointer、匿名session/ownership、source TTL/delete receipt和幂等命令；
- `029_map_render_snapshots.sql`：G01交付地图job、不可变snapshot、`PlanRevisionRef`、路线事实、逻辑唯一键和迟到保护；
- `030_stay_recommendation_snapshots.sql`：住宿区域、候选、评分和选择 lineage；
- `031_day_index_trip_bridge.sql`：G03必需的materialization lineage、`calendar_basis`、nullable calendar range与软人数来源桥接；不得虚构日期适配旧表；
- `032_knowledge_claims.sql`：仅在 G05 数据许可 Gate 通过后使用；
- `033_user_memory_and_feedback.sql`：仅在 G06 consent 合同通过后使用。

具体 Goal 激活前不得创建或执行对应 migration。migration 只追加；应用启动只检查兼容性，不自动执行 DDL。

## 6. 数据留存

- PostgreSQL：内部 revision、最小规范化 Provider 事实、hash、状态和 lineage。
- Redis/短期缓存：路线 geometry 和按 Provider 条款允许的临时响应。
- 日志：不记录原始文本、原图、完整 prompt、Authorization、密钥或可还原身份字段。
- Demo：精确示例 hash 可使用冻结回执；匿名编辑短期存在，保存前要求登录。
- 登录用户原始文本和可还原SourceClaim：加密保存，默认最长30天或直到用户删除行程/账号，以先到者为准；到期后保留不可逆hash、结构化结果、版本和删除回执。
- G01必须提供source主动删除、行程删除和账号删除的级联清理与可回读删除回执；G06只新增偏好和反馈consent，不延后source隐私。

本合同是目标接口，不代表当前分支已实现。
