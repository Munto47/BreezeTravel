# PREDEFINED GOAL：V0.1 可信文本卡片

## Metadata

- Goal ID：`TC-VNEXT-G01-TEXT-CARDS`
- Program ID：`TC-VNEXT-2026`
- Product version：`V0.1`
- Mainline phase：`CORE_MVP`
- Gate profile：`CORE_AGENT_GATE`
- Status：`DRAFT`
- Activation：G00 Blueprint Gate通过并归档后
- Required gate：`Text Card Gate + AGENT_GATE_PASS`
- Next Goal：`TC-VNEXT-G02-MAP-STAY`

## Dependencies

- 唯一激活依赖是`TC-BP-G00-BLUEPRINT`归档且Blueprint Gate为`BLUEPRINT_READY`；随后G01按Program置为`APPROVED`。
- 首个preflight填写branch/baseline；从现有环境安全加载凭据并通过官方目录自动readback Qwen region、endpoint、exact model ID、context和Provider可暴露价格字段，未暴露字段记`NOT_EXPOSED_BY_PROVIDER`。
- 高德开发授权记录为`OWNER_ATTESTED_EXISTING_AUTHORIZATION`；凭据失效先自动诊断，只有确需新账号/费用/数据权限时才进入HITL。fixture不得冒充live准入，也不得留下0个active Goal。

## User Outcome

登录用户无需预填城市、日期和人数，粘贴自己的长文本后得到按天组织、可查看、替换、删除、插入和排序的高准确率地点卡片；未登录体验用户编辑固定北京示例，登录后才创建自己的行程。普通用户看不到原文映射、置信度、长ID、模型或后端流程。

卡片完成后，后台自动开始为同一revision准备首次地图；地图UI和住宿不在本Goal交付。

## Scope

- `TripUnderstandingRevision / DayDraft / ActivityMention / SourceClaim`；
- `StructuredInferenceProvider` 与Qwen Max/Plus/Flash面板；
- 证据编译、语义角色和确定性fallback；
- `ExecutablePlaceMention` 与高精度AMap地点解析；
- `UserFacingTripResult` 严格投影；
- v3 create/result/events/commands；
- 手机验证码优先、邮箱备用、匿名“先体验”；
- 单输入首页和逐日卡片结果；
- 结果页隐私操作：“删除原文但保留卡片”“删除整个行程”；账号隐私页“清空全部旅行数据”；
- 删除前二次确认、账号重新验证身份、处理中/完成/重试的用户友好状态；
- `TripUnderstandingJob`、lease/event/recovery与匿名资源所有权；
- `MapRenderJob / MapRenderSnapshot / PlanRevisionRef`，首批卡片后实际执行首次walking/transit预计算；地图剧场UI不在本Goal。

## Pre-approved actions

- `028_trip_understanding_v3.sql`；
- `029_map_render_snapshots.sql`；
- `/api/v3/trip-understandings`的create/result/events/commands、source删除、整程删除、demo claim和账号旅行数据级联删除；
- G00所列Qwen模型面板在自动目录readback后做dev/validation实验；
- 高德POI与walking/transit在`OWNER_ATTESTED_EXISTING_AUTHORIZATION`及现有无增量费用开发范围使用；
- 新首页和结果页；
- 结果页隐私操作与账号旅行数据隐私页；
- 旧room/v2 API保持可读。

## Parallel work packages

### Current G01 writer and evidence-task boundary

G01当前只登记`WP-G01-INTEGRATOR`一个writer，由主对话在`codex/trip-check-product-reset`及其独立worktree完成候选收口。Qwen/AMap回执、Agent A/B参考、ultra裁决、三角色复审和sealed blind属于短期只读证据任务，不是长期功能工作包：它们不拥有产品分支、不提交产品代码、不修改Goal/registry，也不形成官方`READY_TO_MERGE`。

唯一集成者串行完成Provider证据→候选冻结→参考/裁决→sealed blind→最终Gate；发现的代码问题仍由G01主对话修复。任何新的长期产品切片必须先登记完整v2提示词、独立用户可见功能对话、branch/worktree和writer名额，不能借证据子Agent绕过上限。

## Decisions locked

- 只有带原子地点的`PLANNED`提及自动搜索。
- 低置信宁可UNRESOLVED，不自动错配。
- 城市最高概率、无日期Day N、人数默认2都是可编辑软假设。
- 卡片点击只显示用户详情，不显示原文。
- 体验使用固定北京长文本和同一产品链；精确hash可使用冻结回执。
- DeepSeek只作Baseline，不能静默fallback。
- 卡片编辑创建新revision，不触发路线Provider。
- 公共地图状态只返回`PREPARING/AVAILABLE/NEEDS_UPDATE/LIMITED/UNAVAILABLE`。
- FULL必须登录；DEMO绑定HttpOnly匿名session，固定示例编辑24小时清理，source/行程/账号删除可回读。
- 模型只在dev/validation选择唯一候选，冻结后sealed blind一次。

## Non-goals

- 地图剧场、路线可视化/切换、手动重绘UI、住宿推荐；
- 完整Audit、Top-3、Repair；
- 截图、知识、记忆、分享；
- 删除旧room/API；
- H1、公网、生产、`main`。

## Dataset

- 90条：54 dev / 18 validation / 18 sealed blind；
- 三城60、其他城市15、对抗15；
- 旧根目录`tests/`中的19条未完成旅行文本已按项目所有者要求删除，不再作为regression、oracle或数据源；本Goal从90条受治理数据重新建立基线；
- 两个隔离的`gpt-5.6-sol / xhigh`任务独立生成agent reference，新的`gpt-5.6-sol / ultra`任务在A/B冻结后裁决，family隔离；
- validation与blind各至少65个gold executable mentions；结合coverage≥80%仍须直接验证auto-selected分母≥50，不能只按gold数量推断；
- blind答案由不继承开发上下文的独立Codex任务在仓库外保管；只在dev/validation选模，唯一候选冻结后blind一次。

## Acceptance

完全继承Text Card Gate，尤其：

- 整句/URL/描述/预约作为地点0；
- 错城/错类别严重自动匹配0；
- auto match precision≥99%，validation和blind的auto-selected分母分别≥50；
- executable mention precision≥98%、recall≥95%；
- day F1≥97%、role macro-F1≥94%；
- 证据有效率100%，普通用户可见率0%；
- 首批卡片P95≤8秒；
- Qwen/AMap失败仍有部分可编辑结果；
- login/demo/edit/refresh/concurrency/idempotency浏览器通过；
- 理解job重启/lease/SSE可恢复，重复副作用0；
- 初次地图job实际执行；只有故障oracle case允许PARTIAL/UNAVAILABLE，正例必须满足下述可用覆盖；逻辑重复Provider调用0，地图失败不影响卡片；
- 标准3～12地点负载从卡片READY到可用snapshot：snapshot P95≤15秒、受控live dev P95≤20秒；
- 30份行程、120条已知成功路线正例中snapshot可用覆盖100%、受控live dev≥95%；永远UNAVAILABLE不能过Gate；
- source TTL/delete、匿名越权、日志/trace/分析泄漏全部通过。

## Verification

- schema、compiler、role、query qualification和fallback单测；
- model panel frozen eval；
- AMap fixture/snapshot与受控dev调用；
- PostgreSQL migration 028/029、CAS、job lease/event、逻辑幂等、重启；
- public JSON禁止字段扫描；
- DOM与无障碍检查；
- backend pytest/Ruff、frontend build；
- 浏览器登录、固定体验、文本、编辑、刷新，以及结果页source/整程删除、二次确认、完成/重试和fresh readback；
- 账号隐私页重新验证身份、清空旅行数据、异步状态与完成后空readback；
- H1/公网/生产：`NOT_RUN`。
- Agent Gate：三个隔离审查角色、fresh ultra裁决、sealed agent blind与同commit干净checkout回读。

## Authority

- `AGENTS.md`、Charter、Spec、v3 API、Architecture；
- Program、Roadmap、Release Gates、Agent Gate Protocol、Provider Admission、Risk Register；
- ADR-007、ADR-008、ADR-009、ADR-012、ADR-013、ADR-014。

## Baseline

- branch/commit/upstream：激活时由G00治理过渡commit填写并远端readback；
- 当前用户可见行为、旧OpenAPI snapshot、Qwen/AMap准入结果：激活时现场记录；
- 历史Intake/Candidate只作资产基线，不是G01 PASS；
- H1、公网、生产、商业：`NOT_RUN`。

## Invariants

- 公共JSON/DOM禁止原文、置信度、内部ID/状态/模型/Provider；随机`public_resource_id`不授权且不得渲染或进入日志/分析，匿名capability只在HttpOnly cookie；
- 只有原子`PLANNED`提及可搜索，严重错配为0；
- `PlanRevisionRef`、ETag、CAS、请求幂等和地图逻辑唯一键必须一致；
- LLM不产生POI/路线事实；`UNKNOWN/UNAVAILABLE`不算PASS；
- card edit路线Provider调用为0；source隐私与旧API兼容不可弱化。

## Budget

- 单文本≤50,000 Unicode code point、≤14天、≤80个可执行活动、每账号并发理解job≤2；超限为可编辑`LIMITED`；
- 每任务模型最多1次初始+1次schema修复；POI最多每个ExecutableMention一次主搜索和一次确定性改写；初次路线最多walking/transit各一次/相邻边；
- 不设总费用硬上限，但每次调用记exact binding、token、latency、retry和估算费用；不新增账号/绑卡/付费；
- 每个可回滚切片commit/push并更新checkpoint。

## HITL

新账号/费用/扩大数据权限、未预批准schema/migration/依赖、读取或修改blind truth/oracle、H1/公网/生产/`main`或删除旧数据时请求人工批准；按协议启动Agent评测和sealed blind不属于HITL，普通实现/测试/Gate失败不请求用户诊断。

## Checkpoint ledger

| 时间 | 用户结果 | Commit | Verification | Evidence level | Product progress | Governance ratio | Remaining | Risk/failure | Next autonomous action |
|---|---|---|---|---|---|---|---|---|---|
| 激活时填写 |  |  |  |  |  |  |  |  |  |

## Auto-advance

- Required gate：`Text Card Gate`；Next template：`TC-VNEXT-G02-MAP-STAY.md`；
- subject push/readback、耐久`AGENT_GATE_PASS`、clean tree、无Stop后，生成完整completed归档，并原子更新Goal binding与work-package registry激活G02；不登记外部ledger、不创建authority generation；
- FUX-01、H1、公网、生产、商业和`main`不自动启动。

## Completion record

- Status / Subject commits / Remote branch：激活后填写；
- Verification / Evidence / Gate result / `structurally_valid`：激活后填写；
- H1 / production / commercial：激活时固定为`NOT_RUN / NOT_RUN / NOT_RUN`；
- User-visible result / Remaining risks / Goal archived / Next activated：激活后填写；
- Promotion decision：`NOT_REQUESTED`。

## Stop conditions

- 需要降低严重错配为0的门禁；
- 需要新增付费账号或未批准Provider；
- 必须修改sealed blind/oracle；
- 无法保持旧API可读；
- 需要降低整句/URL/错城/错类别零容忍门禁。
