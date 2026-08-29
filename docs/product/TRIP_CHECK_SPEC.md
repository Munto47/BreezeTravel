# 「行程查」产品与行为规格

> 状态：`ACCEPTED_BLUEPRINT`
>
> 版本：`3.0`
>
> 实现状态：`TARGET_NOT_IMPLEMENTED`
>
> 日期：2026-08-27

## 1. 完整用户流程

```text
手机号验证码登录 / 先体验
→ 粘贴长文本（后续支持截图）
→ 自动生成逐日行程卡片与可编辑软假设
→ 高置信地点自动匹配，低置信地点局部待确认
→ 后台生成 revision 绑定的步行/公交地图
→ 用户查看、替换、删除、插入或排序卡片
→ 用户按需重新渲染地图
→ 补全住宿与用餐
→ Top-3 可行性核验
→ 预览并采纳最小修改
→ 新 ItineraryRevision
→ 地图 stale + 完整 postcheck
```

卡片结果必须先于完整核验可用。任一 Provider 失败都保留已经成功的卡片和事实，不得以空白页、内部错误或红色错误墙结束。

## 2. 输入与软假设

### 2.1 文本

支持粘贴和手工输入。内部保存原文 hash、版本、source span、模型/解析器绑定和证据编译结果；公共结果不得返回原文、offset、置信度或模型过程。

Prompt injection、网页导航文字、URL、预约说明和描述句只能成为引用、主张或待确认文本，不能成为指令、地点或权威事实。

G01初始防滥用预算冻结为：单份文本最多50,000个Unicode code point、最多14天、最多80个可执行活动、同一账号最多2个并发理解任务、主模型最多1次初始调用和1次schema修复。超过预算仍保留已解析内容并返回可编辑的 `LIMITED` 结果，不静默截断、不产生红色错误墙；阈值只能由后续Goal基于延迟、费用和真实输入分布版本化调整。

### 2.2 默认规则

- 明确城市、日结构、日期和人数优先。
- 城市缺失时采用最高概率城市作为 `WORKING_ASSUMPTION`，用户可直接修改。
- 有“第一天/第二天”等结构时保留；完全缺失时创建 Day 1～Day 3。
- 没有绝对日期时日期为 `null`，只显示 Day N；天气和日期相关闭馆保持不可判定。
- 没有人数时 `party_size=2`，来源为 `DEFAULT`，不成为 HARD。
- 北京、上海、杭州进入深度链；其他城市返回基础整理和明确能力说明。

这些假设不能成为进入卡片页的阻断表单。

### 2.3 截图

V0.4 开放：

- PNG/JPEG/WebP；
- 单次最多 6 张；
- 每张不超过 10MB；
- 图片通过短期multipart上传批次进入，理解任务只消费绑定当前用户的不透明`batch_ref`；禁止把Base64图片塞进现有JSON；
- PaddleOCR 是基线，Qwen-VL 只在冻结消融胜出后晋级；
- 原图在成功、失败、取消或超时终态删除；
- OCR文本、阅读顺序和bbox来源映射进入加密`SourceDocument`，继承文本source的30天上限与主动删除合同；source删除后不得保留可还原OCR文本或映射；
- 终态只长期保存不可逆hash、结构化卡片、OCR/模型版本和清理回执；清理失败为内部`PRIVACY_BLOCKED`，不得宣称任务成功；
- Qwen-VL外发前必须完成本地敏感信息遮蔽，否则保持实验状态。

## 3. 语义理解合同

`TripUnderstandingRevision` 是 workspace 前的内部权威草稿，包含：

- `SourceDocumentRef[]`；
- `DestinationHypothesis[]`；
- `WorkingAssumption[]`；
- `DayDraft[]`；
- `SourceClaim[]`；
- `ExcludedMention[]`；
- `InferenceReceipt` 与 content hash。

`ActivityMention` 至少包含：

- day、order、activity type；
- `PLANNED / OPTIONAL / REFERENCE / EXCLUDED / PASS_THROUGH`；
- 原子化 `place_text` 或 `null`；
- time hint、duration claim 和关联主张；
- 内部 source evidence 和 confidence。

只有 `PLANNED` 且 `place_text` 为原子实体时才能进入 `ExecutablePlaceMention`。例如“故宫建议至少留 4 小时”生成“故宫”地点与“至少 4 小时”主张，整句不得成为 POI；“从神武门出来直接上景山”可把神武门标记为上下文，把景山标记为下一活动。

LLM输出只是提案。服务端必须验证 schema、证据子串、语义枚举、同城约束和搜索资格。

## 4. 地点解析合同

地点搜索流程：

1. 对原子地点生成规范化查询；
2. 使用城市、名称/别名、类别、行政区和相邻地点约束搜索；
3. 确定性过滤错城、错类别、停车场/售票处等非目标结果；
4. 校准后分为：
   - `AUTO_SELECTED`；
   - `SUGGESTED`；
   - `UNRESOLVED`；
   - `PROVIDER_UNAVAILABLE`。

自动匹配以精度优先。`UNRESOLVED` 保留原名称和编辑能力，不阻断其他卡片。模型不得把举例或常识写成 canonical POI。

## 5. 用户结果合同

公共 `UserFacingTripResult` 只包含：

- `AssumptionChipView[]`；
- `TripDayView[]`；
- `PlaceCardView[]`；
- `MapReadinessView`；
- `StaySuggestionView`；
- 用户允许执行的 action。

卡片点击打开用户详情面板，仅展示：

- 名称、类别、地址、区域；
- 有可靠证据时的营业、预约和建议停留；
- 到下一站的步行/公交；
- 查看地图、替换、删除；
- 与本次行程相关的简短提示。

“到下一站”只使用与current `PlanRevisionRef`一致的路线；地图需要更新时可显示上次路线并同时展示“行程已修改，路线尚未更新”，或暂时隐藏，不得把旧路线表达为当前事实。

不得展示或高亮原文，不得返回 source span、置信度数字、模型、Provider、UID、hash、revision、receipt 或内部阶段。资源标识可以作为命令 token 存在于 payload，但不得渲染为用户文案。

## 6. 卡片编辑合同

支持：

- 插入地点；
- 删除活动；
- 修改活动文字；
- 替换已解析地点；
- 移动活动到同日或其他日期；
- 修改城市、Day 数、日期或人数软假设；
- 选择住宿。

所有语义编辑要求 `If-Match` 和 `Idempotency-Key`。materialize前只创建新 `TripUnderstandingRevision`，materialize后只创建新 `ItineraryRevision`；ETag是服务端绑定 `PlanRevisionRef(kind, aggregate_id, revision, stop_set_hash)`的不可逆不透明CAS validator，不得暴露引用字段。materialize原子写lineage并切换current plan pointer。旧地图和旧报告投影为需要更新。卡片编辑不得自动触发路线 Provider 或地图重绘。

## 7. 地图合同

地图内部任务与快照分开：

```text
MapRenderJob: QUEUED → BUILDING → READY / PARTIAL / UNAVAILABLE
MapRenderSnapshot: immutable terminal result
freshness: CURRENT | STALE (按PlanRevisionRef比较得出)
```

卡片首次稳定后，后台自动为该revision创建地图job并在G01实际计算walking/transit，不阻塞页面。后续编辑不创建job；用户点击“重新渲染地图”时才为current `PlanRevisionRef`新建或复用逻辑任务。公共状态只使用 `PREPARING / AVAILABLE / NEEDS_UPDATE / LIMITED / UNAVAILABLE`。

迟到任务只能写入其绑定revision。客户端可显示旧地图和“行程已修改，地图尚未更新”，但不能把旧路线标记为当前。除请求幂等键外，`understanding + revision kind + revision + stop_set_hash + route_config_hash`具有逻辑唯一性；连续点击使用不同key也不得重复调用Provider。

每条相邻边同时保存 walking/transit 状态、分钟数、换乘数、距离、响应 hash 和短期路线几何引用：

- 选择更短方式；
- 差值 ≤10 分钟时优先步行；
- 一种失败时使用另一种；
- 两种失败时显示“路线暂不可用”；
- 不以驾车为默认。

路线几何只按 Provider 许可进入短期缓存；数据库持久化规范化事实、hash 和必要回执。

## 8. 住宿推荐合同

没有已选酒店时返回非阻断的“住宿待选择”。

1. N日计划默认过夜日为Day 1…Day N-1；只有原文明确最后一日继续住宿时才包含Day N，最后一天默认不插入酒店。
2. 每个过夜日形成有方向的 `STAY_TO_FIRST` 与 `LAST_TO_STAY` 两条通勤边；使用第一站与最后一站作为锚点。
3. 以GCJ-02坐标经本地等距投影后的几何中位区域为初始搜索中心。
4. 按2km、4km、8km、同城逐级扩展；当前层达到12家合格候选即停止，否则继续。
5. 候选必须通过版本化 `HotelBrandRegistry`、酒店类别和同城校验。
6. 最多筛12家进入路线矩阵。
7. 对每家按上述方向计算全部过夜日步行/公交成本。
8. 排名：
   `total_best_minutes + 0.5 * max_single_leg_minutes + 8 * transfers + evidence_penalty`。
9. `StayScoringPolicyVersion`必须冻结坐标缺失、单向失败、双模式失败的惩罚值和上限；同分依次按缺失边更少、最差单程更短、canonical place ID排序。
10. 返回最多3家；用户卡片只解释区域、通勤摘要、最差单程、换乘、证据缺口和简短理由，不宣称价格、房态、星级或服务质量。
11. 用户选择后，同一家酒店成为所有过夜日的共享 `StayAnchor`，并让住宿往返边进入下一次地图任务。
12. 选择酒店按current plan kind创建新revision，地图变为 `NEEDS_UPDATE`。

证据不足或没有候选时只扩大区域或保留待选择，不得伪造酒店。

## 9. 核验与建议合同

`AuditEngine` 对足够证据判断：

- 地点存在和城市归属；
- 营业、闭馆和预约；
- 路线耗时与日容量；
- 酒店往返和用餐空档；
- 有日期时的天气与临时风险。

正式materialize固定支持 `calendar_basis=ABSOLUTE|DAY_INDEX_ONLY`。`DAY_INDEX_ONLY`只核验不依赖日历日期的地点、路线、容量、住宿和用餐；天气、临时闭馆和特定日期营业保持 `UNKNOWN`。人数软默认的来源为 `SOFT_DEFAULT`，不得伪装成用户确认或HARD证据。

热门程度、典型时长、适合时段、夜景和季节体验是建议性 `KnowledgeClaim`，不得伪装成硬事实。

用户结果最多展示 3 个 Finding，排序考虑严重程度、证据可信度、可操作性和全程影响。分组映射为：

- `MUST_ADJUST` → 必须调整；
- `SHOULD_OPTIMIZE` → 可以更好；
- `NEEDS_CONFIRMATION` → 需要确认。

内部必须保留全部未解决HARD Finding。公共页只展示前三项；同原因、同一天且同一修复动作的硬冲突可以确定性聚合。若聚合后仍超过3项，显示不展开详情的中性汇总“还有N个必须处理的问题”，不得显示“已通过”；用户解决当前项后按排序补位。

采纳 `RepairOption/EditCommand` 创建新 revision。只有完整 postcheck 后才能显示“已解决”。

## 10. 失败与颜色

- 红色：有可靠证据的硬冲突。
- 橙色：可优化但仍可执行。
- 蓝色：需要用户局部确认。
- 灰色：查询中、不可用或证据不足。

Qwen失败可降级为确定性部分结果；高德失败保留原始地点卡；地图失败不影响卡片；酒店缺失不报错；Provider局部失败不抹掉成功事实。

内部异常必须转换为稳定、用户友好的动作，不得泄漏 Pydantic、数据库、堆栈或模型错误。

## 11. 知识、记忆与隐私

`KnowledgeClaim` 必须绑定规范地点、类型、条件、来源、观测/生效/过期时间和许可状态。RAG只用于建议，不决定地点、路线或硬事实。未授权社交内容不得抓取或持久化。

用户记忆显式开启，只保存可查看、可修改、可删除的结构化偏好。原始攻略、截图和聊天默认不进入长期记忆；训练或评测需要单独同意。

G01即落实source隐私：`FULL`必须登录；`DEMO`只使用固定示例并绑定HttpOnly匿名session，未claim编辑24小时删除；登录用户原文和可还原SourceClaim加密保存，最长30天或直到删除行程/账号，以先到者为准。用户可分别“删除原文但保留卡片”“删除整个行程”“删除账号下全部旅行数据”；成功后fresh readback分别证明source不可用、行程410或账号旅行数据为空。删除receipt只在内部保存，原文不得进入日志、trace、分析事件或训练集。

结果页提供前两项隐私操作，账号隐私页提供“清空全部旅行数据”。删除原文和整程需要明确影响与二次确认；账号级删除需要重新验证身份，并显示处理中、完成或可重试状态。只有fresh readback满足删除合同后才能显示完成，失败不得泄漏内部job/表名或用红色错误墙驱赶用户。

## 12. 性能与版本边界

- 首次进度反馈 ≤500 ms；
- 首批可用卡片 P95 ≤8 秒；
- 标准负载（3～12个已映射地点、并发1）从首批卡片READY到路线snapshot：固定snapshot矩阵P95≤15秒，受控live dev矩阵P95≤20秒；该负载只定义性能测量，不限制更长行程；
- 已有地图snapshot时地图首屏几何P95≤1.5秒；未就绪时地图壳和用户状态≤500ms；
- 地图后台失败不延迟卡片；
- 三张截图 OCR P95 ≤12 秒；
- Top-3 基础核验 P95 ≤30 秒。

本文件定义目标行为，不表示当前实现已经具备。各能力只有通过 `../governance/RELEASE_GATES.md` 对应门禁后才能声明完成。

版本阶段固定为G01～G03 `CORE_MVP`、G04～G06 `PRODUCT_ENHANCEMENT`、G07 `CANDIDATE_HARDENING`。G03通过后固定停在`CORE_MVP_OWNER_REVIEW_PENDING`，项目所有者体验验收前不得激活G04；G07通过后停止，H1、公网、生产和商业仍为`NOT_RUN`。
