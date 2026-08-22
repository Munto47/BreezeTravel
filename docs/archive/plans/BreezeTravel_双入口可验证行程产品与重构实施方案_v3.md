# BreezeTravel：双入口可验证行程产品与重构实施方案

> **状态：已被最终版取代。** 本文保留为方案演进记录，不再作为开发基线。后续实现与验收请以 [《BreezeTravel 双入口可验证行程产品与架构重构最终方案》](./BreezeTravel_双入口可验证行程产品与架构重构最终方案_2026-08-20.md) 为准。

> 版本：v3.0  
> 日期：2026-08-20  
> 文档性质：下一阶段产品目标、架构边界、代码重构和验收门禁  
> 适用范围：北京、上海、杭州三城的 2～5 人、2～5 天城市自由行  
> 事实边界：本文定义目标状态与实施路径，不代表其中能力已经全部实现或通过验收

## 1. 最终决策

BreezeTravel 不再把“AI 自动生成一份完整攻略”作为核心产品，也不只做一个导入后给出警告的行程检查器。

下一阶段统一定位为：

> **BreezeTravel 是一个面向小团体的可验证行程工作台。用户可以导入豆包、DeepSeek、ChatGPT 等生成的行程，也可以从城市路线骨架开始拖拽组合；无论从哪个入口进入，系统都会持续检查地点、时间、交通、营业、天气和成员约束，并提供有证据、可预览、可撤销、修复后重新验证的最小修改方案。**

面向普通用户的首屏表达为：

> **把 AI 攻略或旅行想法，变成真的能走、适合同行人、每次修改都有依据的行程。**

技术定位为：

> **Evidence-backed Collaborative Itinerary Verifier & Repair Engine**

产品采用两个入口，但只有一个核心系统：

| 用户状态 | 产品入口 | 入口目标 |
|---|---|---|
| 已有 AI、攻略或手工行程 | 导入 → 消歧 → 排雷 → 修复 | 尽量保留原计划，把问题找出来并局部修好 |
| 还没有完整行程 | 城市骨架 → 选择地点 → 拖拽编辑 → 实时检查 | 降低从零规划门槛，避免一开始就生成不可执行方案 |

两个入口在形成 `EditableItinerary` 后完全汇合，共享实体对齐、证据快照、审计、修复、协同和版本确认。

## 2. 项目要解决的问题

### 2.1 核心用户

首期用户固定为：

- 20～40 岁的旅行组织者；
- 为 2～5 人朋友、情侣、亲子或带父母出行组织 2～5 天城市自由行；
- 目的地限定北京、上海、杭州；
- 已经有一份粗略方案，或希望从常见城市路线快速开始；
- 最担心计划走不通、成员抱怨、营业/预约遗漏和自己承担全部核验工作。

### 2.2 核心任务

系统只承诺帮助用户完成以下任务：

1. 把非结构化行程或城市路线骨架变成可编辑的标准行程；
2. 对地点身份、城市、时间、通勤、营业和成员约束给出三态结论；
3. 明确哪些结论有证据，哪些仍需用户确认；
4. 在不破坏锁定项的前提下提供局部修复方案；
5. 每次修改后让旧报告失效，并对新版本重新验证；
6. 让同行人以低摩擦方式补充约束和确认取舍。

### 2.3 北极星结果

项目的核心结果不是“生成了多少地点”，而是：

> **用户用更短时间得到一份已经确认关键风险、满足成员硬约束、可以实际执行的行程版本。**

内部北极星指标为：

- `time_to_first_confirmed_itinerary`：从开始到首个可确认版本的时间；
- `accepted_high_risk_findings_per_audit`：每次审计中被用户或人工标注认可的高风险问题；
- `hard_constraint_miss_rate`：最终确认版本的硬约束遗漏率；
- `repair_adoption_rate`：用户采纳修复方案的比例；
- `report_share_or_reuse_rate`：报告被分享或临行复检的比例。

## 3. 不变的开发边界

以下边界是开发门禁，不是可选建议。

### 3.1 首期必须限制的范围

- 城市：北京、上海、杭州；
- 行程长度：2～5 天；
- 人数：2～5 人；
- 输入：纯文本行程、系统路线骨架、用户手动选择；
- POI：景点、餐馆、酒店、交通节点；
- 设备：桌面端支持拖拽，移动端必须提供按钮式等价操作；
- 证据：高德 POI/路线、天气、项目已审核来源和用户提供信息；
- 审计：只执行能够说明数据来源和判定边界的规则。

### 3.2 首期明确不做

- 不扩大到更多城市；
- 不支持任意截图、视频、复杂 PDF、Word、Excel；
- 不大规模抓取小红书、抖音或景区网站；
- 不承诺实时排队、精确人流、全平台最低价或自动代订；
- 不提供医疗安全结论或完整无障碍保证；
- 不承诺所有临时闭馆、节假日政策都能自动查到；
- 不因为“技术栈丰富”新增更多 Agent、GraphRAG、Kubernetes 或 MQ；
- 不让 LLM Judge 代替人工事实标注；
- 不把推荐、拖拽或模板数量当成产品效果；
- 不在未完成真人校准前宣称生产质量或真实用户效果。

### 3.3 每次开发必须遵守的系统不变量

1. `UNKNOWN` 永远不能自动转成 `SATISFIED`；
2. 用户锁定项、固定预约和已确认硬约束不得被静默删除；
3. 行程或成员约束发生变化后，旧报告必须立即失效；
4. 修复方案必须在应用前预览，应用后必须完整重新验证；
5. POI 低置信度匹配不得自动接受；
6. 浏览器 localStorage、Yjs 或 LLM 输出都不能单独成为服务端事实源；
7. 每条高风险结论必须能回读 reason code、输入值、证据和观测时间；
8. 拖动一个地点不得触发完整 Planner 和新的 LLM 调用；
9. 批量规划、增量编辑和导入审计必须共享同一个行程契约；
10. 同一规则只能有一个权威实现，不能长期维护 Critic 与 Verifier 两套结论。

### 3.4 当前实现基线

下表用于区分“可以复用的代码资产”和“本方案要求新增的产品能力”。后续状态报告不得把右列目标写成已完成。

| 能力 | 当前可复用资产 | 与目标状态的差距 |
|---|---|---|
| 任务约束 | `TripTaskSpec`、显式/共识/Memory/推断来源分层 | 仍缺成员归属、确认、放弃权限和成员 revision |
| 三态验证 | `ItineraryVerifier`、`ConstraintCheck`、规则注册表 | 仍缺统一 severity、字段级 EvidenceFact 和独立审计生命周期 |
| Planner | Clusterer、Distance、Sequencer、Scheduler、酒店挂载和天气 | 仍以批量生成优先，尚不能支持拖拽后的稳定增量计算 |
| Critic | `critic_v2` 的结构和营业规则 | 与 Verifier 形成第二规则源，必须迁移合并 |
| Repair | 有限轮次、锁定保护和部分定向操作 | 大量场景仍以删除为主，缺少 A/B、edit cost 和完整 postcheck 契约 |
| 版本失效 | `planning_input_hash`、前端 stale 提示 | 哈希未覆盖完整修改链，报告过期仍过度依赖浏览器状态 |
| 协同 | Yjs 房间、地点、聊天、投票和备注 | 尚未同步行程顺序、锁定和成员硬约束 |
| 候选和路线 | 高德搜索、路线、附近证据、backup pool | 缺少基于当前插入位置的统一候选排序和增量 route delta |
| 模板 | Scheduler 的到达日/完整日/返程日时间模板 | 尚无可运营的 CityRouteTemplate、来源、版本和失效机制 |
| 外部行程导入 | 无稳定产品入口 | 需要新增文本解析、source span、实体消歧和用户确认 |
| 报告持久化 | 数据库已有 verification report 表结构 | 应用层尚未形成完整保存、读取、历史和 supersede 流程 |

当前相关验证、约束和安全合同测试可以证明基础代码存在，但不能证明双入口产品、真实文本解析、拖拽体验、模板质量、Auditor 准确率或真实用户价值。

## 4. 产品闭环

### 4.1 入口 A：导入已有行程

```text
粘贴第三方 AI 或手工行程
→ 解析日期、地点、时间、固定承诺和成员信息
→ 展示结构化草稿及原文对应位置
→ POI 实体对齐
→ 用户确认歧义地点
→ 获取证据快照
→ 运行完整审计
→ 展示必须修改 / 建议调整 / 待确认
→ 生成最小修复 A/B
→ 用户预览并应用
→ 创建新 revision
→ 完整重新验证
→ 锁定确认版本
```

### 4.2 入口 B：从城市路线骨架开始

```text
选择城市、天数、人数、预算和人群
→ 选择城市路线骨架
→ 加载片区、时间槽、锚点和替代组
→ 用户选择第一个核心地点
→ 推荐顺路的下一站、餐馆和住宿片区
→ 拖入或通过按钮加入时间轴
→ 只重算受影响路线段和当天规则
→ 显示额外通勤、营业、节奏和成员影响
→ 用户调整、锁定或替换
→ 运行完整审计
→ 锁定确认版本
```

### 4.3 两个入口汇合的位置

```text
ImportedItinerary ─┐
                   ├→ EditableItineraryRevision
RouteTemplate ─────┘          ↓
                       EvidenceSnapshot
                              ↓
                         AuditReport
                              ↓
                         RepairOption
                              ↓
                     ConfirmedItinerary
```

导入解析器和模板系统只负责产生标准行程草稿，不能各自维护一套验证、修复或版本逻辑。

## 5. 目标架构

```text
┌──────────────────────────────── Frontend ────────────────────────────────┐
│ DualEntryHome                                                            │
│   ├── ImportFlow                                                         │
│   └── TemplateStartFlow                                                  │
│                                                                          │
│ ItineraryWorkspace                                                       │
│   ├── MapPane                                                            │
│   ├── TimelineBoard                                                      │
│   ├── RecommendationRail                                                 │
│   ├── AuditDrawer                                                        │
│   ├── RepairCompare                                                      │
│   └── MemberConstraintPanel                                              │
└──────────────────────────────────┬───────────────────────────────────────┘
                                   │ HTTP + SSE + Yjs
┌──────────────────────────────────▼───────────────────────────────────────┐
│ Application Services                                                     │
│   ImportService          TemplateService        ItineraryEditService     │
│   EntityResolution       IncrementalAudit       FullAuditService         │
│   EvidenceSnapshot       RepairService          ConfirmationService      │
└──────────────┬───────────────────┬────────────────────┬──────────────────┘
               │                   │                    │
┌──────────────▼────────┐ ┌────────▼─────────┐ ┌────────▼─────────────────┐
│ Itinerary Domain      │ │ Audit Domain     │ │ Candidate Providers      │
│ revisions / commands  │ │ rules / findings │ │ Amap / RAG / templates   │
│ locks / member rules  │ │ evidence / risk  │ │ existing Agent / weather │
└──────────────┬────────┘ └────────┬─────────┘ └────────┬─────────────────┘
               │                   │                    │
┌──────────────▼───────────────────▼────────────────────▼─────────────────┐
│ PostgreSQL + Redis                                                     │
│ revisions / reports / evidence snapshots / templates / route cache     │
└─────────────────────────────────────────────────────────────────────────┘
```

### 5.1 LLM 的职责边界

LLM 可以：

- 从文本提出结构化解析草稿；
- 识别可能的软偏好；
- 把确定性结果解释成人类可读文案；
- 为候选地点生成不涉及事实承诺的摘要。

LLM 不可以：

- 判断一个 POI 是否真实存在；
- 把低置信度候选自动选为正确地点；
- 判断动态营业、预约或政策事实；
- 绕过硬约束；
- 决定 `SATISFIED / VIOLATED / UNKNOWN`；
- 直接覆盖行程版本；
- 在每次拖拽时被调用。

### 5.2 现有 Planner 的新定位

现有 Planner 不删除，但从唯一主流程降为三个用途：

1. 从模板或用户候选生成第一版批量日程；
2. 当用户明确要求“自动排一下”时提供初始方案；
3. 为 RepairService 提供替代候选和局部排序能力。

Planner 的输出仍必须进入统一 Audit Engine，不能因为是内部生成就跳过验证。

## 6. 核心数据契约

### 6.1 `TripWorkspace`

```text
workspace_id
room_id
city
trip_date_range
current_itinerary_revision
current_task_spec_revision
current_member_constraint_revision
current_report_id
status: DRAFT / AUDITING / NEEDS_CONFIRMATION / CONFIRMED
created_by
created_at / updated_at
```

### 6.2 `ItineraryRevision`

```text
itinerary_id
revision
parent_revision
source_type: IMPORT / TEMPLATE / MANUAL / REPAIR / PLANNER
days[]
locked_items[]
change_summary
created_by
created_at
canonical_hash
```

每次 `ADD / MOVE / REORDER / REPLACE / REMOVE / LOCK / UNLOCK / APPLY_REPAIR` 都必须创建新 revision。行程修改不得就地覆盖旧版本。

### 6.3 `ItineraryStop`

```text
stop_id
day_index
start_time / end_time
raw_name
place_id?
resolution_status: RESOLVED / AMBIGUOUS / UNRESOLVED
resolution_confidence
source_span?
fixed_commitment
locked
category
notes
```

### 6.4 `TravelerProfile`

```text
member_id
display_name
age_group
child_age?
child_height_cm?
walking_limit_minutes?
requires_nap
wheelchair_or_stroller
dietary_restrictions[]
medication_times[]
latest_return_time?
confirmed_at?
```

### 6.5 `MemberConstraint`

```text
constraint_id
owner_member_id
type
operator
value
hardness: HARD / SOFT
priority
source: MEMBER_EXPLICIT / ORGANIZER / ROOM_CONSENSUS / MEMORY / INFERRED
confirmation_status
waivable_by
revision
```

Memory 和 inferred 约束只能是软偏好，未经确认不能升级为成员硬约束。

### 6.6 `EvidenceFact`

```text
fact_id
subject_type / subject_id
fact_type
value
provider
source_url?
observed_at
valid_from?
valid_until?
response_hash
confidence
freshness_status: FRESH / STALE / UNKNOWN
```

### 6.7 `AuditFinding`

```text
finding_id
rule_id / rule_version
status: SATISFIED / VIOLATED / UNKNOWN
severity: BLOCKER / HIGH / MEDIUM / LOW
reason_code
message
affected_days[]
affected_stops[]
affected_members[]
evidence_fact_ids[]
repairable
confirmation_action?
```

验证状态和风险等级必须分开。`UNKNOWN` 可能是 `HIGH`，例如无法确认儿童或轮椅能否进入。

### 6.8 `AuditReport`

```text
report_id
workspace_id
itinerary_revision
task_spec_revision
member_constraint_revision
evidence_snapshot_id
report_input_hash
overall_status
findings[]
created_at
superseded_by?
```

### 6.9 `RepairOption`

```text
repair_id
source_report_id
operations[]
targeted_findings[]
edit_cost
route_cost_delta
new_unknown_count
tradeoffs[]
postcheck_report_id
status: PREVIEW / APPLIED / REJECTED / EXPIRED
```

### 6.10 `CityRouteTemplate`

```text
template_id
city
name
template_version
suitable_days
suitable_groups[]
budget_level
intensity
route_zones[]
anchor_slots[]
alternative_groups[]
hotel_area_rules[]
source_refs[]
status: DRAFT / REVIEWED / RETIRED
last_verified_at
```

模板保存路线骨架、区域、品类和时间槽，不永久写死所有餐馆和商家。稳定地标可以作为 anchor，动态商家必须运行时重新解析和取证。

## 7. 城市路线骨架设计

### 7.1 模板不是标准答案

城市存在高频住宿片区、核心景点和常见天数组合，但系统不得推断所有游客都相同。模板只负责提供低风险起点，用户可以替换、删除、换日和锁定。

### 7.2 首期模板数量

每个城市只建设 5 条骨架，共 15 条：

1. 第一次到访经典路线；
2. 历史文化路线；
3. 亲子/室内路线；
4. 城市漫步路线；
5. 低体力或雨天替代路线。

模板可以跨 2～5 天组合，不为每个天数重复复制完整 POI 清单。

### 7.3 `RouteZone` 与 `AnchorSlot`

```text
RouteZone
  zone_id
  city / district
  center
  preferred_transport
  nearby_zone_ids
  incompatible_same_day_zone_ids

AnchorSlot
  day_offset
  time_window
  zone_id
  slot_type: ATTRACTION / FOOD / HOTEL / BREAK / TRANSIT
  category_constraints
  anchor_place_ids[]
  optional
  dwell_minutes
```

### 7.4 下一站推荐不是直线距离排序

候选插入成本为：

```text
delta_route_minutes =
  route(previous, candidate)
  + route(candidate, next)
  - route(previous, next)
```

候选先通过硬门禁，再参与排序：

```text
hard_constraint_gate
→ opening_and_time_fit
→ insertion_route_cost
→ member_suitability
→ evidence_freshness
→ category_diversity
→ budget_fit
→ soft_preference_score
```

展示分级而不是隐藏远处地点：

| 结果 | 初始解释阈值 | UI 行为 |
|---|---:|---|
| 顺路 | 额外通勤 ≤15 分钟 | 主推荐 |
| 可接受 | 额外通勤 15～30 分钟 | 次级推荐并展示代价 |
| 建议另一天 | 额外通勤 >30 分钟 | 保留可见，推荐换日 |
| 当前不可安排 | 营业、时间、预约或硬约束冲突 | 禁止直接应用，说明原因 |

阈值是首期可配置策略，不作为永久业务真理；必须通过真实任务校准。

### 7.5 酒店单独评分

酒店不按“离第一个景点最近”推荐。评分至少包含：

```text
所有天首站通勤
+ 所有天末站返回通勤
+ 换乘和步行负担
+ 预算和房间数
+ 连续住宿要求
+ 成员限制
+ 证据完整度
```

地点不足时先推荐住宿片区；用户选择足够地点后再计算具体酒店。酒店和固定住宿默认进入 locked items。

## 8. Audit Engine

### 8.1 规则分层

#### L0 输入完整性

- 城市、日期、天数是否明确；
- 时间和每天分组是否可解析；
- 固定预约和返程是否有明确时间；
- 地点是否存在歧义；
- 成员信息是否足以运行对应规则。

#### L1 地点和事实

- POI 是否存在；
- POI 是否属于目标城市和区县；
- POI 类型是否合理；
- 营业时间、闭馆日和最晚入场；
- 预约要求；
- 数据是否缺失或过期。

#### L2 时空可行性

- 时间重叠；
- 游览时长是否足够；
- 相邻地点通勤时间；
- 跨区折返；
- 固定预约和返程冲突；
- 每日酒店返回；
- 到达日和返程日边界。

#### L3 节奏和成员

- 连续活动时长；
- 用餐、休息和午休；
- 步行距离和换乘次数代理指标；
- 儿童年龄/身高限制；
- 老人或低体力约束；
- 饮食禁忌；
- 最晚返回和固定服药时间。

#### L4 天气和动态信息

- 雨、暴雨、高温、低温和大风；
- 户外、徒步、山地、游船和室内活动类型；
- 天气是否进入可靠预报窗口；
- 尚未进入窗口时输出 `UNKNOWN`，不提前给确定结论。

#### L5 群体冲突

- 谁的硬约束受到影响；
- 哪些必去项互相冲突；
- 哪个方案要求谁妥协；
- 是否能通过分组或换日解决；
- 是否还有成员未确认。

### 8.2 统一 Critic 与 Verifier

目标状态只有一个 `AuditEngine` 和一个规则注册表：

```text
AuditEngine
  ├── RuleRegistry
  ├── EvidenceResolver
  ├── SeverityPolicy
  ├── IncrementalDependencyIndex
  └── ReportBuilder
```

迁移期间：

- `app/constraints/rules` 继续作为第一批权威规则；
- `ItineraryVerifier` 变成 `AuditEngine` 的兼容适配器；
- `critic_v2` 中仍有价值的规则迁入注册表；
- 同一规则迁移完成后删除 Critic 对应实现；
- PlannerGraph 最终不再单独返回 `critic_violations`。

### 8.3 完整审计与增量审计

完整审计触发条件：

- 创建审计；
- 应用修复；
- 成员硬约束变化；
- 临行刷新；
- 用户确认最终版本。

增量审计触发条件：

- 同一天内移动或排序一个地点；
- 添加或删除一个地点；
- 修改一个时间槽；
- 锁定或解锁一个地点。

规则声明依赖：

```text
DAY_ORDER
TIME_WINDOW
ROUTE_EDGE
HOTEL
WEATHER
MEMBER_CONSTRAINT
EVIDENCE_FRESHNESS
```

一次编辑只重算受影响天、相邻路线段和依赖这些字段的规则；跨日酒店评分、全局预算和成员约束另行触发全局局部计算。

## 9. Evidence Snapshot

### 9.1 为什么必须快照化

同一地点的营业、天气和路线会变化。报告必须绑定一次证据快照，才能解释某个结论在何时、基于什么信息产生。

### 9.2 数据优先级

```text
用户提供的固定预约凭证
→ 官方景区或交通来源
→ 高德 POI / 路线
→ 项目审核语料
→ 第三方内容提示
→ 无可靠来源则 UNKNOWN
```

第三方内容可以提示风险，但不能单独证明营业、票价、无障碍或预约政策。

### 9.3 新鲜度策略

不同事实使用不同 TTL：

- 路线时间：请求级或短 TTL；
- 天气：只在可靠预报窗口内判定；
- 常规营业时间：中等 TTL；
- 临时闭馆和预约：需要更短 TTL 或官方确认；
- 静态坐标和行政区：较长 TTL。

具体 TTL 必须配置化并保留来源，不在规则代码中散落硬编码。

## 10. Repair Service

### 10.1 允许的原子操作

```text
SHIFT_TIME
MOVE_WITHIN_DAY
MOVE_TO_DAY
REPLACE_STOP
INSERT_BREAK
INSERT_MEAL
CHANGE_HOTEL_AREA
REMOVE_STOP
SPLIT_GROUP
```

`REMOVE_STOP` 是最后选择，不是默认修复。

### 10.2 修复的词典序目标

1. 不破坏固定预约、酒店、返程和用户锁定项；
2. 消除 BLOCKER 和硬约束违规；
3. 不引入新的硬约束违规；
4. 不把已验证事实无故退化为新的 `UNKNOWN`；
5. 最小化地点删除、换日和时间偏移；
6. 保留高票、必去和高优先级地点；
7. 最小化额外通勤和成员负担；
8. 返回 2 个具有明显取舍差异的可行方案。

### 10.3 第一版实现方式

不直接引入复杂通用求解器，先使用有界局部搜索：

1. 根据 finding 生成有限原子操作；
2. 从附近候选、模板 alternative group、backup pool 和推荐 Agent 获取替代项；
3. 对每个候选应用临时 patch；
4. 运行完整 AuditEngine；
5. 丢弃新增硬违规或破坏锁定项的候选；
6. 计算 edit cost、route delta、new unknown 和软偏好得分；
7. 去除高度相似方案；
8. 返回前两个方案及取舍说明。

### 10.4 修复的非回归要求

每个 `RepairOption` 必须保存 postcheck report。没有 postcheck 的方案不得显示为“可行修复”。

## 11. 拖拽与移动端交互

### 11.1 桌面端布局

```text
┌────────────────┬──────────────────┬────────────────────┐
│ 地图           │ 每日时间轴       │ 推荐、证据与风险   │
│ 地点和片区     │ Day 1 / Day 2    │ 顺路候选           │
│ 当前路线       │ 地点、餐饮、休息 │ 另一天候选         │
│ 酒店位置       │ 锁定状态         │ 当前 findings       │
└────────────────┴──────────────────┴────────────────────┘
```

拖动时先进行乐观 UI 更新，然后异步返回：

```text
预计变化
+ 额外通勤 22 分钟
+ 当日结束时间变为 19:40
- 老人连续活动超过约束
? 新地点营业时间待确认
```

### 11.2 移动端等价操作

移动端必须支持：

- 加入某一天；
- 移动到另一天；
- 上移/下移；
- 替换；
- 锁定/解锁；
- 删除；
- 撤销。

不能让核心操作只依赖精确拖拽。

### 11.3 性能边界

- 拖拽 UI 反馈应立即发生；
- 本地结构检查目标在 100ms 量级；
- 缓存命中的增量审计目标 P95 <500ms；
- 需要真实路线取证时异步更新，目标 P95 <3s；
- 完整审计通过 SSE 分阶段反馈；
- 拖拽过程不调用 LLM。

## 12. 协同和版本一致性

### 12.1 Yjs 新共享结构

```text
doc.getMap('room')
doc.getMap('places')
doc.getArray('chat')
doc.getMap('itinerary')
  currentRevision
  currentReportId
  days
doc.getMap('daySlots')
  day-1 -> Y.Array<stop_id>
  day-2 -> Y.Array<stop_id>
doc.getMap('stopLocks')
doc.getMap('memberConstraints')
```

Yjs 负责实时协同意图和顺序，不负责最终事实持久化。服务端 revision、AuditReport 和数据库事务仍是权威记录。

### 12.2 编辑命令

所有修改统一为：

```text
ItineraryEditCommand
  command_id
  workspace_id
  base_revision
  actor_user_id
  operation
  payload
  client_timestamp
```

服务端返回：

```text
ItineraryPatchResult
  accepted
  new_revision
  changed_days
  route_delta
  incremental_findings
  report_stale
  conflict?
```

`base_revision` 不一致时返回 409，由前端合并或提示冲突，不允许后到请求静默覆盖先到修改。

### 12.3 版本哈希

```text
report_input_hash = SHA256(
  canonical_itinerary_revision
  + task_spec_revision
  + member_constraint_revision
  + evidence_snapshot_id
)
```

哈希必须覆盖实际天、时段、顺序、锁定状态和成员约束，不能只依赖一个未必递增的 `itinerary_version`。

## 13. API 设计

### 13.1 工作台和行程版本

```http
POST /api/trip-workspaces
GET  /api/trip-workspaces/{workspace_id}
GET  /api/trip-workspaces/{workspace_id}/revisions
POST /api/trip-workspaces/{workspace_id}/edits
POST /api/trip-workspaces/{workspace_id}/undo
POST /api/trip-workspaces/{workspace_id}/confirm
```

编辑接口必须携带 `base_revision` 和 `Idempotency-Key`。

### 13.2 导入入口

```http
POST  /api/trip-workspaces/{workspace_id}/imports/parse
PATCH /api/trip-workspaces/{workspace_id}/imports/{import_id}/resolutions
POST  /api/trip-workspaces/{workspace_id}/imports/{import_id}/apply
```

`parse` 只生成草稿，歧义未确认时不得直接创建已解析行程。

### 13.3 模板入口

```http
GET  /api/cities/{city}/route-templates
GET  /api/route-templates/{template_id}
POST /api/trip-workspaces/{workspace_id}/templates/{template_id}/apply
GET  /api/trip-workspaces/{workspace_id}/candidates?day=&before=&after=
GET  /api/trip-workspaces/{workspace_id}/hotel-areas
```

### 13.4 审计和修复

```http
POST /api/trip-workspaces/{workspace_id}/audits
GET  /api/audits/{audit_id}
GET  /api/audits/{audit_id}/events
POST /api/audits/{audit_id}/refresh
POST /api/audits/{audit_id}/repairs
POST /api/audits/{audit_id}/repairs/{repair_id}/apply
```

`refresh` 创建新 EvidenceSnapshot 和新 AuditReport，不覆盖旧报告。

### 13.5 成员约束和分享

```http
GET  /api/trip-workspaces/{workspace_id}/members
PUT  /api/trip-workspaces/{workspace_id}/members/{member_id}/constraints
POST /api/trip-workspaces/{workspace_id}/share-links
POST /api/share/{token}/responses
```

分享链接默认只读，轻量表态或填写约束使用单独 scope，不授予修改整个房间的权限。

## 14. 数据库修改

新增迁移建议为：

```text
009_trip_workspace_core.sql
010_audit_evidence_repair.sql
011_route_templates.sql
```

### 14.1 `009_trip_workspace_core.sql`

- `trip_workspaces`
- `itinerary_revisions`
- `itinerary_edit_commands`
- `itinerary_imports`
- `itinerary_stop_resolutions`
- `traveler_profiles`
- `member_constraints`

### 14.2 `010_audit_evidence_repair.sql`

- `evidence_snapshots`
- `evidence_facts`
- 扩展现有 `verification_reports`，绑定 workspace 和 revision；
- `audit_findings`
- `repair_options`
- `repair_operations`

### 14.3 `011_route_templates.sql`

- `city_route_templates`
- `city_route_template_versions`
- `route_template_sources`

模板版本发布后不可就地修改；更新时创建新 version，已有行程继续保留当时使用的版本引用。

## 15. 代码修改方向

### 15.1 后端新增

```text
backend/app/itinerary/
├── models.py
├── revision_service.py
├── edit_commands.py
├── import_parser.py
├── entity_resolver.py
├── incremental_service.py
├── template_service.py
└── repositories.py

backend/app/audit/
├── models.py
├── engine.py
├── registry.py
├── dependency_index.py
├── evidence_service.py
├── severity.py
├── report_service.py
├── repair_service.py
└── rules/
    ├── place_identity.py
    ├── opening_schedule.py
    ├── route_feasibility.py
    ├── density_and_rest.py
    ├── traveler_suitability.py
    ├── weather_conflict.py
    ├── booking_requirement.py
    └── collaboration.py

backend/app/api/
├── trip_workspaces.py
├── itinerary_imports.py
├── audits.py
├── repairs.py
└── route_templates.py
```

### 15.2 后端修改

| 当前文件 | 修改方向 |
|---|---|
| `schemas/task_spec.py` | 增加成员约束引用和受控 constraint type；保留来源分层 |
| `schemas/itinerary.py` | 与 `ItineraryRevision` 兼容，明确 revision 和 parent revision |
| `schemas/verification.py` | 增加 severity、规则版本、成员影响和结构化证据引用 |
| `services/planning_hash.py` | 升级为覆盖实际行程、成员约束和证据快照的 canonical hash |
| `constraints/verifier.py` | 逐步变为 AuditEngine 适配器 |
| `constraints/rules/*` | 保留为权威规则起点，补齐依赖声明和结构化证据 |
| `agents/planner/graph.py` | 改为 schedule → audit → bounded repair → audit → tips |
| `agents/planner/nodes/critic_v2.py` | 规则迁移后删除，不再形成第二事实源 |
| `agents/planner/repair_controller.py` | 保留兼容入口，逐步委托给 RepairService |
| `agents/nodes/task_parser.py` | 只解析 TripTaskSpec；外部行程解析移入 import_parser |
| `api/optimize.py` | 作为批量初始规划接口，输出统一 workspace revision |
| `tools/runtime.py` | EvidenceService 复用工具回执、deadline 和错误分类 |

### 15.3 前端新增

```text
frontend/src/app/plan/page.tsx
frontend/src/app/import/page.tsx
frontend/src/app/workspace/[workspaceId]/page.tsx

frontend/src/components/workspace/
├── WorkspaceShell.tsx
├── TimelineBoard.tsx
├── TimelineDay.tsx
├── StopCard.tsx
├── RecommendationRail.tsx
├── AuditDrawer.tsx
├── RepairCompare.tsx
├── TemplatePicker.tsx
├── ImportResolutionPanel.tsx
└── MemberConstraintPanel.tsx

frontend/src/hooks/
├── useTripWorkspace.ts
├── useItineraryEdits.ts
├── useIncrementalAudit.ts
└── useYjsItinerary.ts
```

### 15.4 前端修改

| 当前文件 | 修改方向 |
|---|---|
| `app/page.tsx` | 改为“导入已有行程 / 从城市骨架开始”双入口 |
| `components/itinerary/ConstraintPanel.tsx` | 升级为必须修改、建议调整、待确认，并展示来源、时间和影响成员 |
| `hooks/useOptimize.ts` | 不再用 localStorage 作为报告真相；接入 workspace revision |
| `hooks/useYjsRoom.ts` | 增加 itinerary order、locks、member constraints 的共享结构 |
| `stores/roomStore.ts` | 拆出 workspace 本地 UI 状态，服务端 revision 为事实源 |
| `components/places/PlaceList.tsx` | 从只读列表演化为候选区，不直接承担完整时间轴 |

拖拽库只在 TimelineBoard 开发阶段引入。库的选择不能先于服务端 edit command 和增量重算契约。

## 16. 分阶段实施方案

### R0：冻结范围并修复版本基础（3～5 天）

#### 开发内容

- 写入本方案并作为开发基线；
- 记录现有功能保留、兼容和淘汰清单；
- 修复所有行程编辑未递增版本的问题；
- canonical hash 覆盖完整行程内容；
- 接通 verification report 服务端持久化；
- 统一报告过期判断，移除 localStorage 的事实源职责；
- 为后续迁移保留兼容 API。

#### 完成门禁

- 任意 ADD/MOVE/REMOVE/SWAP 都生成新 revision；
- 旧 revision 和旧报告可回读；
- 行程或成员约束变化后旧报告必定 stale；
- 当前验证、安全和 evidence 合同测试继续通过；
- 没有新增城市和功能范围。

### R1：独立 Audit Engine（5～7 天）

#### 开发内容

- 建立 `audit/models.py`、`engine.py`、`registry.py`；
- 把现有规则接入统一引擎；
- 增加 severity、rule_version 和 EvidenceFact；
- 将 `critic_v2` 的独有规则逐条迁移；
- Planner 调整为 audit/repair/audit 后再生成 Tips；
- 新增独立审计 API，不依赖 Planner。

#### 完成门禁

- 同一 Itinerary 可以不经过 Planner 独立审计；
- 同一规则只有一份权威实现；
- `UNKNOWN` 误判通过为 0；
- 高风险 finding 可以回读 evidence、observed_at 和 affected members；
- Repair 后的 Tips 与最终行程一致。

### R2：纯文本导入与实体对齐（7～10 天）

#### 开发内容

- 支持典型豆包、DeepSeek、ChatGPT 和手写行程文本；
- 解析天、时间、地点、固定承诺和成员摘要；
- 保留原文 span；
- 高德 POI 候选和联合匹配；
- 低置信度候选确认 UI；
- 导入结果创建标准 ItineraryRevision。

#### 完成门禁

- 至少 30 份真实文本进入盲测；
- 高置信度自动匹配 precision ≥95%，目标 ≥98%；
- 歧义或未匹配地点静默通过为 0；
- 固定预约和返程字段 recall ≥95%；
- Parser 失败时返回可编辑草稿，不生成虚假 POI。

### R3：城市路线骨架与候选服务（7～10 天）

#### 开发内容

- 建立 3 城 × 5 条 REVIEWED 路线骨架；
- 建立 RouteZone、AnchorSlot、alternative group；
- 记录模板来源、版本和最后核验时间；
- 实现插入路线成本；
- 实现顺路、可接受、建议另一天、不可安排分级；
- 实现住宿片区和酒店全行程评分。

#### 完成门禁

- 15 条模板有来源、有版本、可回读；
- 模板不永久写死动态餐馆；
- 远处地点不会被静默隐藏；
- 候选排序能解释路线代价和硬门禁；
- 酒店评分覆盖所有天首末站，不按单一景点推荐。

### R4：拖拽工作台与增量审计（7～10 天）

#### 开发内容

- TimelineBoard 和地图联动；
- ADD/MOVE/REORDER/REPLACE/LOCK/UNDO；
- 移动端等价按钮；
- Yjs 行程顺序和锁定状态；
- IncrementalDependencyIndex；
- 路线边缓存和受影响天重算；
- UI 展示增量通勤、结束时间和 findings。

#### 完成门禁

- 拖动一个地点不执行完整 Planner；
- 拖动过程 LLM 调用数为 0；
- 两人同时编辑不会静默覆盖；
- 缓存命中增量审计 P95 <500ms；
- 服务端拒绝修改时前端能回滚或合并；
- 桌面拖拽和移动按钮产生相同 edit command。

### R5：最小修复、成员约束与确认（7～10 天）

#### 开发内容

- RepairOption 和有界局部搜索；
- 模板替代组、backup pool、推荐 Agent 作为候选源；
- 修复 A/B 和差异视图；
- 成员约束卡；
- 带娃、带父母、低步行模板；
- 分享链接和轻量确认；
- 固定预约、酒店和返程锁定策略。

#### 完成门禁

- 锁定项修复非回归率 100%；
- 修复后新增硬违规数为 0；
- 所有 RepairOption 均有 postcheck report；
- 投票不能覆盖任何成员硬约束；
- 两个方案具有可解释取舍，不是相同结果换文案；
- 用户拒绝方案时记录结构化原因。

### R6：临行复检、固定评测与公网 Beta（7～14 天）

#### 开发内容

- 24～48 小时手动触发复检；
- 新旧 EvidenceSnapshot 和 AuditReport 差异；
- 三城审计数据集；
- 公网双入口 E2E；
- 15～20 名组织者、至少 30 份真实行程；
- 真实小额付费或明确交易实验；
- 发布 evidence 和面试证据包。

#### 完成门禁

- 动态事实变化能说明“什么变了、为什么变”；
- 公网导入、模板、编辑、审计、修复和分享链路通过；
- 技术指标与产品指标分别发布；
- 真人字段由真人填写，不能由 Agent 或 Judge 代填；
- 未达门禁时保留 Beta/候选状态，不宣称生产可用。

## 17. 评测体系

### 17.1 数据集组成

新的 Auditor 数据集与现有推荐 150 条评测完全分开。

目标 150 份由以下部分组成：

- 60 份真实 AI 原始行程；
- 60 份基于真实行程的受控变异；
- 30 份歧义、UNKNOWN、冲突和故障边界案例。

按原始 source document 分割 train/dev/blind，不能让同一行程的变体跨集合泄漏。

### 17.2 解析和实体指标

- 日期、时间、地点和固定承诺字段 F1；
- 高置信度 POI 匹配 precision；
- 歧义召回率；
- 静默错配数；
- 未解析字段人工确认率。

### 17.3 审计指标

| 指标 | 内部门槛 |
|---|---:|
| BLOCKER/HIGH precision | ≥90% |
| BLOCKER/HIGH recall | ≥85% |
| UNKNOWN 被误判为 SATISFIED | 0 |
| 高风险 finding 证据可回读率 | 100% |
| 报告 3 分钟内完成率 | ≥80% |

真实原始行程和人工植入错误必须分别报告 precision/recall，不能用容易检测的植入错误掩盖真实误报。

### 17.4 修复指标

- 锁定项破坏率 = 0；
- 修复后新增硬违规数 = 0；
- 平均 edit distance；
- 平均 route delta；
- 新增 UNKNOWN 数量；
- 修复方案采纳率目标 ≥40%；
- 拒绝原因分布。

### 17.5 产品指标

- 至少一个 finding 被认可的审计比例；
- 高风险 finding 驳回率；
- 从开始到首个确认版本的中位数和 P90；
- 报告分享率；
- 临行复检率；
- 下一次旅行复用率；
- 真实小额支付转化。

“发现问题的行程比例”不能单独作为成功指标，否则系统会被激励制造警告。

## 18. 测试和发布门禁

### 18.1 单元测试

- 每条 AuditRule 的 SATISFIED/VIOLATED/UNKNOWN；
- 数据过期和来源缺失；
- entity resolution 阈值和歧义；
- revision/hash 稳定性；
- insertion cost；
- repair edit cost 和锁定保护；
- template version 和替代组。

### 18.2 性质和变形测试

- 候选输入顺序变化不影响 canonical hash；
- 同一地点轻微别名不产生两个实体；
- 平移无关日期不改变静态地点结论；
- 增加一个远处地点只影响相关天和全局酒店评分；
- 修复未涉及的天保持字节级等价；
- 未变化的 evidence snapshot 得到相同报告。

### 18.3 集成测试

- 导入 → 消歧 → 审计；
- 模板 → 添加地点 → 增量审计；
- Repair preview → apply → postcheck；
- 双用户并发编辑；
- 服务重启后 revision、报告和 Yjs 顺序恢复；
- Amap/天气超时、429、空结果和熔断；
- 分享链接权限和跨房间 IDOR。

### 18.4 公网 E2E

必须覆盖：

1. 导入一份真实 AI 行程；
2. 确认一个歧义 POI；
3. 查看一个 VIOLATED 和一个 UNKNOWN；
4. 预览并应用修复；
5. 从模板创建另一份行程；
6. 拖动地点并看到增量影响；
7. 邀请成员填写约束；
8. 修改后旧报告失效；
9. 刷新证据并查看差异；
10. 服务重启后状态仍可回读。

## 19. 安全、隐私和成本

### 19.1 输入安全

- 导入文本一律视为不可信数据；
- 行程中的 Prompt Injection 不得改变系统规则或工具权限；
- 文本长度、地点数量和工具调用数有明确上限；
- 文件上传在后续阶段实施时单独做 MIME、大小、恶意文件和隐私检查。

### 19.2 权限

- 工作台读写、报告查看、修复应用和分享表态使用不同 scope；
- 服务端身份为唯一授权来源；
- room_id、workspace_id、audit_id 和 repair_id 都执行资源归属检查；
- 分享 token 可撤销、可过期、默认最小权限。

### 19.3 隐私

- 儿童年龄、饮食禁忌、轮椅和服药时间属于敏感旅行信息；
- 只收集运行规则所需的最小字段；
- 用户可以删除成员资料和历史导入；
- 日志不得记录完整行程原文、手机号、token 或敏感成员信息。

### 19.4 成本

- 文本导入最多一次结构化 LLM 解析，失败走可编辑降级；
- 拖拽和规则验证不调用 LLM；
- 路线边和静态 POI 使用带来源的缓存；
- 完整审计设定 provider call budget；
- Repair 候选数量和搜索深度有上限；
- API 成本、延迟和降级状态写入 ToolReceipt 和 trace。

## 20. PR 和开发管理约束

每个 PR 必须说明：

1. 对应本文哪个阶段和哪项用户任务；
2. 是否改变数据契约或 revision；
3. 新增或修改了哪些规则；
4. 失败时如何降级；
5. 新增了什么自动测试；
6. 产生了什么可回读证据；
7. 是否扩大城市、用户、数据或部署范围；
8. 是否引入新的事实来源或隐私字段。

出现以下任一情况时停止合并：

- UNKNOWN 被当作通过；
- 修改未创建新 revision；
- 修复没有 postcheck；
- 新规则没有 reason code 和证据边界；
- 低置信度 POI 被自动接受；
- 前端状态与服务端 revision 不一致且没有冲突处理；
- 同一规则在 Critic 和 AuditEngine 中重复实现；
- 新功能只增加技术名词，无法对应用户任务和指标；
- 使用局部、小样本或冻结快照冒充真实产品质量。

## 21. 阶段优先级

### P0：必须完成

- R0 版本、哈希、报告持久化；
- R1 独立 Audit Engine；
- R2 纯文本导入和实体消歧；
- R3 最小城市骨架和插入成本；
- R4 基础编辑与增量审计；
- R5 有 postcheck 的最小修复；
- R6 真实行程和真人验证。

### P1：P0 稳定后增强

- 成员分享和轻量确认；
- 更细的酒店评分；
- 临行复检差异；
- 低体力、亲子和雨天约束包；
- 更强的模板数据治理；
- 两实例和长任务恢复。

### P2：只有证据驱动时实施

- 任意截图和复杂文件；
- 自动抓取官方页面；
- 通用求解器；
- 主动后台监控和推送；
- B2B Verifier API/SDK；
- MQ、Kubernetes、GraphRAG；
- 更多城市和更多 Agent。

## 22. 最终交付效果

完成 P0 后，BreezeTravel 应能够展示两条真实完整链路。

### 链路一：已有行程

```text
导入豆包北京三日游
→ 识别并确认歧义地点
→ 发现跨区通勤、营业 UNKNOWN 和老人休息不足
→ 展示证据和受影响成员
→ 预览“颐和园换日”和“景山替换”两个方案
→ 应用一个方案
→ 完整复验无新增硬违规
→ 生成可分享确认版本
```

### 链路二：从零规划

```text
选择杭州三日低体力路线
→ 从西湖骨架开始
→ 加入断桥后推荐白堤、浙博和附近午餐
→ 将灵隐寺拖入同一天
→ 系统显示额外通勤和节奏冲突，建议移到第二天
→ 推荐适合全行程的住宿片区
→ 成员补充午休约束
→ 增量调整并最终通过完整审计
```

这两条链路共享同一套行程版本、证据、规则、修复和协同能力。项目的技术主线因此从“多 Agent 生成更多内容”转为：

```text
非结构化输入或路线骨架
→ 标准行程模型
→ POI 实体消歧
→ 增量编辑
→ 外部证据快照
→ 确定性审计
→ 有界修复搜索
→ 非回归复验
→ 多人确认
→ 版本化证据
```

## 23. 最终验收定义

只有同时满足以下条件，才能把本次转型标记为“可用 MVP”：

- 双入口都能产生相同标准行程 revision；
- 导入文本的低置信度地点不会静默通过；
- 模板有来源、版本和失效机制；
- 拖拽和移动按钮使用相同编辑命令；
- 每次修改都会可靠作废旧报告；
- AuditEngine 是唯一权威规则源；
- 高风险 finding 有来源、时间、reason code 和影响成员；
- RepairOption 不破坏锁定项且有 postcheck；
- 多人硬约束不会被票数覆盖；
- 公网完整 E2E 可重复；
- 新审计集与旧推荐评测严格分开；
- 真人校准和产品指标由真实用户产生；
- 对外声明与最新 evidence 完全一致。

在此之前，正确表述是“可验证行程工作台开发中”或“受控三城 Beta candidate”，不能表述为已完成转型、生产可用或已经验证商业价值。

## 24. 方案依据和证据边界

本方案合并了两条互补判断：

1. 已有 AI 行程用户需要“导入、实体消歧、事实核验、成员约束和最小修复”；
2. 没有完整行程的用户可以从城市高频路线骨架开始，通过地图、时间轴和附近候选逐步组合，但每次编辑仍必须进入同一验证闭环。

外部方向依据包括：

- [ChinaTravel](https://arxiv.org/abs/2412.13682)：复杂旅行约束仍是神经符号规划的困难问题；
- [Google Research：Optimizing LLM-based trip planning](https://research.google/blog/optimizing-llm-based-trip-planning/)：将 LLM 的软需求理解与开放时间、路线等确定性优化结合；
- [AlterAtlas](https://arxiv.org/abs/2607.16565)：从生成转向人物约束驱动的验证和方案比较；
- [Placez itinerary audit](https://www.placez.io/audit) 与 [TripSapien](https://www.tripsapien.com/check/chatgpt-itinerary)：证明“导入后检查”已有直接竞品，因此 BreezeTravel 必须依靠中国场景、成员约束、三态证据和非回归修复形成差异；
- [Wanderlog](https://wanderlog.com/home)：证明地图、时间轴、协同和路线编辑是成熟交互，拖拽本身不是护城河；
- [北京常规旅游线路](https://whlyj.beijing.gov.cn/ggfw/ly/201502/t20150205_479314.html)、[上海精品线路](https://www.shanghai.gov.cn/nw17239/20250430/a3efe384da7544e38e8a74c10714f73b.html)和[杭州文化旅游精品线路](https://wgly.hangzhou.gov.cn/module/download/downfile.jsp?classid=0&filename=c3ccb4250d3143a09047ee20fbe1170c.pdf)：说明可以从城市高频片区和路线骨架开始，但官方线路仍需转换为可替换、可版本化的模板，而不是固定答案。

上述资料只能证明方向合理和需求假设值得验证，不能证明 BreezeTravel 已经建立商业壁垒。市场结论必须继续由真实组织者访谈、真实行程、行为数据和付费实验更新。

本地配套文档：

- `docs/BreezeTravel_产品方向与市场痛点调研报告_2026-08-20.md`：产品方向和市场假设；
- `docs/BreezeTravel_AI应用与AI后端深度优化方案.md`：可靠性、证据等级和原 R0～R6 工程路线；
- 本文：两条产品入口合并后的权威目标、架构和实施门禁。
