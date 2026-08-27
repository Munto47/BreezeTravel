# BreezeTravel「行程查」目标架构

> 状态：`ACCEPTED_BLUEPRINT`
>
> 架构版本：`Blueprint 1.0`
>
> 实现状态：`INCREMENTAL`
>
> 日期：2026-08-27

## 1. 架构目标

使用现有可靠后端资产，把“整句切分后逐句搜索 POI”的旧链替换为：

```text
Text / Screenshot
→ SourceDocument
→ TripUnderstandingRevision
→ DayDraft / ActivityMention / SourceClaim
→ ExecutablePlaceMention
→ PlaceResolution
→ UserFacingTripResult
→ MapRenderSnapshot
→ ItineraryRevision
→ EvidenceSnapshot
→ AuditEngine
→ Top-3 Finding
→ RepairOption / EditCommand
→ 新 Revision
→ 手动地图更新 + 完整 postcheck
```

采用 FastAPI 模块化单体、Next.js/React 和 PostgreSQL。HTTP、领域服务、固定工作流、Provider adapter、评测 runner 与后台任务部署在同一应用边界，不为技术展示拆成微服务。

## 2. 分层与依赖方向

```text
Public Web / Miniapp
    ↓ user-facing contracts only
Application Services
    ↓
Understanding / Itinerary / Map / Stay / Audit domains
    ↓
Repositories + Provider ports
    ↓
PostgreSQL / Redis / Qwen / AMap / Weather
```

公共 UI 不能直接消费内部 domain model。`UserFacingTripResultProjector` 负责把内部 revision、匹配、地图和 Finding 投影为用户语言，并通过字段 allowlist 防止原文、证据和内部术语泄漏。

内部诊断读取 repository/receipt，不复用公共结果组件；生产默认关闭且必须独立授权。

## 3. 核心对象

```text
TripUnderstanding
  ├─ TripUnderstandingRevision
  │    ├─ DestinationHypothesis
  │    ├─ WorkingAssumption
  │    ├─ DayDraft / ActivityMention
  │    ├─ SourceClaim
  │    └─ InferenceReceipt
  ├─ PlaceResolutionReceipt
  ├─ UserFacingTripResult
  ├─ TripUnderstandingJob / Event
  ├─ MapRenderJob
  ├─ MapRenderSnapshot
  └─ StayRecommendationSnapshot

TripWorkspace
  ├─ ItineraryRevision
  ├─ TripBriefRevision (兼容/正式核验条件)
  ├─ TripCheckRun / RunSpec
  ├─ EvidenceSnapshot
  ├─ AuditReport / AuditFinding
  └─ AdviceBundle / RepairOption / EditCommand
```

`TripUnderstandingRevision` 是用户输入到可信卡片之间的内部权威层。它不会替代正式 `ItineraryRevision`，而是在 materialize 前承载不完整日期、软人数、城市假设和非地点语义。

所有可编辑计划、地图和住宿对象都绑定统一引用：

```text
PlanRevisionRef =
  kind: UNDERSTANDING | ITINERARY
  aggregate_id
  revision
  stop_set_hash
```

materialize 前，commands 和 `StayAnchor` 只创建 `TripUnderstandingRevision`；G03 materialize 在同一事务内创建首个 `ItineraryRevision`、写入 `MaterializationLineage(source_ref, target_ref)` 并切换 current plan pointer。materialize 后的v3 commands只创建`ItineraryRevision`。ETag是不可逆不透明CAS validator，服务端绑定当前`PlanRevisionRef`但不序列化其中字段，不能跨kind重放。地图和住宿必须绑定完整引用，不能只写模糊的revision数字。

## 4. 语义编译

`StructuredInferenceProvider` 接收：

- task type；
- schema version；
- 经本地高风险字段遮蔽后的 `redacted_input_payload`；
- fixed model snapshot；
- prompt/config hash；
- deadline 和失败预算。

它返回结构化提案与 `InferenceReceipt`。服务端语义编译器负责：

- schema 再验证；
- 原文证据编译；
- 角色、日序和顺序一致性；
- 原子地点资格；
- 描述、预约、时长和路线主张分离；
- 冲突降级和确定性 fallback。

LLM不能调用地图工具、写数据库、选择最终 POI 或创建 EvidenceFact。

模型 adapter 默认支持 Qwen OpenAI-compatible API；DeepSeek adapter保留为冻结 Baseline。业务层不得出现模型专属 response 字段。

## 5. 地点解析

`PlaceResolutionService` 只接收 `ExecutablePlaceMention`：

1. 确定性规范化和别名；
2. 可选的 LLM 查询改写；
3. AMap POI搜索；
4. 城市、类别、行政区和上下文过滤；
5. 候选排序与校准；
6. `AUTO_SELECTED / SUGGESTED / UNRESOLVED / PROVIDER_UNAVAILABLE`。

自动选择门槛按冻结 Validation/Blind 校准，不由模型自报 confidence 决定。严重错配为零容忍；覆盖不足通过待确认处理。

## 6. 用户投影

`UserFacingTripResultProjector` 使用显式 DTO allowlist：

- assumption chips；
- daily place cards；
- friendly availability/status；
- map readiness；
- stay suggestion；
- allowed actions。

以下字段在序列化测试中必须不存在：source、quote、offset、confidence、model、provider、hash、revision、receipt、run、stage、Evidence、Audit、Repair、Postcheck。

资源 token仍可用于命令，但前端禁止渲染、复制或放入用户文案。

## 7. 可恢复理解任务与地图后台状态机

`POST 202 + SSE` 由 PostgreSQL 中的 `TripUnderstandingJob` 承载，保存状态、lease、attempt、事件游标、幂等创建回执和终态结果指针。进程重启后只接管过期 lease；事件重放不能再次调用模型、POI 或创建 revision。不能用进程内 background task 充当权威执行模型。

卡片 READY 后，应用服务写入可变的 `MapRenderJob(QUEUED)`，数据库 worker通过 lease接管：

```text
QUEUED
→ BUILDING
→ READY / PARTIAL / UNAVAILABLE
```

终态 job 产生不可变 `MapRenderSnapshot`；`STALE` 不是快照状态，而是快照 `PlanRevisionRef` 与 current plan pointer 比较得到的 freshness。任务绑定完整 `PlanRevisionRef`、route config hash 和幂等键。逻辑唯一键固定为：

```text
(trip_understanding_id, revision_kind, revision,
 stop_set_hash, route_config_hash)
```

即使客户端换了 `Idempotency-Key`，相同逻辑任务也复用已有 job/snapshot，不产生第二轮 Provider 调用。每条相邻边并行查询 walking/transit，保存规范化事实和短期 geometry ref。默认模式由确定性策略选择。

编辑流程：

```text
Revision N + Map READY
→ card command creates Revision N+1
→ latest compatible map remains N and projects NEEDS_UPDATE
→ no route provider call
→ user clicks rerender
→ idempotent MapRenderJob for N+1
```

公共 `MapReadinessView` 只使用 `PREPARING / AVAILABLE / NEEDS_UPDATE / LIMITED / UNAVAILABLE`；内部 job 状态和 freshness 不进入普通用户API。迟到 N 任务不能写 current N+1 pointer。SSE断线不取消任务；重复事件不产生副作用。不引入消息队列。

## 8. 住宿推荐

`StayAreaPlanner` 按冻结的 `StayScoringPolicyVersion` 工作。N日计划默认过夜日是Day 1…Day N-1；只有原文明确在最后一日继续住宿时才包含Day N。每个过夜日形成两条有方向的通勤边：`STAY_TO_FIRST` 和 `LAST_TO_STAY`。锚点使用GCJ-02坐标，经本地等距投影后求几何中位区域。`StayCandidateProvider` 按 2/4/8 km 和同城逐级查询，并使用版本化 `HotelBrandRegistry` 和类别过滤；每一层合格候选达到12家即停止扩圈，否则继续。

最多 12 家进入路线矩阵，评分为：

```text
total_best_minutes
+ 0.5 * max_single_leg_minutes
+ 8 * total_transfers
+ evidence_penalty
```

缺坐标、单向路线失败、双模式失败的惩罚值、上限和tie-break均属于版本化策略；未冻结前不得进入默认运行时。总分相同依次按缺失边更少、最差单程更短、canonical place ID排序。

`StayRecommendationSnapshot` 冻结候选与评分。公共投影最多展示3家，并只解释区域、首末站通勤摘要、最差单程、换乘数、证据缺口和简短推荐理由。选择后在materialize前创建新 understanding revision、materialize后创建新 itinerary revision，并共享同一 `StayAnchor`；地图投影为 `NEEDS_UPDATE`。住宿边进入下一次地图任务，最后一天默认不追加酒店。价格、房态、星级和服务质量不在 V0.2 权威范围。

## 9. 核验与建议

Provider adapters产生字段级事实、observed_at、有效期、规范化 hash 和失败类别。`AuditEngine` 是 Finding 唯一权威。

G03通过必需的日历兼容桥接支持：

```text
calendar_basis = ABSOLUTE | DAY_INDEX_ONLY
calendar_range = nullable
party_size_basis = EXPLICIT | SOFT_DEFAULT
```

`DAY_INDEX_ONLY` 可以进入不依赖具体日期的地点、路线、容量、住宿和用餐核验，但日期天气、临时闭馆和特定日期营业保持 `UNKNOWN`。它不是“用户已确认日期”，也不得为兼容旧表虚构日历日期。

```text
AuditFinding
→ rule_id / rule_version
→ EvidenceFact
→ EvidenceSnapshot
→ Provider receipt
→ RunSpec / config hash
```

Top-3排序由确定性 severity、confidence tier、actionability 和 itinerary impact完成；LLM只能把已选 Finding 与 RepairOption表达成用户语言。内部必须保留全部未解决HARD Finding。公共页一次只展示前三个；剩余项留在未解决队列并显示中性汇总“还有N个必须处理的问题”，绝不显示“已通过”。同原因、同一天且同一修复动作的HARD项可确定性聚合，解决后下一项自动补位。

模型解释不能创建 EvidenceFact；无来源、过期或冲突证据不能转成 PASS。采纳后必须创建新 revision、刷新相关 Evidence并完整 postcheck。

## 10. 状态所有权

- PostgreSQL：understanding/itinerary revision、run、lease、幂等命令、map/stay snapshot、receipt、evidence、finding、advice、lineage。
- Redis：缓存、限流、短期路线几何和可重建协调；丢失不能改变权威结论。
- 临时文件：原始截图；所有终态删除。
- SSE：用户进度投影；断线不取消后台工作。
- LangGraph checkpoint：可恢复计算进度，不是业务状态或 exactly-once 证明。

Provider副作用使用稳定幂等键、事务外调用和事务内回执写入。配置漂移创建新任务，不能拼接旧阶段。

## 11. 隐私与可观测性

日志和 Trace允许记录 opaque correlation token、task、版本、耗时、token、失败类别和 hash。禁止记录原图、完整 prompt、原始文本、密钥、Authorization、未脱敏 Provider 响应或个人身份字段。

发送模型前本地遮蔽手机号、证件号、订单号等高风险信息；映射只存在服务端受限范围。登录用户的原始文本和SourceClaim加密保存，默认最长30天或直到用户主动删除行程/账号，以先到者为准；终态后只保留生成卡片所需的结构化结果，30天后删除原文和可还原quote，仅保留不可逆hash、版本和删除回执。训练、评测或公共知识使用必须另行同意。

路线 geometry按 Provider条款短期缓存；正式持久化前取得许可。KnowledgeClaim与用户记忆分别执行来源许可和 consent Gate。

## 12. 资产处置

| 资产 | 决定 | 用途 |
|---|---|---|
| Revision/CAS/Idempotency/SSE/lease | KEEP | 新主链可靠性底座 |
| EvidenceSnapshot/AuditEngine/EditCommand/Postcheck | KEEP | 正式核验权威链 |
| TripIntake v2 evidence compiler | ADAPT | 复用证据与失败语义，不沿用强制确认 UX |
| 现有 AMap POI/route adapters | ADAPT | 原子地点、walking/transit、许可边界 |
| Workspace map projection | ADAPT | 替换几何虚线为 revision 绑定地图 |
| 手机验证码/邮箱认证 | KEEP | 新入口复用 |
| 房间首页与编号分享 | REMOVE_FROM_ENTRY | 保留兼容，不进入新用户主链 |
| Builder/Planner/Yjs/旧 RAG/ReAct/Critic/LoRA | FREEZE | 最低回归或消融 Baseline |
| 旧整句 ItineraryTextParser materialization | REMOVE_FROM_RUNTIME | 不得继续产生地点卡片 |
| 旧 Candidate/Intake evidence | ARCHIVE | 历史证据，不晋级新版 |

## 13. 技术准入

拒绝运行时多 Agent、微服务、Kafka、Temporal、Kubernetes、GraphRAG和重新微调。新技术只有在对应产品问题、固定数据、预设指标、失败降级和回滚都明确时才可进入默认运行时。

版本实施顺序与门禁以 `governance/PROGRAM.md` 和 `governance/RELEASE_GATES.md` 为准。
