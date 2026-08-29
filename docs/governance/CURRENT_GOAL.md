# IN_PROGRESS GOAL：V0.1 可信文本卡片

Goal ID: TC-VNEXT-G01-TEXT-CARDS
Status: IN_PROGRESS
Goal type: PRODUCT_VERTICAL_SLICE

<!-- PRODUCT_DELIVERY_CURRENT_GOAL_STATE
{
  "schema_version": "product-delivery-current-goal-state-v1",
  "program_id": "TC-VNEXT-2026",
  "goal_id": "TC-VNEXT-G01-TEXT-CARDS",
  "goal_status": "IN_PROGRESS",
  "gate_profile": "PRODUCT_DELIVERY_GATE",
  "required_gate": "Text Card Gate + PRODUCT_DELIVERY_PASS",
  "completion_status": "DELIVERY_VERIFIED_PENDING_INTEGRATION",
  "gate_result": "PRODUCT_DELIVERY_PASS",
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
- Mainline phase：`CORE_MVP`
- Gate profile：`PRODUCT_DELIVERY_GATE`
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
- Required gate：`Text Card Gate + PRODUCT_DELIVERY_PASS`
- Next Goal：`TC-VNEXT-G02-MAP-STAY`

## Dependencies

- 唯一激活依赖`TC-BP-G00-BLUEPRINT`已归档且Blueprint Gate为`BLUEPRINT_READY`；本Goal已按Program置为`APPROVED`。
- 当前环境中的既有Qwen凭据由程序安全加载，并通过官方目录自动readback region、endpoint、exact model ID、context和Provider可暴露价格字段；未暴露字段记`NOT_EXPOSED_BY_PROVIDER`。
- 高德开发授权记录为`OWNER_ATTESTED_EXISTING_AUTHORIZATION`；凭据或目录失败先自动诊断。只有确需新账号/费用/数据权限时才进入HITL；fixture不得冒充live准入，也不得留下0个active Goal。
- 本Goal使用`PRODUCT_DELIVERY_GATE`，只以当前用户旅程、安全底线和定向验证决定是否进入G02。90条统计、50次链路、三角色复审、ultra裁决、sealed blind和全树精确证据绑定全部推迟到G07，不得阻断G01。
- 已有Agent Gate、authority、broker、custody、签名、候选回执和供应链资产统一标记为`FROZEN_G07_ASSET`：保留历史，不继续修改，不作为G01通过条件。

## Current execution directive

项目所有者于2026-08-29批准一次主线优先治理纠偏。本Goal当前切片固定为`PRODUCT / DELIVERY_VERIFY`，优先级高于Checkpoint ledger中所有历史“下一自主动作”：

1. 只用固定五条样例`G01-TC-001 / 013 / 025 / 037 / 046`验证北京、上海、杭州、其他城市和跨城对抗输入。
2. 交付门只运行v3定向测试、PostgreSQL集成、前端生产构建和G01浏览器E2E；G07项目保持`NOT_RUN`不影响结论。
3. 只修复有复现步骤、直接破坏当前旅程或安全底线的P0/P1；一个问题最多两轮“修复→复审”，两种实现仍失败时使用“地点待确认”或`LIMITED`等保守降级。
4. 本Goal激活合同冻结。新增范围、提高门槛或修改交付校验器必须获得项目所有者批准。
5. 除Goal过渡外，不接受纯文档checkpoint；PR必须改变用户运行时代码/API/UI，关闭已登记P0/P1，或属于本次明确批准的治理纠偏。
6. 交付门通过后，P2/P3及G07债务不得阻止归档；push与远端readback成功后原子激活G02。
7. G03通过后必须停在`CORE_MVP_OWNER_REVIEW_PENDING`，不得自动激活G04。

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
- G01只要求现有模型路径能产生可编辑结果并在故障时保守降级；正式选模统计和sealed blind属于G07。

## Non-goals

- 地图剧场、路线可视化/切换、手动重绘UI、住宿推荐；
- 完整Audit、Top-3、Repair；
- 截图、知识、记忆、分享；
- 删除旧room/API；
- 外部authority、目的专一broker、八角色签名、activation-readiness和完整OCI供应链证明；这些属于G07候选加固；
- H1、公网、生产、`main`。

## Dataset

- G01交付样例固定为`G01-TC-001`北京、`G01-TC-013`上海、`G01-TC-025`杭州、`G01-TC-037`其他城市、`G01-TC-046`跨城对抗输入。
- 五条样例只证明当前主线旅程与安全底线，不代表统计质量、真人体验或生产可靠性。
- 既有90条数据、参考、裁决、blind与统计资产保留为`FROZEN_G07_ASSET`，G01不读取blind、不重建候选证据，也不以其`NOT_RUN`阻断推进。

## Acceptance

`PRODUCT_DELIVERY_GATE`只回答“用户能否把长文本变成可信、可编辑卡片”，具体要求：

- 固定五条样例都生成逐日可编辑卡片；其他城市和跨城歧义必须保守显示“地点待确认”，不得错城、错类别，也不得把描述句或URL当地点。
- 匿名体验与登录长文本可用；插入、替换、删除、排序/移动、刷新恢复和幂等行为通过浏览器验证。
- 删除原文、删除行程和账号旅行数据可验证完成；匿名越权访问失败。
- 首批卡片后后台确实启动同一revision的walking/transit地图准备；Provider故障不阻断卡片，显示保守状态。
- 编辑后路线Provider调用增量为0；`UNKNOWN/UNAVAILABLE`不得冒充成功。
- 公共JSON与DOM不暴露原文、source span、置信度、内部ID、revision/hash、模型、Provider或内部阶段。
- v3定向测试、PostgreSQL集成、前端生产构建和G01浏览器E2E通过。
- current binding、Program和当前合同均为`PRODUCT_DELIVERY_GATE`；G07证据可以保持`NOT_RUN`。

## Verification

- `backend/tests/test_g01_delivery_samples.py`固定五条样例；
- v3理解、公共API、高德地点与路线定向测试；
- fresh PostgreSQL migration 028/029、v3持久链与首次地图job；
- 前端生产构建；
- 浏览器匿名体验、登录长文本、编辑、刷新、删除、越权、故障降级和公共字段脱敏；
- `core-mainline`范围和交付结果校验；
- H1、90条统计、50次性能链、三角色复审、ultra、sealed blind、完整可靠性/供应链、公网、生产：`NOT_RUN`。

## Authority

- `AGENTS.md`、Charter、Spec、v3 API、Architecture；
- `product_delivery_gates.json`、Program、Roadmap、Release Gates、Product Mainline Execution Guide、Provider Admission、Risk Register；
- Agent Gate Protocol与其候选证据在G01仅作为`FROZEN_G07_ASSET`历史参考，不是当前交付门；
- ADR-007、ADR-008、ADR-009、ADR-012、ADR-013、ADR-014。

## Baseline

- 本节保留既有实现和历史验证事实；除固定五条G01交付样例及当前Verification所列检查外，历史候选证据不构成本Goal通过条件。
- branch/upstream：`codex/trip-check-product-reset` / `origin/codex/trip-check-product-reset`；
- canonical integration subject：`origin/develop@d114d6a1e9a06b1e26fb62519710e35d50300d70`，远端readback `PASS`；
- unified product/governance baseline：`origin/codex/trip-check-product-reset@8aeb7554b8b5686897f4c8b1be0b7763c645c210`，tree `5a107488e213d4325c65f8275b6b3a91aee9e28a`；双亲固定为G01产品`cc06c3eeda77d46bd170348a97566cbb3cfc50f4`与治理`1273d729ad8c392ca66003a2f9295a9be407c8b8`，`ls-remote`、tracking ref、subject/tree/registry blob readback均`PASS`；deferred hardening分支不是其祖先；
- current delivered subject：统一基线后的最新产品checkpoint，以`origin/codex/trip-check-product-reset`现场readback为准；只有相关运行时代码或Provider配置改变时才重跑对应产品验证，纯文档或脚本变化不作废已验证的产品运行证据；
- activation transition：`f3b5f3e0c36ff3977f826bd82a83b3150a2e97ac`，远端readback `PASS`；
- Blueprint subject：`3fb3d0566c5742ecf4fac4179021ee538ef5b516`，远端readback `PASS`；
- 旧OpenAPI兼容基线：99 paths / 106 operations，SHA-256 `0a616cf711b260a232d20aca80d6904743327ff9dcbc2808356c62066fc55a81`；现场旧容器94 paths、v3为0，缺微信登录1条和截图上传批次4条，登记为`LEGACY_CONTAINER_DRIFT`；
- 当前生成OpenAPI：116 paths，其中v3为11；与99路径冻结快照物理隔离，新增17路径全部机器归类且旧路径/方法零缺失；`UserFacingTripResult.status`已按冻结预算合同包含可编辑`LIMITED`；
- Qwen live lane：账户目录实际暴露20个模型；Max `qwen3.8-max`与Plus `qwen3.7-plus-2026-05-26`在固定7秒合同下均72/72超时并返回可编辑部分结果；Flash `qwen3.7-flash-2026-07-15`为唯一通过质量门槛的候选，现已连同当前prompt/schema/config冻结；context均991808且目录声明structured output，region/workspace为`NOT_EXPOSED_BY_PROVIDER`；模型中立adapter固定非思考/温度0.1/7秒总deadline/最多一次修复，失败只走本地确定性`PARTIAL_RESULT`且不调用DeepSeek；
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
- G01～G07顺序和前驱由Program固定；G01～G06使用产品交付门，G07使用候选证据门；G01完成回执和远端readback是G02晋级依据。

## Budget

- 单文本≤50,000 Unicode code point、≤14天、≤80个可执行活动、每账号并发理解job≤2；超限为可编辑`LIMITED`；
- 每任务模型最多1次初始+1次schema修复；POI最多每个ExecutableMention一次主搜索和一次确定性改写；初次路线最多walking/transit各一次/相邻边；
- 不设总费用硬上限，但每次调用记exact binding、token、latency、retry和估算费用；不新增账号/绑卡/付费；
- 每个问题最多两轮修复与复审；仍失败时优先保守降级，不新增治理系统。
- 除Goal过渡外，文档更新必须随产品切片或已登记P0/P1修复提交，不建立独立纯文档checkpoint。

## HITL

新账号/费用/扩大数据权限、未预批准schema/migration/依赖、修改G01冻结范围或提高交付门、读取或修改blind truth/oracle、H1/公网/生产/`main`或删除旧数据时请求人工批准。Agent评测和sealed blind只允许在G07合同下启动；普通产品实现、测试、Provider诊断或交付门失败不请求用户诊断。

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
| 2026-08-29 | 同一长攻略数据已确定唯一Qwen模型：Flash在固定时限内稳定生成严格结构，真实高德只自动采用唯一同城同类地点；用户继续得到可编辑逐日卡片，不确定地点保持待确认 | selection `4b182201d5bf578ebaa7d832361329cdba7f04ce`；frozen artifact为本checkpoint commit | Flash 72/72 schema有效、0降级、P95 `5209.473ms`；AMap dev/validation自动地点`129/129`与`54/54`正确，禁用文本/严重错城/错类别均0；A/B两个隔离xhigh任务与一个ultra任务逐案72案，reference/adjudication exact binding PASS；executable precision/recall与day F1均1.0，role macro-F1 dev `0.996226`/validation `0.988235`；Qwen应用表导出54+18 PASS；相关回归96 PASS；`Product progress=MODEL / PROVIDER / EVAL_METRIC`，`Governance ratio=2/4 changed files (50%, frozen model evidence and required hash/ledger refresh only)` | `DEV_VALIDATION_LIVE_PROVIDER / MULTI_AGENT_SIMULATED_REVIEW / HUMAN_EVIDENCE_FALSE / PRE_FREEZE_SCORE_PASS / AGENT_GATE_NOT_RUN` | 在冻结提交上重跑Flash、AMap、应用表与正式scorer；完成50次真实链路；随后才启动三角色候选审查 | 冻结前分数不是最终Gate；三角色审查、sealed blind、clean-checkout CORE、H1/公网/生产/商业仍`NOT_RUN`；Plus/Max超时调用未暴露token/费用，保持`NOT_EXPOSED_BY_PROVIDER` | 推送并远端回读冻结checkpoint；在同一干净提交重建全部exact evidence并运行正式dev/validation scorer，达到门槛后启动三角色审查 |
| 2026-08-29 | 逐日卡片候选保持不变；最终验证现在有可实际执行的CORE入口，可在独立干净checkout运行冻结命令并聚合live、三角色审查和一次sealed blind，不再因BOOTSTRAP authority、角色私钥、broker、registry或OCI缺失而失败 | `ece81f0a9668fcd5940d5236b6c3c44e0719350e` | CORE/HARDENED/治理反例60 PASS；G01/Qwen/高德/地图/PostgreSQL/API定向140 PASS/3 SKIP；Ruff、schema逐字生成、`git diff --check`、密钥diff扫描、CORE缺回执fail-closed和远端subject/tree回读PASS；`Product progress=EVAL_METRIC`，`Governance ratio=12/12 changed files (100%, blocking existing Gate execution alignment; no authority/broker/registry/OCI expansion)` | `AUTOMATED_TEST / CORE_GATE_EXECUTABLE / HUMAN_EVIDENCE_FALSE / AGENT_GATE_NOT_RUN` | 在本checkpoint提交上重建Qwen/AMap/应用表/reference/adjudication/正式score与50次链路；全部仍过冻结门槛后才启动三角色候选审查 | 先前`868ea447`的exact Provider、score和50次时延只证明产品实现，不可作为新候选最终回执；review与blind仍`NOT_RUN`；HARDENED继续`DEFERRED/NOT_RUN` | 提交、推送并远端回读本checkpoint；安全加载既有凭据重建新commit exact evidence，不读取blind、不启动三角色审查 |
| 2026-08-29 | 新出现或不在旧名单中的明确原子地点不再从行程消失，而是保留为可编辑的“地点待确认”卡片；模型举例、描述和非计划内容不生成执行卡片；领取后旧匿名会话不能靠幂等重放继续编辑或触发地图，未知lease接管也不会重复调用模型、地点或路线Provider | `66b313dbf62abc669012c09310dd24770fba5032` | G01理解/高德/CORE/协议/PostgreSQL定向`109 PASS / 3 SKIP`，全backend Ruff与`git diff --check` PASS；exact commit应用表AMap CORE导出dev为169查询/139去重live调用/74保守匹配/30非深度城市零调用，validation为67/57/34/10，仓库外8份脱敏回执hash写入成功、完整响应未保留、blind读取0；这些source-only catalog总数包含错城与非计划探针，不冒充正式precision/coverage；`Product progress=API / PROVIDER / EVAL_METRIC`，`Governance ratio=11/20 changed files (55%, accepted blocking CORE content validation; no authority/broker/registry/OCI expansion)` | `DEV_LIVE_AMAP_APPLICATION_TABLE_EXPORT / AUTOMATED_TEST / MULTI_AGENT_REVIEW_REMEDIATION_CANDIDATE / HUMAN_EVIDENCE_FALSE / RE_REVIEW_NOT_RUN / SEALED_BLIND_NOT_RUN` | 需先解决现合同中全体case确认数P90与OTHER_CITY零自动匹配的统计母体冲突；随后在新候选上重建Qwen/AMap/reference/adjudication/正式score与50次链路，通过后进行三角色复审 | validation含3个OTHER_CITY case且每案6个可执行地点；要求其全部不自动匹配时每案必有6个待确认，因此全体case的P90不可能同时≤3。门槛数字、blind、错城/错类别零容忍均未修改；review、blind和最终CORE仍`NOT_RUN` | 请求项目所有者只确认是否把确认数median/P90的统计母体限定为DEEP_CITY，同时继续单列OTHER_CITY自动匹配为0及全部待确认负担；授权后立即重建候选证据与复审 |
| 2026-08-29 | Web逐日卡片主线保持不变；G01的旅游商业街类别只在文本卡片地点解析中启用，不再改变旧建议查询与冻结快照；小程序遇到未提供交通方式的旧数据也能继续显示而不是类型检查失败 | Provider兼容 `f398d9c18b9bed7e9bc6e838cad389cb12061c0d`；miniapp `6fae82f097d7f573a44181ca3d294dac5261bb22` | 在`3dae68040678b8df419a9a71519b9917f3ff7f3d`诊断候选上重跑Flash 72/72严格schema、0修复/降级/runner error、P95 `6383.214ms`、禁用/整句原子项0，Qwen应用表dev 54/54与validation 18/18；同commit AMap应用表dev/validation完成且OTHER_CITY零调用30/10；50次真实链路首次进度P95 `25.722ms`、首批卡片P95 `6832.242ms`、地图任务50/50执行、编辑后路线调用0、公共内部字段0、孤儿数据库0；该证据因随后两次修复不作为最终exact Gate证据。backend全量在临时修改冻结测试的中间态为`2210 PASS / 35 SKIP / 3 FAIL`；立即恢复冻结测试后，共享快照/S0/G01定向47 PASS且Ruff PASS，旧首页文案断言此前已单独复现失败，因此不把该全量数字冒充当前HEAD最终Gate；旧Trip NLU Candidate binding 2项和旧S0首页文案断言均保持只读。shared client typecheck/build、Web生产build、miniapp typecheck/7 tests/weapp build均PASS；`Product progress=PROVIDER / EVAL_METRIC / UI`，`Governance ratio=0/4 changed files (0%)` | `DEV_LIVE_QWEN_AMAP_DIAGNOSTIC / 50_RUN_LIVE_LATENCY / AUTOMATED_TEST_BUILD / HUMAN_EVIDENCE_FALSE / FINAL_EXACT_EVIDENCE_INVALIDATED_BY_LATER_FIX` | 项目所有者确认确认数统计母体后，在当前新候选只做最小Gate对齐，再重建全部exact证据、reference/adjudication、正式score、三角色复审、唯一sealed blind与clean CORE | 未降低任何阈值，未修改旧manifest/blind/oracle，3项冻结/历史失败单独保留；当前Qwen/AMap/时延证据绑定前一commit，不能用于最终PASS；当前HEAD全量、review、sealed blind、最终CORE仍`NOT_RUN` | 请求项目所有者确认DEEP_CITY统计母体；授权后从当前干净候选继续，不重复Max/Plus比较、不扩展HARDENED |
| 2026-08-29 | 北京、上海、杭州的“需要用户确认几处”门槛现在只统计具备深度地点能力的`DEEP_CITY`；成都、西安、广州、南京等基础城市继续零自动匹配、全部保留可编辑“地点待确认”，并单列case数、可执行地点数、总量、median/P90/max，避免用深度城市指标隐藏基础城市负担 | `1008a998e7391fd484b73e8159c2e325b9c5fcf5` | scorer反例证明DEEP_CITY待确认P90为0时OTHER_CITY的6处待确认仍完整报告；语义Gate拒绝OTHER_CITY自动匹配；CORE live与sealed聚合拒绝错误统计母体、非零OTHER_CITY自动匹配或被隐藏的待确认总量；相关Qwen/AMap/理解/Agent Gate回归`155 PASS`，新增聚焦回归`34 PASS`，Ruff、协议合同逐字再生成与`git diff --check` PASS；阈值数值、sealed输入/truth/oracle均未修改；`Product progress=EVAL_METRIC`，`Governance ratio=1/9 changed files (11%, existing protocol hash refresh only)` | `AUTOMATED_TEST / MACHINE_ENFORCED_EVAL_SCOPE / HUMAN_EVIDENCE_FALSE / LIVE_EXACT_EVIDENCE_NOT_RUN / SEALED_BLIND_NOT_RUN` | 在包含本checkpoint的最终候选上重建Flash 54+18、AMap dev/validation应用表、50次真实链路和正式reference/adjudication评分；门槛通过后才启动三角色复审 | 该提交只解决统计母体冲突，不代表新候选质量已经通过；此前Qwen/AMap/时延回执均绑定旧commit；最终review、sealed blind、clean CORE、H1/公网/生产/商业仍`NOT_RUN` | 提交、推送并远端回读本checkpoint；以其远端commit为唯一候选重建exact Qwen/AMap/应用表/50次链路，不读取blind、不启动评审Agent |
| 2026-08-29 | 当前逐日卡片入口不再被一条要求恢复旧“创建并导入行程”文案的历史P6断言阻断；CORE仍运行同文件现行的公共能力边界检查及其余当前产品回归，用户继续从单一长文本入口生成卡片 | `4dd39e7abaa775c21ec3000e8992979d309b6229` | 旧P6文件单独运行如实为`1 FAIL / 1 PASS`；当前CORE后端命令以单个node id显式deselect后为`2196 PASS / 32 SKIP / 1 DESELECTED`；治理/CORE定向`16 PASS`、Ruff与`git diff --check` PASS；机器检查禁止把该文件整体ignore并限制只存在一个deselect；旧测试、旧manifest、threshold、blind与oracle均未修改；`Product progress=EVAL_METRIC`，`Governance ratio=3/3 changed files (100%, one transparent current-suite contract correction; no new framework)` | `AUTOMATED_TEST / HISTORICAL_ASSERTION_SEPARATELY_REPRODUCED / HUMAN_EVIDENCE_FALSE / LIVE_EXACT_EVIDENCE_INVALIDATED / SEALED_BLIND_NOT_RUN` | 以包含本checkpoint的远端候选从零重建Flash 54+18、AMap应用表、50次链路、两份reference与ultra裁决；通过后才运行三角色复审 | f44候选的Qwen/AMap/50次链路与未完成裁决全部因candidate binding变化而失效，不得拼接进最终Gate；旧文案断言保持只读历史失败；HARDENED、H1、公网、生产、商业仍`DEFERRED/NOT_RUN` | 推送并远端回读本checkpoint；以新的唯一远端commit重建全部exact产品证据，不读取blind、不启动三角色评审 |
| 2026-08-29 | 当高德带类别搜索暂时漏掉一个明确地点时，卡片链现在只做一次去掉Provider类别过滤的确定性重查，并继续要求唯一名称、同城和本地类别一致；因此可恢复真实地点而不把错城、错类别或歧义候选自动塞给用户 | `13fb09185a7f6377444d1f7142b026600f549f73` | 前一候选的两份独立reference均为dev/validation `324/108 executable`，但validation canonical仅`48`，低于50分母，故未启动ultra；旧稳定index与当前index对比发现20个`MATCHED→UNRESOLVED`且Provider响应hash变化，脱敏live诊断证明去类型过滤后`20/20`恢复唯一同城同类；新实现限制主搜索+最多一次rewrite，主命中1次、歧义1次不重查、错城/错类别重查后仍待确认；地点/语义/评测`57 PASS`、PostgreSQL/CORE集成`21 PASS / 3 SKIP`、Ruff与diff-check PASS；`Product progress=PROVIDER / EVAL_METRIC`，`Governance ratio=0/2 changed files (0%)` | `DEV_LIVE_PROVIDER_DIAGNOSIS / AUTOMATED_TEST / FAILED_CANDIDATE_REJECTED_BEFORE_ADJUDICATION / HUMAN_EVIDENCE_FALSE / SEALED_BLIND_NOT_RUN` | 在本checkpoint远端commit重建Qwen 54+18、AMap应用表、50次链路和两份全新reference；canonical分母≥50且全部质量门槛通过后才启动ultra和三角色复审 | 新rewrite会让零兼容候选最多产生2次POI调用，但仍在既有预算内；e018候选的Qwen/AMap/应用表/50次链路/A/B全部因代码与candidate binding变化失效；没有降低阈值、修改blind/oracle或自动接受不确定地点 | 提交、推送并远端回读本checkpoint；从新唯一候选全量重建exact证据，不复用e018 reference，不启动review或blind |
| 2026-08-29 | 同一地点既出现在已安排日程又嵌在“仅供参考”的店名中时，卡片现在保留真正的日程位置，不会把参考项内部文字误当成已计划地点；高德同时返回唯一主名称和更宽泛派生名称时只采用唯一主名称精确候选，多个同名主候选仍显示“地点待确认” | `95a4298d3e07676dd1f657137fe3a1b7b1e03ec4` | `7cb69d4`两份隔离reference与fresh ultra裁决完整覆盖72案且blind读取0；正式score如实拒绝该候选：dev有1个嵌套span严重错配、DEEP_CITY覆盖`63.89%`，validation覆盖`79.17%`；逐例诊断定位上述两类产品根因后，新增反例与地点/Qwen单测`31 PASS`、G01理解/评测/CORE定向`99 PASS`，Ruff与diff-check PASS；live高德最小候选集诊断确认唯一主名称与真正多主候选可区分，未保留完整响应；`Product progress=MODEL / PROVIDER / EVAL_METRIC`，`Governance ratio=0/4 changed files (0%)` | `DEV_VALIDATION_FAILED_CANDIDATE_DIAGNOSED / DEV_LIVE_PROVIDER_MINIMAL_DIAGNOSTIC / AUTOMATED_TEST / HUMAN_EVIDENCE_FALSE / SEALED_BLIND_NOT_RUN` | 在包含本修复的远端候选重建Qwen、AMap应用表、50次链路和两份全新reference/ultra；只有正式score全部通过才启动三角色复审 | Provider候选集合曾发生漂移，最终证据必须来自同一新候选并重新绑定；`7cb69d4`全部Qwen/AMap/reference/adjudication/score/时延因产品代码变化失效，不能拼接；阈值、blind与oracle未修改 | 写入本checkpoint并推送回读；随后从新远端commit全量重建exact产品证据，不读取blind、不启动review |
| 2026-08-29 | 没有模型类别提示时，明确写成车站、酒店、餐馆或景点的原子地点也会先用相容类别查询；只有唯一主名称、同城且同类时才自动确认，其余仍是可编辑“地点待确认” | `affd93dafc6c05e58b1c07e72c9e10aa20ae8d03` | 项目所有者已明确授权`DEEP_CITY`确认数统计口径；缺类别提示反例覆盖交通/酒店/餐饮/景点，带类别查询零结果时最多一次无类型重查并继续本地类别核对；地点/Qwen定向`34 PASS`，G01理解/评测/API/地图扩展回归`98 PASS`，PostgreSQL专项`1 SKIP`（本机未提供测试数据库），Ruff与`git diff --check` PASS；产品提交subject/tree与远端branch readback PASS；`Product progress=PROVIDER / EVAL_METRIC`，`Governance ratio=0/2 changed files (0%)` | `AUTOMATED_TEST / OWNER_AUTHORIZED_EVAL_SCOPE / LIVE_MINIMAL_PROVIDER_DIAGNOSIS / HUMAN_EVIDENCE_FALSE / FINAL_EXACT_EVIDENCE_NOT_RUN / SEALED_BLIND_NOT_RUN` | 在包含本checkpoint的远端候选上重新生成Flash 54+18、AMap dev/validation、应用表和50次链路，再生成两份全新reference与ultra裁决；正式分数全部通过后才启动三角色复审 | 类别词推断会让部分地点产生一次额外重查，最终P95、费用和保守匹配精度必须用新候选实测；`d355557`及更早证据全部失效，不得拼接；阈值、blind、truth与oracle均未修改 | 提交、推送并远端回读本checkpoint；以新的远端commit为唯一候选从零重建exact产品证据，不读取blind、不启动review |
| 2026-08-29 | 用户卡片行为不变；正式地点证据现在能如实记录一次“带类别查询 + 保守无类型重查”为同一地点的两次调用，不再把合法重查误报为导出失败或少算Provider调用 | `691adf41027ac5d18929fb3c78516e649e197024` | `1ccb190`真实AMap导出在捕获后因旧合同硬限制一次调用而fail closed、未发布输出；同候选Flash运行72案为70严格有效/2可编辑部分结果，但因随后修复已失效且不用于Gate；现有v2地点数据库/HTTP/runtime回执的调用上限由1精确扩至2，三者仍绑定同一逻辑effect及聚合请求/响应hash，超过2继续拒绝；G01产品/理解/评测扩展回归`99 PASS`，评测/协议回归`63 PASS`，Ruff与diff-check PASS；HARDENED authority生成副作用已排除、文件未改变；`Product progress=EVAL_METRIC`，`Governance ratio=5/7 changed files (71%, existing G01 receipt contract/hash refresh only; no new framework)` | `LIVE_PROVIDER_EXPORT_FAILED_CLOSED / AUTOMATED_TEST / INVALIDATED_QWEN_DIAGNOSTIC / HUMAN_EVIDENCE_FALSE / SEALED_BLIND_NOT_RUN` | 在包含本checkpoint的全新远端候选上再次从零生成Flash、AMap、应用表和50次链路；全部合格后才生成reference/adjudication与正式分数 | 新候选尚无exact live证据；两次调用增加最坏延迟与调用量，必须由新运行验证；`1ccb190`及更早证据均不得拼接；阈值、blind、truth、oracle和HARDENED资产未修改 | 提交、推送并远端回读本checkpoint；建立全新外部输出目录并重跑exact Qwen/AMap，不读取blind、不启动review |
| 2026-08-29 | 高德把体育场馆或历史游览街区记作技术性的“体育设施/道路地名”时，用户仍可在名称唯一、同城且原子名称明确匹配的情况下得到正确“景点”卡片；普通道路或无关名称不会借此自动匹配 | `e9337d8379ce4266d769add8a9214abf84eb6f6a` | `1dbe9ba` exact Flash为72/72严格有效、0修复/降级、P95 `6148.407ms`，AMap dev/validation与Qwen应用表均完整产出；但validation纯DEEP_CITY预检查最多`54/72=75%`，低于80%硬门槛，故在正式标注完成前主动中止A/B与第33次前后的时延批次，全部旧回执随产品修复失效；最小脱敏live候选集确认江湾体育场`080101`和南宋御街`190301`均为唯一同城主名称；新规则只允许体育场馆词或御街/古街/步行街/斜街/文化街区词与对应技术类型组合，反例证明故宫+道路类型仍待确认；exact产品/理解/评测回归`102 PASS`、Ruff与diff-check PASS；`Product progress=PROVIDER / EVAL_METRIC`，`Governance ratio=1/4 changed files (25%, existing CORE exporter category readback only)` | `DEV_VALIDATION_HARD_GATE_PRECHECK_FAILED_AND_DIAGNOSED / DEV_LIVE_PROVIDER_MINIMAL_DIAGNOSTIC / AUTOMATED_TEST / HUMAN_EVIDENCE_FALSE / SEALED_BLIND_NOT_RUN` | 在包含本checkpoint的新远端候选再次全量生成Flash、AMap、应用表；先重算DEEP_CITY覆盖上界，达到门槛后才重启两份全新reference与50次链路 | 产品语义兼容只覆盖明确词形和Provider技术类型组合，其他地点继续待确认；最终precision、确认数与P95仍必须由新候选实测；`1dbe9ba`全部Qwen/AMap/application/packet/未完成A/B/33次时延均不可复用 | 提交、推送并远端回读本checkpoint；以新commit全量重建exact证据并先做可达性预检查，不读取blind、不启动review |
| 2026-08-29 | G01产品提交与最新主线治理已收敛到同一远端基线；长期功能固定为独立用户可见对话、独立branch/worktree和版本化prompt，主对话唯一集成，子Agent只读复核/诊断 | `8aeb7554b8b5686897f4c8b1be0b7763c645c210` | 非快进双亲与远端subject/tree/registry blob readback `PASS`；work-package validator `PASS`；独立功能对话/机器治理/G01相关定向`141 PASS`；backend全量`2263 PASS / 35 SKIP / 3 FAIL`，失败路径相对`cc06c3e`零diff；Ruff与双层diff-check `PASS`；deferred hardening分支祖先检查为false；`Product progress=NONE`，`Governance ratio=55/55 changed files (100%, authorized one-time unification checkpoint)` | `AUTOMATED_TEST / GOVERNANCE_UNIFICATION / REMOTE_READBACK_PASS / G01_AGENT_GATE_NOT_RUN` | 在本统一基线后的最新控制面commit重建Flash 54+18、AMap dev/validation、应用表、50次真实链路、两份reference与ultra裁决；通过正式分数后才启动三角色复审与唯一sealed blind | 全量3项为未改动的旧P6首页断言和两项旧Trip NLU Candidate binding失败，不能称全量绿；统一提交改变candidate binding，统一前全部Qwen/AMap/reference/score回执不得复用；H1/公网/生产/商业仍`NOT_RUN` | 提交并远端回读本baseline登记；随后从新远端subject重建G01 exact证据，不读取blind、不激活G02 |

| 2026-08-29 | 防偏航scope guard已在独立干净控制面checkpoint安装：活动切片合同、产品进展派生、G01～G06复杂机制延后、软预算、policy自改拒绝、证据冻结和Gate入口均由机器执行；原custody工作树保持未修改 | scope guard `8d1339667e4dd115a405ae1cf653db2a70d8fe34` | scope/治理/工作包定向`63 PASS`；Ruff、schema生成、`git diff --check`、scope enforce `PASS`；backend全量`2269 PASS / 4 SKIP / 3 FAIL`，失败路径相对基线零diff；现有custody审计为`DEFER_TO_G07`，22个非生成文件、2317行手写新增、4个新schema、`Product progress=NONE`；远端subject/tree回读PASS；`Product progress=NONE`，`Governance ratio=13/13 changed files (100%, owner-authorized one-time scope-guard bootstrap)` | `AUTOMATED_TEST / GOVERNANCE_SCOPE_GUARD / CURRENT_CUSTODY_AUDIT_DEFER_TO_G07 / HUMAN_EVIDENCE_FALSE / AGENT_GATE_NOT_RUN` | 为下一G01产品切片替换active_slice并强制执行；G01完成后对一个G02产品切片试运行并复盘 | 全量3项仍为既有2个Trip NLU Candidate binding失败和1个旧首页文案断言；本checkpoint不产生产品进展、不证明Provider、review、blind或最终Gate通过；custody草稿保持未提交、未混入 | 在远端控制面checkpoint上登记下一G01 `PRODUCT`切片，只允许当前Goal最小产品路径；提交前重跑scope check，不继续custody/hardening |
| 2026-08-29 | 长攻略的模型输出现只保留语义判断，日序、卡片顺序、显式目的地和本地时间提示由确定性代码恢复；Qwen请求固定单槽位、7秒和768输出token，卡片语义边界保持不变 | latency implementation `c98ecf3f80890c3932b3d22b53dbcf39d04c1b29`；first freeze `954a8cb83a5adf42766d892083f752d23865be0f`；live-limit fix `735b0203380723d41e49732419d3ddf77bd18612` | 冻结前72/72、0降级、P95 `5126.962ms`、最大`5512.512ms`、单位有效输出token较旧候选下降`38.39%`；F2 exact Qwen 72/72、P95 `4631.157ms`，AMap与Qwen应用表dev/validation完整产出；随后50次脚本因仍传2048而fail closed，最小一行修复后真实6卡/5自动匹配/编辑后路线调用0 smoke通过；scope/CORE组合与相关回归`134 PASS`；`Product progress=MODEL / PROVIDER / EVAL_METRIC`，`Governance ratio=1-line Gate Fix after bounded product slice` | `DEV_VALIDATION_LIVE_PROVIDER / AUTOMATED_TEST / FAILED_FROZEN_CANDIDATE_DIAGNOSED / HUMAN_EVIDENCE_FALSE / SEALED_BLIND_NOT_RUN` | 从新C3/F3重建Qwen、AMap、应用表、50次链路、reference/adjudication和正式score；通过后才启动三角色复审与唯一sealed blind | F2及其全部exact输出因735b020后续修复失效，不得拼接；F3 exact、review、blind、CORE、H1/公网/生产/商业仍`NOT_RUN`；3.6 Flash精确snapshot诊断schema失败，3.8 Flash别名不可回读等价snapshot | 完成CURRENT_GATE_FIX preflight并建立C3→F3；只从F3重建正式证据，不再修改模型、schema、deadline或输出上限 |

## Auto-advance

- Required gate：`Text Card Gate + PRODUCT_DELIVERY_PASS`；Next template：`TC-VNEXT-G02-MAP-STAY.md`；
- subject push/readback、耐久`PRODUCT_DELIVERY_PASS`、clean tree、无Stop后，生成完整completed归档，并在治理过渡commit原子更新`CURRENT_GOAL.md + current_goal_binding.json + current_work_packages.json`激活G02；G02 phase/profile/binding必须等于Program记录；G01不登记候选ledger、不推进authority generation；
- FUX-01、H1、公网、生产、商业和`main`不自动启动。

## Completion record

- Status：`PENDING`；
- Subject commits：既有G01产品提交保持不变；本次主线纠偏subject在提交与远端readback后写入completed归档。固定五条、v3定向、fresh PostgreSQL、前端构建和浏览器旅程已经形成`PRODUCT_DELIVERY_PASS / TEXT_CARD_GATE_PASS`；
- Remote branch：`origin/codex/trip-check-product-reset`；canonical integration：`origin/develop`；
- Verification / Evidence / Gate result：`LOCAL_AUTOMATED_REGRESSION_COMPLETE / DEV_LIVE_QWEN_AMAP_PERSISTENCE_OBSERVED / PRODUCT_DELIVERY_PASS / TEXT_CARD_GATE_PASS`；
- H1 / production / commercial：`NOT_RUN / NOT_RUN / NOT_RUN`；
- `structurally_valid=true`：只继承G00蓝图结构，不代表G01通过；
- User-visible result：`http://localhost:3000`可匿名启动固定北京三日体验并登录领取；登录后可粘贴文本生成卡片、执行六类编辑、删除原文/单份行程/全部v3旅行数据；显式live配置下，真实长文本已生成3天6卡并把4个高德精确地点及坐标写入现有活动表，首批卡片后异步执行walking/transit，编辑后只提示路线需要更新且不自动调用路线Provider。该结果是本地开发live证据，不是真人、公网或生产证据；
- Remaining risks：新的`core-mainline`尚未在GitHub PR上形成耐久PASS，G01尚未并入`origin/develop`；90条统计、50次链路、三角色复审、ultra、sealed blind及完整候选加固均为`FROZEN_G07_ASSET / NOT_RUN`，不阻断G01；
- Next autonomous action：运行固定五条、v3定向、fresh PostgreSQL、前端构建和G01浏览器E2E；通过后写入产品交付回执并完成远端PR/readback，再激活G02；
- Goal archived：`NO`；
- Next activated：`NO`；
- Promotion decision：`NOT_REQUESTED`。

## Stop conditions

- 需要降低严重错配为0的门禁；
- 需要新增付费账号或未批准Provider；
- 必须修改sealed blind/oracle；
- 无法保持旧API可读；
- 需要降低严重错配、整句/URL地点或隐私门禁。
