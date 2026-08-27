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
- Latest delivered checkpoint：Demo `d6ab378a1f7d169efc94422e0b7611e3c8a49d0c` + compatibility `3106fe00b755e603bce5517f5ddea71e78e17214`，远端hash/subject/tree/file readback `PASS`
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
- current delivered subject：`origin/codex/trip-check-product-reset@3106fe00b755e603bce5517f5ddea71e78e17214`，tree `d82cd0b8de3e7530f23d1cf677f70d474fcd981a`，远端文件readback与clean-tree `PASS`；
- activation transition：`f3b5f3e0c36ff3977f826bd82a83b3150a2e97ac`，远端readback `PASS`；
- Blueprint subject：`3fb3d0566c5742ecf4fac4179021ee538ef5b516`，远端readback `PASS`；
- 旧OpenAPI兼容基线：99 paths / 106 operations，SHA-256 `0a616cf711b260a232d20aca80d6904743327ff9dcbc2808356c62066fc55a81`；现场旧容器94 paths、v3为0，缺微信登录1条和截图上传批次4条，登记为`LEGACY_CONTAINER_DRIFT`；
- Qwen live lane：`NOT_READY`（key与通用兼容URL存在；账号region/workspace/exact model ID/价格绑定未确认；当前runtime仍为DeepSeek）；
- AMap live persistence：`BLOCKED_PENDING_WRITTEN_PERMISSION`（凭据存在但没有持久化书面许可；仅允许fixture且本切片不发起live调用）；
- 历史Candidate：`HISTORICAL_BINDING_INVALID / FROZEN`；10/10数据、schema和generator绑定有效，validator/scorer/gate绑定失效；不得修改manifest、blind、oracle或冻结证据；
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

## Auto-advance

- Required gate：`Text Card Gate`；Next template：`TC-VNEXT-G02-MAP-STAY.md`；
- subject push/readback、Gate PASS、clean tree、无Stop后，生成完整completed归档并在治理过渡commit原子激活G02；
- FUX-01、H1、公网、生产、商业和`main`不自动启动。

## Completion record

- Status：`PENDING`；
- Subject commits：S0 `7986214c1b236217ceb5d2d55f8cecc882e03f2b`，S0 receipt `1097d351b9c82d4f3276b6fd759c8d0f766e2119`，Demo `d6ab378a1f7d169efc94422e0b7611e3c8a49d0c`，compat `3106fe00b755e603bce5517f5ddea71e78e17214`；FULL与Text Card Gate仍未完成；
- Remote branch：`origin/codex/trip-check-product-reset`；canonical integration：`origin/develop`；
- Verification / Evidence / Gate result：`FIXTURE_DEMO_LOCAL_AUTOMATED_PASS / FULL_NOT_RUN / TEXT_CARD_GATE_PENDING`；
- `structurally_valid=true`：只继承G00蓝图结构，不代表G01通过；
- User-visible result：`http://localhost:3000`可匿名启动固定北京三日体验，真实持久v3链返回六张卡并支持刷新恢复；这不是FULL文本、真人、公网或生产证据；
- Remaining risks：Qwen live lane未就绪；AMap持久化等待书面许可；FULL登录文本、commands/删除/claim、029初次地图任务、90条数据和Text Card Gate尚未完成；
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
