# IN_PROGRESS GOAL：V0.1 可信文本卡片

Goal ID: TC-VNEXT-G01-TEXT-CARDS
Status: IN_PROGRESS
Goal type: PRODUCT_VERTICAL_SLICE

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
- Latest delivered checkpoint：fresh PostgreSQL 30份地图job/120条可用边持久化矩阵 `869bdaf282cbf7ab0e2b4f249426d49061bd5593`，tree `b5c33adeba6541911a31ef89a11090b2721a8355`；远端hash/subject/tree/file readback `PASS`
- Activation：`TC-BP-G00-BLUEPRINT`已归档且Blueprint Gate为`BLUEPRINT_READY`
- Approved by / at：User / 2026-08-27
- Required gate：`Text Card Gate`
- Next Goal：`TC-VNEXT-G02-MAP-STAY`

## Dependencies

- 唯一激活依赖`TC-BP-G00-BLUEPRINT`已归档且Blueprint Gate为`BLUEPRINT_READY`；本Goal已按Program置为`APPROVED`。
- 首个preflight读取已固化的activation commit并填写现场baseline，同时readback Qwen账号、region、endpoint、exact model ID、pricing/privacy条款和高德POI/route最小持久化许可；缺失lane标记`NOT_READY`，不阻止schema、UI、fixture和其他安全独立切片。
- 到真实模型/Provider Gate前仍缺失时按HITL请求最小动作；不得用fixture冒充live准入，也不得留下0个active Goal。

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
- G00所列Qwen模型面板在账号/区域/exact binding readback后做dev/validation实验；
- 高德POI与walking/transit只在许可readback及现有无增量费用开发范围使用；
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
- H1、公网、生产、`main`。

## Dataset

- 90条：54 dev / 18 validation / 18 sealed blind；
- 三城60、其他城市15、对抗15；
- 旧根目录`tests/`中的19条未完成旅行文本已按项目所有者要求删除，不再作为regression、oracle或当前数据源；G01必须从本合同的90条受治理数据重新建立可复现基线；
- 双人独立标注、冲突裁决、family隔离；
- validation与blind各至少65个gold executable mentions；结合coverage≥80%仍须直接验证auto-selected分母≥50，不能只按gold数量推断；
- blind标签由独立custodian保管；只在dev/validation选模，唯一候选冻结后blind一次。

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

## Authority

- `AGENTS.md`、Charter、Spec、v3 API、Architecture；
- Program、Roadmap、Release Gates、Provider Admission、Risk Register；
- ADR-007、ADR-008、ADR-009、ADR-012。

## Baseline

- branch/upstream：`codex/trip-check-product-reset` / `origin/codex/trip-check-product-reset`；
- canonical integration subject：`origin/develop@d114d6a1e9a06b1e26fb62519710e35d50300d70`，远端readback `PASS`；
- implementation baseline：`origin/codex/trip-check-product-reset@d114d6a1e9a06b1e26fb62519710e35d50300d70`；写入前`ls-remote`、HEAD与clean-tree一致；
- current delivered subject：`origin/codex/trip-check-product-reset@869bdaf282cbf7ab0e2b4f249426d49061bd5593`，tree `b5c33adeba6541911a31ef89a11090b2721a8355`，远端文件readback `PASS`；
- activation transition：`f3b5f3e0c36ff3977f826bd82a83b3150a2e97ac`，远端readback `PASS`；
- Blueprint subject：`3fb3d0566c5742ecf4fac4179021ee538ef5b516`，远端readback `PASS`；
- 旧OpenAPI兼容基线：99 paths / 106 operations，SHA-256 `0a616cf711b260a232d20aca80d6904743327ff9dcbc2808356c62066fc55a81`；现场旧容器94 paths、v3为0，缺微信登录1条和截图上传批次4条，登记为`LEGACY_CONTAINER_DRIFT`；
- 当前生成OpenAPI：116 paths，其中v3为11；与99路径冻结快照物理隔离，新增17路径全部机器归类且旧路径/方法零缺失；`UserFacingTripResult.status`已按冻结预算合同包含可编辑`LIMITED`；
- Qwen live lane：`NOT_READY`（key与通用兼容URL存在；账号region/workspace/exact model ID/价格绑定未确认；当前runtime仍为DeepSeek）；
- AMap live persistence：`BLOCKED_PENDING_WRITTEN_PERMISSION`（凭据存在但没有持久化书面许可；仅允许fixture且本切片不发起live调用）；
- 历史Candidate：`HISTORICAL_BINDING_INVALID / FROZEN`；10/10数据、schema和generator绑定有效，validator/scorer/gate绑定失效；不得修改manifest、blind、oracle或冻结证据；
- G01 Text Card数据：独立90条输入已生成并字节绑定，`54 dev / 18 validation / 18 frozen_blind`、`60 DEEP_CITY / 15 OTHER_CITY / 15 ADVERSARIAL`、30个family各A/B/C且不跨split；仓库内human label/gold/oracle为0；
- G01标注与评分：双人独立标注、第三人冲突裁决、逐字span、Provider receipt、仓库外保管和validation最小gold分母均由机器合同fail closed；通用开发scorer拒绝读取blind；当前状态为`HITL_PENDING / TEXT_CARD_GATE_NOT_RUN`；
- 本地确定性proposal baseline：只读取dev/validation，dev `54 cases / 80 eligible / 53 auto-matched`，validation `18 / 5 / 5`，external calls、human labels和blind reads均为0；没有gold故质量`NOT_SCORED`，且validation auto-selected分母5明确低于门槛50；
- 地图正例fixture：北京/上海/杭州各10份、每份5个已映射地点与4条相邻边，共30份行程/120条唯一有向边；真实`MapRenderWorker → MapRenderer`受控矩阵生成30个READY snapshot、walking/transit各120个可用mode fact、可用覆盖100%、逻辑重复请求0、external call 0、worker→snapshot P95 `0.45ms`；只计`CONTROLLED_FIXTURE`子门禁，live高德和完整Gate仍为`NOT_RUN`；
- Provider故障与预算：只有显式脱敏的typed unavailable会被降级，普通代码/schema异常不吞掉；Qwen候选故障用本地确定性语义返回`PARTIAL_RESULT`且不使用DeepSeek，地点故障保留六张可确认卡，单一路线模式故障隔离到该模式；81个可执行活动保留81张卡、只发起前80次地点解析并返回`LIMITED`，没有静默截断；以上均为test double/fixture，不是live调用；
- PostgreSQL地图正例矩阵：fresh database应用现有migration后，30份FULL理解结果各原子创建初次地图job；独立worker持久化30个READY job/snapshot、120条selected edge、240个AVAILABLE mode fact与240个Provider effect receipt，external calls与重复逻辑请求均为0，P95门槛断言≤15秒，旧`rooms`表仍存在；数据库结束后安全删除，仅证明受控fixture持久链；
- G00治理结构验证`structurally_valid=true`；这只证明蓝图结构，不证明V0.1产品能力；
- 历史Intake/Candidate只作资产基线，不是G01 PASS，不得因此宣称 `V1_CANDIDATE_READY`；
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

新账号/费用、许可无法满足、未预批准schema/migration/依赖、sealed blind/oracle变化、H1/公网/生产/`main`或删除旧数据时请求人工批准；普通实现/测试失败不请求用户诊断。

## Checkpoint ledger

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

## Auto-advance

- Required gate：`Text Card Gate`；Next template：`TC-VNEXT-G02-MAP-STAY.md`；
- subject push/readback、Gate PASS、clean tree、无Stop后，生成完整completed归档并在治理过渡commit原子激活G02；
- FUX-01、H1、公网、生产、商业和`main`不自动启动。

## Completion record

- Status：`PENDING`；
- Subject commits：S0 `7986214c1b236217ceb5d2d55f8cecc882e03f2b`，S0 receipt `1097d351b9c82d4f3276b6fd759c8d0f766e2119`，Demo `d6ab378a1f7d169efc94422e0b7611e3c8a49d0c`，compat `3106fe00b755e603bce5517f5ddea71e78e17214`，FULL文本 `2bdac7ce47c9d9ecc9c55c5e720908e0c238bf50`，commands `dacee589d9dbfb04a94ae7acc04a00946abc4710`，领取/隐私 `1ed7927813fcf197db519ccdad3b7472239eeb46`，029地图 `ccfe16e0d110ed0243576f327c1df93dcb8e61a0`，90条数据/评测合同 `9ebdb3b83633e20d4f281a7fe4fd758748aaf5e4`，地图正例矩阵 `1bee280eff6fc9fc607e44fe6f8c9feccf81b5e2`，Provider/预算降级 `80a9dc3835f1685f80a6d06513a0f82c22368091`，同镜像worker `cf9003c97e0c5b3350021c102183a0d39bbf269c`，PostgreSQL 30/120矩阵 `869bdaf282cbf7ab0e2b4f249426d49061bd5593`；Text Card Gate仍未完成；
- Remote branch：`origin/codex/trip-check-product-reset`；canonical integration：`origin/develop`；
- Verification / Evidence / Gate result：`FIXTURE_DEMO_AND_CONSERVATIVE_FULL_LOCAL_AUTOMATED_PASS / LIVE_PROVIDER_NOT_RUN / TEXT_CARD_GATE_PENDING`；
- `structurally_valid=true`：只继承G00蓝图结构，不代表G01通过；
- User-visible result：`http://localhost:3000`可匿名启动固定北京三日体验并登录领取；登录后可粘贴文本生成卡片、执行六类编辑、删除原文/单份行程/全部v3旅行数据；首批卡片后后台自动生成walking/transit快照，编辑后诚实提示路线尚未更新。当前地点和路线均为fixture证据；90条数据只是内部受治理评测输入，不会进入用户页面，也不是真人、公网或生产证据；
- Remaining risks：Qwen live lane未就绪；AMap持久化等待书面许可；地图fixture与PostgreSQL 30/120、离线故障降级已通过，但live覆盖、真实模型调用/修复预算和完整runtime Gate仍未完成；90条输入已完成但双人真人标注/裁决、外部blind custodian与Text Card Gate仍未完成；
- Goal archived：`NO`；
- Next activated：`NO`；
- Promotion decision：`NOT_REQUESTED`。

## Stop conditions

- 需要降低严重错配为0的门禁；
- Qwen账号/区域/隐私无法满足；
- 需要新增付费账号或未批准Provider；
- 必须修改sealed blind/oracle；
- 无法保持旧API可读；
- 两个不同切片仍不能阻止整句成为地点。
