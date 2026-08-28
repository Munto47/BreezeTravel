# IN_PROGRESS GOAL：V0.1 可信文本卡片

Goal ID: TC-VNEXT-G01-TEXT-CARDS
Status: IN_PROGRESS
Goal type: PRODUCT_VERTICAL_SLICE

<!-- AGENT_GATE_CURRENT_GOAL_STATE
{
  "schema_version": "current-goal-document-state-v1",
  "program_id": "TC-VNEXT-2026",
  "goal_id": "TC-VNEXT-G01-TEXT-CARDS",
  "goal_status": "IN_PROGRESS",
  "required_gate": "Text Card Gate + AGENT_GATE_PASS",
  "completion_status": "PENDING",
  "gate_result": "AGENT_GATE_NOT_RUN",
  "goal_archived": false,
  "next_goal_id": "TC-VNEXT-G02-MAP-STAY",
  "next_activated": false,
  "h1_status": "NOT_RUN",
  "production_status": "NOT_RUN",
  "commercial_status": "NOT_RUN"
}
-->

## Metadata

- Goal ID：`TC-VNEXT-G01-TEXT-CARDS`
- Program ID：`TC-VNEXT-2026`
- Product version：`V0.1`
- Status：`IN_PROGRESS`
- Goal type：`PRODUCT_VERTICAL_SLICE`
- Branch：`codex/trip-check-product-reset`
- Canonical integration subject：`origin/develop@d114d6a1e9a06b1e26fb62519710e35d50300d70`，远端readback `PASS`
- Implementation baseline/upstream：`origin/codex/trip-check-product-reset@d114d6a1e9a06b1e26fb62519710e35d50300d70`，现场`ls-remote`与clean-tree readback `PASS`
- Blueprint subject commit：`3fb3d0566c5742ecf4fac4179021ee538ef5b516`
- Activation commit：`f3b5f3e0c36ff3977f826bd82a83b3150a2e97ac`，远端readback `PASS`
- Latest delivered checkpoint：真实Qwen → 高德地点 → PostgreSQL卡片 → 异步walking/transit链 `c48aea6c279e04e8870c4a81349324388e82a6f2`，tree `7e2a34b16369c757c8027f545d16310f94e25628`；远端readback在本checkpoint push后执行
- Activation：`TC-BP-G00-BLUEPRINT`已归档且Blueprint Gate为`BLUEPRINT_READY`
- Approved by / at：User / 2026-08-27
- Required gate：`Text Card Gate + AGENT_GATE_PASS`
- Next Goal：`TC-VNEXT-G02-MAP-STAY`

## Dependencies

- 唯一激活依赖`TC-BP-G00-BLUEPRINT`已归档且Blueprint Gate为`BLUEPRINT_READY`；本Goal已按Program置为`APPROVED`。
- 当前环境中的既有Qwen凭据由程序安全加载，并通过官方目录自动readback region、endpoint、exact model ID、context和Provider可暴露价格字段；未暴露字段记`NOT_EXPOSED_BY_PROVIDER`。
- 高德开发授权记录为`OWNER_ATTESTED_EXISTING_AUTHORIZATION`；凭据或目录失败先自动诊断。只有确需新账号/费用/数据权限时才进入HITL；fixture不得冒充live准入，也不得留下0个active Goal。
- 本Goal使用`CORE_AGENT_GATE`。候选必须绑定同一commit/config/data、prompt/schema和scorer，并完成自动化、所需live Provider、三角色审查、ultra裁决、一次sealed blind和clean checkout readback。
- 已有generation-1 `BOOTSTRAP`、authority verifier、purpose-specific broker设计和相关schema保留为`DEFERRED_CANDIDATE_HARDENING`；G01不继续实现、不切`ACTIVE`，其`NOT_RUN`不阻断Text Card Gate。是否复用由G07基于明确威胁模型重新决定。

## Current execution directive

项目所有者于2026-08-28要求把“治理挤占产品主线”的踩坑固化为约束。本Goal立即执行以下纠偏，优先级高于Checkpoint ledger中历史行的旧“下一自主动作”：

1. 不再继续仓库外authority broker/supervisor、八角色签名、activation-readiness或隔离OCI供应链建设；现有实现和证据保留，不删除、不冒充PASS。
2. 下一自主动作切回Qwen账号自动发现、模型中立adapter和Max/Plus/Flash同数据比较。
3. 随后接通高德POI真实地点映射与最小脱敏live回执，验证严重错配为0。
4. 再运行Agent A/B、ultra裁决、一次sealed blind和完整Text Card Gate。
5. 只修复可复现的当前Goal P0/P1及blocking P2；其他P2登记到后续Goal或风险清单。
6. 从本指令起，纯治理切片不得连续；两个无产品/模型/Provider/产品指标进展的checkpoint会强制回到主线。

## User Outcome

登录用户无需预填城市、日期和人数，粘贴自己的长文本后得到按天组织、可查看、替换、删除、插入和排序的高准确率地点卡片；未登录体验用户编辑固定北京示例，登录后才创建自己的行程。普通用户看不到原文映射、置信度、长ID、模型或后端流程。

卡片完成后，后台自动开始为同一revision准备首次地图；地图UI和住宿不在本Goal交付。

## Scope

- `TripUnderstandingRevision / DayDraft / ActivityMention / SourceClaim`；
- `StructuredInferenceProvider`与Qwen Max/Plus/Flash面板；
- 证据编译、语义角色和确定性fallback；
- `ExecutablePlaceMention`与高精度AMap地点解析；
- `UserFacingTripResult`严格投影；
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
- G00所列Qwen模型面板在官方目录自动readback后做dev/validation实验；
- 高德POI与walking/transit在`OWNER_ATTESTED_EXISTING_AUTHORIZATION`及现有无增量费用开发范围使用；
- 新首页和结果页；
- 结果页隐私操作与账号旅行数据隐私页；
- 旧room/v2 API保持可读。

## Decisions locked

- 只有带原子地点的`PLANNED`提及自动搜索。
- 低置信宁可`UNRESOLVED`，不自动错配。
- 城市最高概率、无日期`Day N`、人数默认2都是可编辑软假设。
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
- 外部authority、目的专一broker、八角色签名、activation-readiness和完整OCI供应链证明；这些属于G07候选加固；
- H1、公网、生产、`main`。

## Dataset

- 90条：54 dev / 18 validation / 18 sealed blind；
- 三城60、其他城市15、对抗15；
- 旧根目录`tests/`中的19条未完成旅行文本已按项目所有者要求删除，不再作为regression、oracle或当前数据源；G01必须从本合同的90条受治理数据重新建立可复现基线；
- 两个隔离的`gpt-5.6-sol / xhigh`任务独立生成agent reference，新的`gpt-5.6-sol / ultra`任务在A/B输出冻结并hash后裁决，family隔离；
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
- current binding与Program表中的G01顺序、前驱和`CORE_AGENT_GATE`合同一致；候选commit/config/data、prompt/schema/scorer和全部required回执绑定一致。
- `DEFERRED_CANDIDATE_HARDENING`不得被误报为已运行，也不得作为G01 required项。

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
- `CORE_AGENT_GATE`：三个隔离审查角色、fresh ultra裁决、sealed agent blind、确定性scorer和同commit干净checkout回读。
- Qwen/AMap live回执绑定candidate commit/tree、Goal/split、exact Provider配置、请求purpose、脱敏请求/响应hash、Provider request ID（若提供）、时间、token/latency/repair/费用和持久化effect ID；不保存key、完整原文或完整响应。外部签名/capture/OCI HARDENED链在G01为`DEFERRED / NOT_RUN`。

## Authority

- `AGENTS.md`、Charter、Spec、v3 API、Architecture；
- Program、Roadmap、Release Gates、Agent Gate Protocol、Product Mainline Execution Guide、Provider Admission、Risk Register；
- ADR-007、ADR-008、ADR-009、ADR-012、ADR-013、ADR-014。

## Baseline

- branch/upstream：`codex/trip-check-product-reset` / `origin/codex/trip-check-product-reset`；
- canonical integration subject：`origin/develop@d114d6a1e9a06b1e26fb62519710e35d50300d70`，远端readback `PASS`；
- implementation baseline：`origin/codex/trip-check-product-reset@d114d6a1e9a06b1e26fb62519710e35d50300d70`；写入前`ls-remote`、HEAD与clean-tree一致；
- current delivered subject：`c4b601467abc880981f50117de3099f25cb332bf`，tree `49aa0f65d71aa38f367cd1b924313cc35ed84867`；本checkpoint push后更新远端subject/tree/file readback；
- activation transition：`f3b5f3e0c36ff3977f826bd82a83b3150a2e97ac`，远端readback `PASS`；
- Blueprint subject：`3fb3d0566c5742ecf4fac4179021ee538ef5b516`，远端readback `PASS`；
- 旧OpenAPI兼容基线：99 paths / 106 operations，SHA-256 `0a616cf711b260a232d20aca80d6904743327ff9dcbc2808356c62066fc55a81`；现场旧容器94 paths、v3为0，缺微信登录1条和截图上传批次4条，登记为`LEGACY_CONTAINER_DRIFT`；
- 当前生成OpenAPI：116 paths，其中v3为11；与99路径冻结快照物理隔离，新增17路径全部机器归类且旧路径/方法零缺失；`UserFacingTripResult.status`已按冻结预算合同包含可编辑`LIMITED`；
- Qwen live lane：账户目录`DISCOVERED`，实际暴露20个模型；候选为Max `qwen3.8-max`、Plus `qwen3.7-plus-2026-05-26`、Flash `qwen3.7-flash-2026-07-15`，context均991808且目录声明structured output；region/workspace为`NOT_EXPOSED_BY_PROVIDER`；模型中立adapter已接入FULL worker的显式live模式，固定非思考/温度0.1/7秒总deadline/最多一次修复，失败只走本地确定性`PARTIAL_RESULT`且不调用DeepSeek；唯一模型尚未冻结；
- AMap live lane：POI 2.0仅接收原子`PLANNED`地点，北京/上海/杭州只在唯一规范化精确名称、城市一致、类别不冲突时自动采用；其他情况保持“地点待确认”。walking/transit v3并行计算并写入现有地图effect表；真实北京长文本链持久化4个自动地点、6个路线mode effect/2次真实路线调用，地图`LIMITED`但不阻断6张卡，编辑后自动路线调用增量0；key、完整原文和完整响应均未写入回执；
- 历史Candidate：`HISTORICAL_BINDING_INVALID / FROZEN`；10/10数据、schema和generator绑定有效，validator/scorer/gate绑定失效；不得修改manifest、blind、oracle或冻结证据；
- G01 Text Card数据：独立90条输入已生成并字节绑定，`54 dev / 18 validation / 18 frozen_blind`、`60 DEEP_CITY / 15 OTHER_CITY / 15 ADVERSARIAL`、30个family各A/B/C且不跨split；仓库内旧human label/gold/oracle为0，历史v1 schema与manifest保持逐字节只读；
- G01标注与评分：agent evaluation v2使用A/B隔离标注、fresh ultra裁决、逐字span、live Provider receipt、仓库外原始输出和validation最小分母的fail-closed合同；通用开发scorer拒绝读取blind；当前状态为`AGENT_EVALUATION_V2_PROTOCOL_PENDING / TEXT_CARD_GATE_NOT_RUN`，不是HITL；
- 本地确定性proposal baseline：只读取dev/validation，dev `54 cases / 80 eligible / 53 auto-matched`，validation `18 / 5 / 5`，external calls、human labels和blind reads均为0；没有gold故质量`NOT_SCORED`，且validation auto-selected分母5明确低于门槛50；
- 地图正例fixture：北京/上海/杭州各10份、每份5个已映射地点与4条相邻边，共30份行程/120条唯一有向边；真实`MapRenderWorker → MapRenderer`受控矩阵生成30个READY snapshot、walking/transit各120个可用mode fact、可用覆盖100%、逻辑重复请求0、external call 0、worker→snapshot P95 `0.45ms`；只计`CONTROLLED_FIXTURE`子门禁，live高德和完整Gate仍为`NOT_RUN`；
- Provider故障与预算：只有显式脱敏的typed unavailable会被降级，普通代码/schema异常不吞掉；Qwen候选故障用本地确定性语义返回`PARTIAL_RESULT`且不使用DeepSeek，地点故障保留六张可确认卡，单一路线模式故障隔离到该模式；81个可执行活动保留81张卡、只发起前80次地点解析并返回`LIMITED`，没有静默截断；以上均为test double/fixture，不是live调用；
- PostgreSQL地图正例矩阵：fresh database应用现有migration后，30份FULL理解结果各原子创建初次地图job；独立worker持久化30个READY job/snapshot、120条selected edge、240个AVAILABLE mode fact与240个Provider effect receipt，external calls与重复逻辑请求均为0，P95门槛断言≤15秒，旧`rooms`表仍存在；数据库结束后安全删除，仅证明受控fixture持久链；
- 最终本地自动验证：后端全量`2054 PASS / 34 SKIP / 2 FAIL`，两项均为已登记的冻结Candidate manifest代码绑定失败，新增失败0；S0、fresh PostgreSQL、90条数据合同、地图矩阵、Ruff、OpenAPI、共享client、前端生产build和真实本地浏览器链均通过；Text Card Gate仍为`NOT_RUN`，不因此宣称Goal完成；
- G00治理结构验证`structurally_valid=true`；这只证明蓝图结构，不证明V0.1产品能力；
- 历史Intake/Candidate只作资产基线，不是G01 PASS，不得因此宣称 `V1_CANDIDATE_READY`；
- H1、公网、生产、商业：`NOT_RUN`。

## Invariants

- 公共JSON/DOM禁止原文、置信度、内部ID/状态/模型/Provider；随机`public_resource_id`不授权且不得渲染或进入日志/分析，匿名capability只在HttpOnly cookie；
- 只有原子`PLANNED`提及可搜索，严重错配为0；
- `PlanRevisionRef`、ETag、CAS、请求幂等和地图逻辑唯一键必须一致；
- LLM不产生POI/路线事实；`UNKNOWN/UNAVAILABLE`不算PASS；
- card edit路线Provider调用为0；source隐私与旧API兼容不可弱化。
- G01～G07顺序、前驱和自动Gate合同由Program表固定；G01完成回执和远端readback是G02晋级依据，候选binding不能替代实际Gate结果。

## Budget

- 单文本≤50,000 Unicode code point、≤14天、≤80个可执行活动、每账号并发理解job≤2；超限为可编辑`LIMITED`；
- 每任务模型最多1次初始+1次schema修复；POI最多每个ExecutableMention一次主搜索和一次确定性改写；初次路线最多walking/transit各一次/相邻边；
- 不设总费用硬上限，但每次调用记exact binding、token、latency、retry和估算费用；不新增账号/绑卡/付费；
- 每个可回滚切片commit/push并更新checkpoint。

## HITL

新账号/费用/扩大数据权限、未预批准schema/migration/依赖、读取或修改blind truth/oracle、H1/公网/生产/`main`或删除旧数据时请求人工批准。按协议启动Agent评测和sealed blind不属于HITL；普通实现、测试、Provider诊断或Gate失败不请求用户诊断。

## Checkpoint ledger

下表是不可改写的历史执行记录；其中旧行的“下一自主动作”只说明当时决策，不覆盖上方`Current execution directive`。从本指令后的新checkpoint必须在Verification或Risk/failure中记录`Product progress`和`Governance ratio`。

| 时间 | 用户结果 | Commit | Verification | Evidence level | Remaining | Risk/failure | Next autonomous action |
|---|---|---|---|---|---|---|---|
| 2026-08-27 | Blueprint完成并激活V0.1可信文本卡片Goal；尚未修改产品代码 | `f3b5f3e0c36ff3977f826bd82a83b3150a2e97ac` | G00 Blueprint Gate `PASS`；单active Goal结构检查；remote readback `PASS` | `BLUEPRINT_ONLY` | 依赖readback、代码/合同现场审计与G01纵向切片 | Qwen/高德准入尚未现场确认 | 执行G01首个preflight与当前实现/合同审计 |
| 2026-08-27 | 完成全仓分支与worktree审计；miniapp 33个源码/配置/测试文件纳入统一Git基线；删除根`tests/`；`develop`与当前实现分支统一到Blueprint | `1e679dc7e006b84e9a984eba1ace028b291fa493` | subject与`origin/develop`readback PASS；Ruff、Web build、共享client typecheck/build、小程序typecheck/7 tests/build、双入口结构检查、12项治理兼容、remediation新checkout 3 tests均PASS；backend全量2017 PASS/32 SKIP/3 FAIL | `REPOSITORY_CONSOLIDATION_WITH_LEGACY_FAILURES` | G01依赖preflight、代码/合同现场审计和首个纵向切片 | backend剩余2项旧candidate manifest代码绑定漂移；两个旧P5 v5 worktree未提交草稿均冻结且不并入；未修改oracle | 执行G01首个preflight与现有实现/合同审计 |
| 2026-08-27 | G01-S0完成：旧入口处置、99路径兼容基线、v3公共边界和Provider边界已固化；尚未交付新页面 | `7986214c1b236217ceb5d2d55f8cecc882e03f2b` | 6组件/12页面/99旧路径/6源码新增路径/29模块/220测试组唯一归类PASS；legacy方法零缺失；冻结diff为0；S0 pytest 2 PASS/1环境SKIP；Ruff PASS；历史Candidate原样18 PASS/3 SKIP/2 FAIL；subject push、`ls-remote`、subject/tree/远端文件readback PASS | `S0_CHECKPOINT / HISTORICAL_FAILURE_DIAGNOSED` | migration 028、持久worker、v3 create/events/result和北京三日六卡UI | Candidate绑定失效保持FROZEN；Qwen NOT_READY；AMap等待书面许可；不阻塞fixture纵向切片 | 实现migration 028及持久化DEMO create→events→result链 |
| 2026-08-27 | 固定北京体验已贯穿真实v3 create/events/result：匿名用户从无前置表单首页得到Day 1～3六张地点卡，刷新可恢复；地图与住宿诚实显示不可用 | Demo `d6ab378a1f7d169efc94422e0b7611e3c8a49d0c`；compat `3106fe00b755e603bce5517f5ddea71e78e17214` | 原生Windows全量`2026 PASS / 33 SKIP / 2 FAIL`，仅两项登记的Candidate绑定失败；定向10 PASS；Ruff PASS；frontend build PASS；共享client typecheck/build PASS；真实Compose Playwright 1 PASS；应用内浏览器create/六卡/刷新/DOM脱敏/console回读PASS；028 applied=1、v3 tables=14、旧rooms=5171；remote hash/subject/tree/file readback PASS | `FIXTURE_DEMO_VERTICAL_SLICE / AUTOMATED_LOCAL_BROWSER` | FULL文本语义、真实登录所有权、地点解析、卡片commands、claim/source与整程删除、029初次地图任务、90条数据与Text Card Gate | 历史Candidate保持`HISTORICAL_BINDING_INVALID / FROZEN`；Qwen `NOT_READY`；AMap `BLOCKED_PENDING_WRITTEN_PERMISSION`；本轮完整后端镜像重建因锁定PaddlePaddle 194.8MB在慢镜像源下载而中止，但运行中backend/worker已是相同运行时代码的d6ab镜像，3106仅改离线兼容脚本，前端3106镜像已切换；H1/公网/生产/商业/main `NOT_RUN` | 从当前checkpoint实现FULL登录文本链、严格地点资格与持久结果，再交付commands/删除/claim和029地图任务 |
| 2026-08-27 | 登录用户可在首页直接粘贴1～50,000字符文本，经真实持久worker得到逐日卡片并刷新恢复；未知或仅参考内容不虚构地点；用户Source与逐字claim加密，公共结果和DOM不含原文/内部字段 | `2bdac7ce47c9d9ecc9c55c5e720908e0c238bf50` | FULL/DEMO单元与API 10 PASS；S0/兼容定向13 PASS/1环境SKIP；PostgreSQL fresh-db integration 1 PASS，证明登录所有权、跨用户拒绝、密文Source/quote、重放和旧表兼容；Ruff PASS；frontend生产build PASS；真实Compose Playwright 2 PASS；runtime日志原文命中0、实际resource path命中0、脱敏模板22；remote hash/subject/tree/file和clean-tree PASS | `CONSERVATIVE_FULL_TEXT_VERTICAL_SLICE / LOCAL_FIXTURE / AUTOMATED_BROWSER` | 卡片commands、claim、source/整程/账号删除、029初次地图任务、90条受治理数据、模型/Provider准入与Text Card Gate | 当前FULL语义为保守确定性+受控POI snapshot，不代表Qwen/AMap live；Qwen `NOT_READY`、AMap `BLOCKED_PENDING_WRITTEN_PERMISSION`；历史Candidate冻结失败未变；H1/公网/生产/商业/main `NOT_RUN` | 实现If-Match+幂等的卡片commands及claim/删除隐私链，再交付029地图job |
| 2026-08-27 | 用户可在结果页排序、跨天移动、插入、编辑、替换和删除卡片，也可修改软假设；每次调整产生新ETag/不可变revision并明确提示地图需要手动更新 | `dacee589d9dbfb04a94ae7acc04a00946abc4710` | commands六分支纯函数与API/CAS定向14 PASS；PostgreSQL fresh-db integration 1 PASS；Ruff PASS；frontend生产build PASS；真实Compose Playwright 2 PASS并在同一FULL资源执行排序/插入/编辑/删除/刷新；runtime readback revision 2→6、understanding job=1、Provider side-effect=1、map=NEEDS_UPDATE；actual resource path日志命中0；remote hash/subject/tree/file和clean-tree PASS | `REVISION_SAFE_COMMANDS / LOCAL_FIXTURE / AUTOMATED_BROWSER` | DEMO claim、source/整程/账号删除、029初次地图任务、90条数据、Provider准入与Text Card Gate | 卡片token按revision轮换；编辑不会触发路线；live门禁和历史Candidate边界未变化 | 实现claim与三层删除的授权、幂等、回执和fresh readback |
| 2026-08-27 | 匿名北京示例可在登录后领取并轮换公开地址；登录用户可删除原文但保留卡片、删除单份行程，或在账号隐私页清空全部v3旅行数据；所有删除均有授权绑定、幂等和fresh readback | `1ed7927813fcf197db519ccdad3b7472239eeb46` | v3 API/领域16 PASS；PostgreSQL fresh-db integration 1 PASS，覆盖领取、原文密文清理、FK安全整单/账号级清理与伪匿名回执；S0/P6兼容5 PASS；Ruff PASS；frontend与共享client build/typecheck PASS；OpenAPI当前114路径零漂移、旧99路径原hash不变；真实Compose Playwright 3 PASS；历史Candidate原样18 PASS/3 SKIP/2 FAIL；remote hash/subject/tree/file readback PASS | `PRIVACY_DELETION_VERTICAL_SLICE / LOCAL_AUTOMATED_BROWSER` | 029初次地图任务、90条受治理数据、Provider准入与Text Card Gate | 全量pytest首跑2035 PASS/33 SKIP/4 FAIL；其中新增的OpenAPI审计与P6文案失败已定向修复，最终全量重跑仍待后续切片；Qwen/AMap与冻结Candidate边界未变化 | 实现029地图job/snapshot、首次预计算和手动更新API |
| 2026-08-28 | 首批卡片完成后，独立worker自动为同一行程版本实际计算walking/transit并持久化不可变地图快照；编辑不自动重算，只返回`NEEDS_UPDATE`，手动map-renders API才为新版本排队 | `ccfe16e0d110ed0243576f327c1df93dcb8e61a0` | v3/S0定向25 PASS；fresh/existing migration与PostgreSQL地图链3 PASS；Ruff PASS；OpenAPI 116路径零漂移、旧99路径SHA不变；共享client和frontend生产build PASS；真实Compose Playwright 3 PASS；runtime 029 applied、旧rooms=5171、最新snapshot READY、3 edges/6 mode facts、external calls=0；remote hash/subject/tree/file readback PASS | `REVISION_BOUND_MAP_BACKEND / CONTROLLED_FIXTURE / AUTOMATED_LOCAL_BROWSER` | 90条受治理数据、双人标注/裁决、Qwen/AMap受控lane与Text Card Gate；最终全量pytest重跑 | live Qwen仍`NOT_READY`、AMap持久化仍等待书面许可；本切片只证明fixture地图后端，不是live Provider、H1、公网或生产证据；历史Candidate冻结失败未变 | 建立独立的90条G01 Text Card数据合同、标注工作流和dev/validation确定性基线，绝不修改历史Candidate资产 |
| 2026-08-28 | 独立G01 Text Card主集、双人标注/裁决合同、确定性scorer与完整Gate阈值已机器化；开发runner只运行dev/validation且无法读取blind | `9ebdb3b83633e20d4f281a7fe4fd758748aaf5e4` | 数据生成逐字节复现；90 case/30 family/54-18-18/60-15-15校验PASS；目标测试26 PASS；全backend Ruff PASS；冻结Candidate目录零diff；subject push及remote subject/tree/contract readback PASS；fixture baseline 72 case、external/human/blind reads均0 | `GOVERNED_INPUT_CORPUS / PROPOSAL_ONLY_LOCAL_FIXTURE / HUMAN_GOLD_PENDING` | 30行程/120边地图正例fixture矩阵、双人真人标注/裁决、Qwen exact账号绑定、高德书面持久化许可、候选冻结后一次sealed blind与完整Text Card Gate | validation fixture仅5个auto-selected，低于50分母且无human gold，明确`NOT_SCORED`；Gate为`HITL_PENDING`，不是FAIL或PASS；历史Candidate未修改 | 继续自主建立30行程/120边地图正例与剩余离线runtime矩阵；随后把真人标注、Qwen账号readback和高德书面许可收敛为最小HITL动作 |
| 2026-08-28 | 三城地图正例从输入合同贯穿真实内存worker与renderer：30份行程的120条相邻边均同时得到walking/transit事实与可用snapshot | `1bee280eff6fc9fc607e44fe6f8c9feccf81b5e2` | fixture与生成器逐字节绑定；北京/上海/杭州各10份、120唯一有向边；真实worker矩阵30 READY、120/120 usable、walking/transit各120、walking选择93/transit 27、重复请求0、external calls 0、P95 0.45ms；目标测试29 PASS；Ruff PASS；remote subject/tree/contract readback PASS | `MAP_POSITIVE_FIXTURE_SUBGATE_PASS / IN_MEMORY_WORKER / NON_LIVE` | Provider失败部分结果、预算与完整runtime矩阵；PostgreSQL 30份持久化矩阵；双人真人标注、Qwen/AMap准入、sealed blind与完整Gate | 合成路线数字不是高德事实；本证据不能替代live或PostgreSQL矩阵，完整Text Card Gate保持`NOT_RUN` | 继续实现Qwen/AMap失败时可编辑部分结果与剩余离线runtime Gate，再运行最终全量验证 |
| 2026-08-28 | 模型、地点或单一路线模式暂不可用时仍保留可编辑卡片/可用路线；超过80个可执行活动不静默截断，返回`LIMITED` | Runtime `80a9dc3835f1685f80a6d06513a0f82c22368091`；Compose `cf9003c97e0c5b3350021c102183a0d39bbf269c` | typed failure与81活动测试33 PASS；fresh PostgreSQL 1 PASS；Ruff、当前OpenAPI、共享client typecheck/build、frontend生产build PASS；本地Compose Playwright首轮因缺baseURL在导航前失败，显式`E2E_BASE_URL`后3 PASS；backend/两worker同镜像且health 200、近期ERROR 0；远端readback PASS | `OFFLINE_FAILURE_DEGRADATION_PASS / CONTROLLED_TEST_DOUBLE / LOCAL_BROWSER` | PostgreSQL 30份地图持久化矩阵、全量pytest最终重跑；双人真人标注/裁决、Qwen账号exact binding、高德书面许可、dev/validation选模与sealed blind | 测试故障不是live Qwen/AMap证据；migration 028的历史effect_type枚举仍名为fixture，若在当前Goal改成provider-neutral值需要新增未预批准migration，当前只在JSON receipt记录真实binding | 运行最终全量pytest和冻结Candidate诊断；继续可自主的PostgreSQL地图矩阵，随后请求最小HITL输入 |
| 2026-08-28 | 30份三城正例已穿过fresh PostgreSQL理解结果、初次地图job、worker、不可变snapshot、edge/mode fact和effect receipt全链 | `869bdaf282cbf7ab0e2b4f249426d49061bd5593` | 独立integration 1 PASS/3.91s；30 READY jobs/snapshots、120/120 selected edges、240/240 AVAILABLE mode facts、240 effect receipts、external calls 0、请求三元组唯一、P95≤15s断言PASS、旧rooms表存在；同轮G01目标33 PASS+现有PostgreSQL 1 PASS、Ruff PASS；remote subject/tree/test readback PASS | `POSTGRES_MAP_POSITIVE_FIXTURE_SUBGATE_PASS / FRESH_DATABASE / NON_LIVE` | 最终全量pytest与Candidate冻结诊断；双人真人标注/裁决、Qwen exact账号、AMap书面许可、dev/validation模型比较与一次sealed blind | live覆盖仍`NOT_RUN`；合成路线事实不得进入用户路线权威；完整Text Card Gate仍为`HITL_PENDING` | 运行最终全量pytest、OpenAPI/client/frontend/浏览器回归及远端clean readback，确认只剩外部HITL项 |
| 2026-08-28 | 可自主的G01本地实现与回归已收口；`localhost:3000`继续提供匿名北京示例、登录FULL文本、编辑/删除与地图后台链 | `e99ed68412d54978ac4f187ccb3e5e9b3659d57f` | 后端全量2054 PASS/34 SKIP/2冻结FAIL，新增失败0；Candidate单跑18 PASS/3 SKIP/2冻结FAIL；fresh PostgreSQL 2 PASS；S0 3 PASS且冻结diff 0；90条合同PASS/Gate `HITL_PENDING`；30/120矩阵PASS；Ruff/OpenAPI/client/frontend PASS；Playwright 3 PASS；首页/health 200，三进程近期ERROR 0 | `LOCAL_AUTOMATED_REGRESSION_COMPLETE / HISTORICAL_BINDING_INVALID_FROZEN / TEXT_CARD_GATE_HITL_PENDING` | 两名独立标注员与第三名裁决员；Qwen账号region/workspace/exact model/价格绑定；AMap持久化书面许可；候选冻结后的外部custodian一次sealed blind | H1、live Provider、公网、生产、商业、main均`NOT_RUN`；无授权不得读取blind truth或发起高德持久化调用 | 等待最小HITL输入；到齐后完成双标/裁决、同数据候选比较、冻结候选、外部sealed blind与完整Text Card Gate |
| 2026-08-28 | 旧H1前真人硬门禁已替换为诚实分级的Agent Gate治理合同；G01建立不可签发PASS的generation-1 BOOTSTRAP、Agent评测v2、外部分离签名边界、一次性sealed custody和完整自动门禁协议，公共产品代码与API未改 | `0a79f82db0a22d027ee145265f483986640411a8` | 精确候选后端全量2097 PASS/32 SKIP/0 FAIL；Agent Gate/治理/G01 eval定向61 PASS；PostgreSQL typed registry显式启用1 PASS；Ruff与cached diff-check PASS；真实Docker 28.5.2同daemon OCI archive/load/readback PASS、cross-daemon `NOT_RUN`；产品/API/frontend/packages/miniapp diff 0；架构、可靠性、产品三路复审与独立裁决均为0 P0/P1/当前Goal P2，裁决`ALLOW_BOOTSTRAP_CHECKPOINT`；subject/tree/6关键blob远端回读PASS | `AUTOMATED_TEST / MULTI_AGENT_SIMULATED_REVIEW / BOOTSTRAP_ONLY` | 实现仓库外两阶段signer IPC和直接HTTPS capture执行链；签发activation readiness后才可切ACTIVE；再运行Qwen/AMap live、Agent A/B/ultra、sealed blind及完整Text Card Gate | authority仍为`BOOTSTRAP`；正式signer/capture、Qwen/AMap、Agent reference/adjudication、sealed blind、四组件、Agent Gate、H1、生产、商业均`NOT_RUN`；Agent审查不是真人证据 | 在当前G01实现并攻击测试仓库外signer/capture链；闭合后由独立custody签activation readiness，原子切ACTIVE并登记generation-1 anchor，不激活G02 |
| 2026-08-28 | 建立只读、fail-closed的外部authority verifier foundation：候选仓库只能验证独立custody签名的目的专一broker conformance回执；generic payload/role/verdict签名接口和候选持钥模板均不存在；协议代码、全部schema与Final Gate运行时来源形成可执行hash闭包 | `7d77ed01f133039fa5b0b97522480f7a79864773` | exact commit完整两份contract `_validate_contract_code_bindings` PASS、全部protocol schema Git readback PASS；目标测试92 PASS；Ruff/diff-check PASS；后端全量2152 PASS/35 SKIP/3 FAIL，其中2项为既有冻结NLU Candidate绑定失败，1项旧P4 solver在并行负载下2秒超时，隔离同用例1 PASS/2.22s且solver文件3 PASS；schema漂移、duplicate key、路径穿越、超量/超大输入、NaN/Infinity、浮点溢出/下溢和超安全整数均fail closed；架构/可靠性/产品复审0 P0/P1/P2，fresh裁决`ALLOW_BOOTSTRAP_VERIFIER_CHECKPOINT`；subject/tree/7个blob远端回读PASS | `AUTOMATED_TEST / MULTI_AGENT_SIMULATED_REVIEW / BOOTSTRAP_VERIFIER_FOUNDATION_ONLY` | 在仓库外实现authority-owned purpose-specific broker/supervisor、固定registry与九个操作的真实process conformance；随后实现直接HTTPS capture并生成activation readiness，才可切ACTIVE | 正式external broker/IPC/conformance、live capture、Qwen/AMap、ACTIVE/anchor、四组件、sealed blind、Agent Gate、H1、生产和商业均`NOT_RUN`；本地ignored旧generic signer pyc不可导入且不进Git；本checkpoint不证明进程隔离、Provider事实、组织独立或真人证据 | 由独立authority custodian在候选仓库外实现并攻击测试固定purpose-specific broker/supervisor；先只使用受控测试key完成conformance，不调用Provider、不切ACTIVE |
| 2026-08-29 | 将治理偏航经验固化为产品主线硬约束：G01～G06采用CORE Gate，复杂authority链移至G07候选加固；P2分级、两轮复审上限和两次无产品进展强制转向已写入统一指导；G01下一动作回到Qwen/高德/真实卡片Gate | `eed251175fb66f8d268f8bb05d50d7b5b2bf3836` | Agent Gate/治理定向103 PASS；最终治理合同9 PASS；`git diff --check` PASS；subject/tree/18个文档变更远端readback PASS；`Product progress=NONE`，`Governance ratio=PURE_GOVERNANCE_ALLOWED_ONE` | `GOVERNANCE_CORRECTION / AUTOMATED_TEST / PRODUCT_MAINLINE_RESTORED` | Qwen exact binding、模型中立adapter、Max/Plus/Flash比较；AMap live地点映射；Agent A/B、ultra、sealed blind和Text Card Gate | 上一轮未提交的external verifier实现修改仍保留在工作区且未混入本checkpoint；authority HARDENED链为G07 `DEFERRED/NOT_RUN`，不阻断G01 | 安全自动发现Qwen exact binding，完成模型中立adapter并在dev/validation比较Max/Plus/Flash；不得先继续authority/broker加固 |
| 2026-08-29 | FULL文本链已有模型中立Qwen实现：可显式启用真实Qwen，推断城市以“暂按…”展示；模型超时或schema修复耗尽仍返回可编辑逐日卡片，不回退DeepSeek。G01～G06的评测入口不再读取authority、角色私钥、broker、registry或OCI | `74ea6c33ed8274f83c65c4ede6011875d6828cb7` | 账户目录实际回读20模型、三候选ID/context/structured output/价格字段；exact commit Flash dev长文本smoke在7秒内完成初始响应并启动唯一修复，总计2调用后得到可编辑`PARTIAL_RESULT`，脱敏回执SHA-256 `acee20c2ca3e7c6b72d7a52f32a4c0337dfa7f8728d2ffa6c27862c936d8d75a`；目标回归81 PASS、Ruff与diff-check PASS、密钥diff扫描PASS、subject/tree/关键blob远端回读PASS；`Product progress=MODEL`，`Governance ratio=23/37 changed files (62%, one-time alignment checkpoint)` | `DEV_LIVE_PROVIDER_OBSERVATION / AUTOMATED_TEST / AGENT_GATE_NOT_RUN` | Max/Plus/Flash跑完相同54 dev+18 validation并在仓库外保存预测；根据无答案指标修正prompt/schema；接通AMap POI与持久effect后才启动Agent A/B | 当前Flash初始响应约6.39秒/362输入token/974输出token，但destination schema校验失败，剩余deadline不足以完成修复；说明失败降级有效，不代表质量Gate通过或模型已冻结；HARDENED仍`DEFERRED/NOT_RUN` | 实现仓库外三模型prediction runner，先跑provider-independent schema/禁用文本/时延/token/费用指标；不读取blind、不启动审查Agent |
| 2026-08-29 | 同一72条长文本已完成Qwen三模型真实比较：Flash可在固定7秒内稳定生成可编辑逐日语义卡片；Plus/Max超时仍返回可编辑部分结果且不会调用DeepSeek | `be96ca2b503c908c110645e824f2a99c04ae63bf` | dev 54 + validation 18逐模型运行；Flash结构有效72/72、降级0、禁用文本原子项0、整句原子项0、P95 5134.483ms、54888输入/62567输出token、估算0.0610312元；Plus与Max均72/72 `DEADLINE_EXCEEDED`并受控降级，失败调用token/费用为`NOT_EXPOSED_BY_PROVIDER`；三份预测与summary SHA-256回读一致、runner error 0、blind reads 0；目标回归73 PASS、Ruff/diff-check PASS；`Product progress=EVAL_METRIC`，`Governance ratio=1/12 changed files (8%, checkpoint ledger only)` | `DEV_LIVE_PROVIDER_COMPARISON / PROVIDER_INDEPENDENT_METRICS / HUMAN_EVIDENCE_FALSE / AGENT_GATE_NOT_RUN` | 实现AMap POI 2.0保守解析与持久脱敏回执；生成dev/validation地点index后再启动两份隔离Agent reference和ultra裁决，完成正式语义/地点评分与唯一模型冻结 | Flash仅通过结构、禁用文本和时延前置检查，precision/recall/day/role及错城错类别尚未评分，不能宣称模型已冻结；Plus/Max在7秒合同下淘汰；真实地点/路线、浏览器长文本、Agent Gate和blind仍`NOT_RUN` | 接通FULL worker的AMap地点Provider，只允许原子PLANNED调用并先完成零错配反例与最小持久回执；不启动审查Agent或blind |
| 2026-08-29 | 登录用户的真实长文本已贯穿Flash、保守高德地点匹配、现有PostgreSQL活动表和首次异步walking/transit：得到Day 1～3共6张可编辑卡片；不确定地点保留待确认，地图失败不阻断卡片，编辑后不会自动重算路线 | `c48aea6c279e04e8870c4a81349324388e82a6f2` | exact commit真实链：首次进度21.487ms、首批卡片6773.675ms，6卡/4个自动地点/4份坐标与adcode回执，初始地图`LIMITED`、6个mode effect/2次真实路线调用、编辑后路线调用增量0，公共字段泄漏0；外部最小回执SHA-256 `fb99f32bf5ea6f56220805b026291b229dbb90aac574fad8439e8d2b587adf7f`；fresh PostgreSQL与地点/路线集成15 PASS，Qwen/AMap/API回归31 PASS，Ruff与frontend生产build PASS；`Product progress=PROVIDER`，`Governance ratio=0/23 changed files (0%)` | `DEV_LIVE_PROVIDER_PERSISTENCE / AUTOMATED_TEST / HUMAN_EVIDENCE_FALSE / AGENT_GATE_NOT_RUN` | 用现有应用表生成dev/validation高德index；真实页面长文本、六类编辑、刷新/并发/幂等/隐私删除与50次时延矩阵；随后才启动Agent A/B | 当前只有1次真实端到端样本，不代表P95；地图为保守`LIMITED`且未证明72条正式地点precision；模型尚未冻结，review与blind均`NOT_RUN`；HARDENED保持`DEFERRED/NOT_RUN` | 完成现有应用表CORE exporter和dev/validation地点index，再用实际页面跑真实长文本与浏览器矩阵；不读取blind、不启动审查Agent |
| 2026-08-29 | 实际结果页已用长攻略完成逐日卡片验证：普通用户可插入、替换、删除、同日排序、跨日移动并刷新恢复；两个隔离会话能识别并恢复并发冲突，相同编辑可安全幂等重放；卡片详情只显示友好地点信息和操作 | `4e94a1ca0329aea7060602c2f37a385618ec49bb` | in-app browser地点详情内部字段命中0；Playwright 4 PASS，覆盖匿名体验、登录FULL长文本、领取、六类编辑、刷新、越权、双会话200/409冲突恢复、幂等重放、原文/整程/账号旅行数据删除；后端Qwen/AMap/CORE评测定向79 PASS，Ruff、schema逐字生成回读、`git diff --check`与frontend生产build PASS；`Product progress=UI / EVAL_METRIC`，`Governance ratio=19/24 changed files (79%, existing CORE evaluation and receipt bindings; no authority expansion)` | `AUTOMATED_LOCAL_BROWSER / AUTOMATED_TEST / CORE_PROVIDER_EXPORTER_READY / HUMAN_EVIDENCE_FALSE / AGENT_GATE_NOT_RUN` | 从同一干净候选运行AMap dev/validation live index并校验应用表回读；完成至少50次真实链路时延；随后才启动两份隔离reference和ultra裁决 | 浏览器FULL链使用显式fixture worker，不替代此前单次live Qwen/AMap证据；AMap正式72条index、质量分数、50次P95、模型冻结、review与blind仍`NOT_RUN`；HARDENED继续`DEFERRED/NOT_RUN` | 推送并远端回读本checkpoint；在干净提交上运行应用表CORE AMap dev/validation exporter，再执行provider-independent校验和真实链路时延矩阵；不读取blind、不启动审查Agent |
| 2026-08-29 | 多城市长攻略现按每个明确深度城市保守搜索：仅当地点只在一个城市唯一匹配才生成已确认卡片，跨城歧义继续显示“地点待确认”；Qwen提示已要求完整保留REFERENCE/EXCLUDED等地点提及，CORE可从现有应用表导出脱敏模型回执 | `c4b601467abc880981f50117de3099f25cb332bf` | Qwen schema继续fail-closed且跨Pydantic版本逐字一致；多城市唯一采用、非深度城市不继承参考城市、非地点span不计活动角色反例通过；Qwen/理解/G01评测/Gate定向87 PASS，Ruff、compileall、schema生成逐字回读与`git diff --check` PASS；subject/tree远端回读PASS；`Product progress=MODEL / API / EVAL_METRIC`，`Governance ratio=11/14 changed files (79%, existing CORE evaluation/export bindings; no authority expansion)` | `AUTOMATED_TEST / CORE_QWEN_APPLICATION_TABLE_EXPORT_READY / HUMAN_EVIDENCE_FALSE / AGENT_GATE_NOT_RUN` | 在本checkpoint后的干净候选上重新运行Flash 54 dev+18 validation和AMap应用表index，先以既有已裁决参考做开发预评分；全部硬门槛通过后才冻结模型并重建正式reference/adjudication | 新提示尚未获得真实Qwen 72条结果；当前只证明代码与评测合同一致，不能宣称role macro-F1或模型已通过；review、blind和最终CORE均`NOT_RUN` | 推送并远端回读本checkpoint；安全加载既有环境凭据，运行Flash dev/validation真实预测并做开发预评分；不读取blind、不启动审查Agent |

## Auto-advance

- Required gate：`Text Card Gate + AGENT_GATE_PASS`；Next template：`TC-VNEXT-G02-MAP-STAY.md`；
- subject push/readback、耐久`AGENT_GATE_PASS`、clean tree、无Stop后，生成完整completed归档并在治理过渡commit原子激活G02；G02 binding必须等于Program记录；G01不推进authority generation；
- FUX-01、H1、公网、生产、商业和`main`不自动启动。

## Completion record

- Status：`PENDING`；
- Subject commits：S0 `7986214c1b236217ceb5d2d55f8cecc882e03f2b`，S0 receipt `1097d351b9c82d4f3276b6fd759c8d0f766e2119`，Demo `d6ab378a1f7d169efc94422e0b7611e3c8a49d0c`，compat `3106fe00b755e603bce5517f5ddea71e78e17214`，FULL文本 `2bdac7ce47c9d9ecc9c55c5e720908e0c238bf50`，commands `dacee589d9dbfb04a94ae7acc04a00946abc4710`，领取/隐私 `1ed7927813fcf197db519ccdad3b7472239eeb46`，029地图 `ccfe16e0d110ed0243576f327c1df93dcb8e61a0`，90条数据/评测合同 `9ebdb3b83633e20d4f281a7fe4fd758748aaf5e4`，地图正例矩阵 `1bee280eff6fc9fc607e44fe6f8c9feccf81b5e2`，Provider/预算降级 `80a9dc3835f1685f80a6d06513a0f82c22368091`，同镜像worker `cf9003c97e0c5b3350021c102183a0d39bbf269c`，PostgreSQL 30/120矩阵 `869bdaf282cbf7ab0e2b4f249426d49061bd5593`，最终自动验证 `e99ed68412d54978ac4f187ccb3e5e9b3659d57f`，Agent Gate BOOTSTRAP `0a79f82db0a22d027ee145265f483986640411a8`，外部authority verifier foundation `7d77ed01f133039fa5b0b97522480f7a79864773`，产品主线执行纠偏 `eed251175fb66f8d268f8bb05d50d7b5b2bf3836`，CORE/Qwen adapter `74ea6c33ed8274f83c65c4ede6011875d6828cb7`，三模型前置比较 `84fe36e39e80770ff48cec9ca6549f5cf0dd17e9`，AMap地点 `ccece46`，AMap路线 `926614e`，真实持久化链 `c48aea6c279e04e8870c4a81349324388e82a6f2`，多城市/语义召回/CORE Qwen exporter `c4b601467abc880981f50117de3099f25cb332bf`；Text Card Gate仍未完成；
- Remote branch：`origin/codex/trip-check-product-reset`；canonical integration：`origin/develop`；
- Verification / Evidence / Gate result：`LOCAL_AUTOMATED_REGRESSION_COMPLETE / DEV_LIVE_QWEN_AMAP_PERSISTENCE_OBSERVED / AGENT_GATE_NOT_RUN / TEXT_CARD_GATE_NOT_RUN`；
- H1 / production / commercial：`NOT_RUN / NOT_RUN / NOT_RUN`；
- `structurally_valid=true`：只继承G00蓝图结构，不代表G01通过；
- User-visible result：`http://localhost:3000`可匿名启动固定北京三日体验并登录领取；登录后可粘贴文本生成卡片、执行六类编辑、删除原文/单份行程/全部v3旅行数据；显式live配置下，真实长文本已生成3天6卡并把4个高德精确地点及坐标写入现有活动表，首批卡片后异步执行walking/transit，编辑后只提示路线需要更新且不自动调用路线Provider。该结果是本地开发live证据，不是真人、公网或生产证据；
- Remaining risks：旧提示的Flash 72条结果已揭示非PLANNED角色召回不足；新提示尚未完成真实72条重跑，模型仍未冻结；Plus/Max在固定7秒内均超时淘汰；正式precision/recall/day/role、多城市地点分母、三角色review、独立sealed blind与最终Text Card Gate仍未运行；外部authority verifier foundation保留为G07候选加固实验，broker/supervisor/capture/ACTIVE均`DEFERRED / NOT_RUN`且不阻断G01；
- Next autonomous action：在干净候选上运行Flash dev/validation真实预测与AMap应用表index，使用已冻结的开发参考做预评分；达到全部硬门槛后再冻结唯一模型并启动新的正式A/B与ultra，不读取blind；
- Goal archived：`NO`；
- Next activated：`NO`；
- Promotion decision：`NOT_REQUESTED`。

## Stop conditions

- 需要降低严重错配为0的门禁；
- 需要新增付费账号或未批准Provider；
- 必须修改sealed blind/oracle；
- 无法保持旧API可读；
- 需要降低严重错配、整句/URL地点或隐私门禁。
