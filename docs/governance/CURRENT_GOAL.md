# IN PROGRESS GOAL：V0.9 候选版收口

Goal ID: TC-VNEXT-G07-CANDIDATE
Status: IN_PROGRESS
Goal type: CANDIDATE_HARDENING

<!-- PRODUCT_DELIVERY_CURRENT_GOAL_STATE
{
  "schema_version": "product-delivery-current-goal-state-v1",
  "program_id": "TC-VNEXT-2026",
  "goal_id": "TC-VNEXT-G07-CANDIDATE",
  "goal_status": "IN_PROGRESS",
  "gate_profile": "HARDENED_CANDIDATE_GATE",
  "required_gate": "Candidate Evidence Gate G0～G7 + HARDENED_CANDIDATE_GATE_PASS",
  "completion_status": "NOT_RUN",
  "gate_result": "HARDENED_CANDIDATE_GATE_NOT_RUN",
  "goal_archived": false,
  "last_completed_goal_id": "TC-VNEXT-G06-MEMORY-SHARE",
  "next_goal_id": "TC-H1-G01-HUMAN-USABILITY",
  "next_activated": false,
  "h1_status": "NOT_RUN",
  "public_network_status": "NOT_RUN",
  "production_status": "NOT_RUN",
  "commercial_status": "NOT_RUN",
  "release_status": "NOT_REQUESTED",
  "deployment_status": "NOT_REQUESTED",
  "main_merge_status": "NOT_REQUESTED"
}
-->

## Metadata

- Goal ID：`TC-VNEXT-G07-CANDIDATE`
- Program ID：`TC-VNEXT-2026`
- Product version：`V0.9`
- Mainline phase：`CANDIDATE_HARDENING`
- Gate profile：`HARDENED_CANDIDATE_GATE`
- Status：`IN_PROGRESS`
- Activation：G06 Consent & Share Gate与`PRODUCT_DELIVERY_PASS`已通过并归档
- Governance transition baseline：`origin/develop@9994be151923b9c349fc1129605777032a0b8ebe`
- Activation branch / worktree：`codex/g06-g07-transition` / `D:/munto/code/claudeProject/agentTravel-g06-g07-transition`
- Canonical implementation branch / worktree：`codex/g07-candidate-cycle-2` / `D:/CODEX/BreezeTravel`；项目所有者于2026-09-02明确批准第二候选修复周期
- Upstream / remote readback：交接基线`origin/codex/workspace-handoff-20260902@76f92b1f9ad1592cee256417658c13c3a5c858e7`，其父为原候选停止点`71b8513d4dcdc61e585e1bee6c02ce004a6ee0ac`，并保留`origin/develop@ff36a10ecae98088742e9722da3f4bf3676f6d04`祖先；2026-09-02 fresh fetch与`ls-remote`一致
- Predecessor：G06产品`e3de1b57b014439ec16eb0034e8b7e47867053d0`、交付回执`215770f2ad975ed89271047fa40780fdddbd02a0`、PR #20 integration `9994be151923b9c349fc1129605777032a0b8ebe`；develop exact-tip GitHub Actions `33402780730 PASS`
- Required gate：`Candidate Evidence Gate G0～G7 + HARDENED_CANDIDATE_GATE_PASS`
- Next Goal：`TC-H1-G01-HUMAN-USABILITY`（仅人工批准后）

## Dependencies

- 唯一激活依赖是G06归档且Consent & Share Gate与`PRODUCT_DELIVERY_PASS`通过；该依赖已由耐久回执、PR #20、`develop@9994be151923b9c349fc1129605777032a0b8ebe`远端readback和exact-tip CI满足。
- G06→G07治理过渡已由PR #21并入`develop@ff36a10ecae98088742e9722da3f4bf3676f6d04`，G07独立实现工作树已建立；候选评测、Provider、性能、可靠性、blind与复审仍未运行。
- 首个preflight先绑定fresh branch/baseline并修复G04方案A；随后冻结候选RunSpec、Provider绑定和全部required Gate矩阵。缺失项标记`NOT_RUN/NOT_READY`并自主修复，只有确需新授权或人工阶段时才按HITL处理，不能把缺失证据包装成PASS。
- G04方案A恰好两个历史失败例外保持原样披露；G07首个阻断动作是修复并移除该例外，移除前不得接受exact-binding或宣称整仓pytest零失败。

## User Outcome

用户可在候选环境稳定完成登录/体验、文本或截图输入、卡片编辑、地图查看与手动更新、住宿选择、Top-3核验、建议采纳、偏好和分享；每项能力有同一候选commit的可回读证据。

## Scope

- 只修复现有主链阻断和candidate regression；
- 性能、无障碍、隐私、安全和恢复；
- model/provider snapshot与live矩阵；
- PostgreSQL、并发、幂等、lease和重启；
- controlled public demo材料；
- architecture/recovery diagrams；
- model ablation；
- release manifest与最终disclosure。
- 将旧manifest生成器适配TC-VNEXT Goal/Gate、v3 OpenAPI、新数据集和同绑定receipts；旧360/三城测试只作历史兼容。

## Pre-approved actions

- 不预批准新产品功能、migration或Provider；
- 允许在既有合同内修复候选阻断；
- 允许当前已有零增量费用Provider Gate；
- 允许受控demo artifact、视频脚本和manifest；
- 公网部署本身仍需人工批准。

## Parallel work packages

| Package | Owned paths（首个候选preflight精确化） | Dependencies | Acceptance | Activation state |
|---|---|---|---|---|
| `WP-G07-INTEGRATOR` | G07治理、候选评测/回执、既有CI及阻断修复；首切片仅限Trip NLU candidate manifest与方案A移除路径 | G06冻结候选 | fresh baseline可回读；两个历史失败恢复普通PASS；例外执行器不再生效 | `INTEGRATOR_ONLY / CANDIDATE_HARDENING` |
| `WP-G07-PERFORMANCE` | 性能、资源预算和基准回执 | G01～G06冻结候选 | 主链P95与资源预算通过 | `NOT_STARTED` |
| `WP-G07-RELIABILITY` | 并发、恢复、lease、幂等与故障矩阵 | 同commit候选 | 重复副作用0、恢复可回读 | `NOT_STARTED` |
| `WP-G07-PRIVACY-DEMO` | 隐私/权限审查、manifest和演示材料 | 同commit公共投影 | 泄漏0、材料与边界一致 | `NOT_STARTED` |

当前registry只激活唯一集成者的第二候选周期第一轮语义保守性修复切片。恢复与资源生命周期修复已在`3e1b0cd`远端回读；本切片只关闭跨城错配、引用/预约说明误成行程、非原子二选一误成卡和mixed-role丢失计划地点四项accepted findings。上一周期全部PASS组件与stop checkpoint只作失败历史，不能拼接；任何未实际运行的层级保持`NOT_RUN/NOT_READY`。

## Decisions locked

- 候选commit上重新运行G0～G7。
- 历史证据不得拼接。
- 自动/fixture/live/browser/public/human分层披露。
- `VNEXT_CANDIDATE_READY_AGENT_VERIFIED`不等于H1、生产或商业。
- 新功能请求进入未来Program，不在收口Goal扩展。
- 所有`NOT_RUN`明确列出。
- 当前合同`structurally_valid=true`只表示结构一致；历史Intake/Candidate仍不得改写或替代当前Gate，也不得因此宣称 `V1_CANDIDATE_READY`。
- G04方案A两个精确历史失败例外必须在G07 exact-binding验收前移除；移除证据与候选commit绑定，不得扩大或重命名例外。
- `HardeningDecision`只有两种：`NOT_REQUIRED_WITH_RATIONALE`记录威胁、替代控制和残余风险；`REQUIRED`只启用威胁模型点名的控制。不得因为旧代码存在默认恢复八角色签名、broker、远端anchor或OCI。

## Non-goals

- 新城市深核验；
- 新模型/Provider；
- 一键登录；
- 新知识来源；
- 商业付费；
- H1招募和consent；
- 自动部署、release或`main`合并。

## Acceptance

完全继承Candidate Evidence Gate：

- G0～G7同一subject全部PASS，并取得`HARDENED_CANDIDATE_GATE_PASS`；
- 所有版本零容忍0；
- browser主链、刷新、断线、并发、重启、partial和performance通过；
- Provider许可与隐私无阻断；
- 受控demo、90秒视频、5分钟脚本、架构图、恢复图、消融和manifest可回读；
- final disclosure准确列出candidate、NOT_RUN和风险；
- clean tree、push和远端readback。
- `HardeningDecision`与候选commit绑定；所选控制全部实际验证，未选控制明确为`NOT_REQUIRED_WITH_RATIONALE`而非伪装PASS。

## Verification

- full backend pytest/Ruff；
- frontend/miniapp适用build；
- PostgreSQL fresh/existing migration；
- snapshot/replay；
- live Provider矩阵；
- browser E2E和P95；
- accessibility/security/privacy；
- release manifest hash/readback；
- 三角色Agent审查、fresh ultra裁决、全部所需sealed agent blind与clean checkout fresh readback；
- H1、production、commercial：`NOT_RUN`。

## Authority

- `AGENTS.md`、全部Blueprint产品/架构/治理权威、Agent Gate Protocol、Product Mainline Execution Guide、ADR-007～ADR-012、ADR-013、ADR-014；
- G01～G06 completed归档、当前候选RunSpec和同subject evidence；历史V1 manifest仅作baseline。

## Baseline

- 激活baseline：`origin/develop@9994be151923b9c349fc1129605777032a0b8ebe`；治理过渡branch/worktree：`codex/g06-g07-transition` / `D:/munto/code/claudeProject/agentTravel-g06-g07-transition`；
- 第一候选周期baseline：`origin/develop@ff36a10ecae98088742e9722da3f4bf3676f6d04`；原分支`codex/g07-candidate`停止于`71b8513d4dcdc61e585e1bee6c02ce004a6ee0ac`，不得再写入；
- 第二候选周期baseline：`origin/codex/workspace-handoff-20260902@76f92b1f9ad1592cee256417658c13c3a5c858e7`；branch/worktree：`codex/g07-candidate-cycle-2` / `D:/CODEX/BreezeTravel`；仍以`origin/develop@ff36a10`为集成祖先，不把交接提交或上一周期组件视为Gate PASS；
- dirty tree或不同binding结果不得拼接；H1/production/commercial：`NOT_RUN`。

## Invariants

- 不新增产品功能、不降低Gate、不修改blind/oracle；
- G0～G7同一subject/config/dataset/model/rule/provider重新运行；
- fixture/snapshot/live/browser/public/human/commercial分层；UNKNOWN/NOT_RUN不算PASS；
- Provider许可、隐私删除、内部字段和事实正确性均为阻断项；
- `VNEXT_CANDIDATE_READY_AGENT_VERIFIED`不自动授权H1、公网、生产、release或`main`。

## Budget

- 只使用G01～G06已准入账号/Provider和现有无增量费用矩阵；候选RunSpec冻结并记录总调用/token/延迟/成本；
- 失败策略最多两次，同一blocker两个切片无改善触发独立诊断；每切片checkpoint。

## HITL

新功能/schema/migration/依赖/Provider、费用、修改blind、公开demo部署、H1招募/consent、release/`main`需批准。

## Checkpoint ledger

| 时间 | 用户结果 | Commit | Verification | Evidence level | Product progress | Governance ratio | Remaining | Risk/failure | Next autonomous action |
|---|---|---|---|---|---|---|---|---|---|
| 2026-08-31 | G06显式记忆与分享已交付、并入`develop`并完整归档；G07候选收口合同原子激活，尚未运行候选工作 | G06产品`e3de1b57b014439ec16eb0034e8b7e47867053d0`；回执`215770f2ad975ed89271047fa40780fdddbd02a0`；integration`9994be151923b9c349fc1129605777032a0b8ebe`；本治理过渡commit在提交后由远端readback记录 | G06首轮CI`33400646254 PASS`、回执tip CI`33402192501 PASS`、develop exact-tip CI`33402780730 PASS`；fresh fetch、`rev-parse`与`ls-remote`一致 | `PRODUCT_DELIVERY_PASS / REMOTE_INTEGRATION_PASS / GOAL_TRANSITION` | `Product progress=NONE / G07_NOT_STARTED` | `Governance ratio=100% / atomic G06 archive and G07 activation only` | 合并本过渡PR；从新develop建立G07实现分支；首先修复并移除G04方案A两个历史失败例外，再冻结候选RunSpec和exact bindings | G07全Gate、live Provider、90条统计、50链、复审、blind、性能、可靠性、隐私、供应链均`NOT_RUN`；H1、公网、生产、商业、发布、部署和main仍未运行或未请求 | 校验归档/绑定/范围，提交push并通过过渡CI；合并后fresh readback再开始G07 preflight |
| 2026-08-31 | G07已从最终G06→G07过渡tip建立隔离候选工作树；两个历史失败已在未修改基线上精确复现并定位为candidate manifest的validator/scorer/gate绑定过期 | baseline `ff36a10ecae98088742e9722da3f4bf3676f6d04`；本preflight checkpoint待提交 | fresh fetch/`ls-remote`一致；两节点原样`2 failed`，指纹均为`manifest evaluator/schema code binding mismatch`；当前schema/generator与清单一致，validator/scorer/gate三项不一致 | `LOCAL_AUTOMATED / EXACT_FAILURE_REPRODUCTION / CANDIDATE_HARDENING_PREFLIGHT` | `Product progress=EVAL_METRIC / G07_IN_PROGRESS` | `Governance ratio=preflight binding only` | 提交并远端回读preflight；只更新candidate manifest绑定并移除方案A执行器，再要求两节点和普通非P5全量pytest零失败 | G07其余Gate仍`NOT_RUN`；本checkpoint不接受exact-binding、不修改blind/oracle、不声明整仓绿 | 运行治理定向、scope guard与diff check，提交push后执行最小exact-binding修复 |
| 2026-08-31 | G07候选不再把两个Trip NLU失败转换为批准例外；当前评测字节可直接验证同一数据，G01～G06累积普通后端合同已原生零失败 | preflight `e3db50d9e6ec6ae039ab4d672eac5b134d5c8e76`；exact-binding subject `2c90ea51323d0ffc819fa116ad76461760ece8f9`；耐久回执为本checkpoint | 原两节点`2 passed`；candidate code bindings五项与当前字节完全一致；custody manifest SHA-256仍为`cab1056d3a435f7a4c576a97f0d6d75ef17b8d4ed6833721ea038b64db52b0ab`且10项数据/receipt hash全匹配；原生非P5全量`2052 passed, 43 skipped, 0 failed`，pytest exit `0`；治理定向`55 passed`；Ruff与scope validation PASS | `LOCAL_AUTOMATED / EXACT_CANDIDATE_BINDING / NATIVE_FULL_REGRESSION_ZERO_FAILURE` | `Product progress=EVAL_METRIC / G07_EXACT_BINDING_REPAIRED` | `Governance ratio=targeted candidate manifest plus stale historical assertions; no product runtime change` | 提交、push并readback耐久`G07.exact-binding.json`；随后冻结G07 RunSpec与全Gate矩阵 | 完整含P5套件、G0～G7、Provider、50链、复审、blind、性能、可靠性、隐私和供应链仍`NOT_RUN`；历史G04/G05/G06回执保留当时方案A事实，不得改写为历史零失败 | 验证回执subject/tree/hash与历史回执不变，提交push后切换到RunSpec矩阵切片 |
| 2026-09-01 | 候选RunSpec、G0～G8矩阵与威胁模型已冻结；自动合同不再跳过Trip NLU，v3候选门与46条浏览器主链均可执行发现 | exact-binding receipt checkpoint `7324957e43296a7fb00b203344e0f8ac971b93b1`；candidate-contract subject `91fee72ea7f892b12e3100dafeaf5d50ea1b4e96`；耐久回执为本checkpoint | 候选合同/旧v2兼容/新v3端到端fixture等`95 passed`；Ruff PASS；scope与core-mainline PASS；G01 S0 frozen diff 0；浏览器`46 tests / 7 files`可发现；frontend production build PASS；fail-closed G07 manifest可在clean subject生成；远端subject readback一致 | `LOCAL_AUTOMATED / REMOTE_READBACK / CANDIDATE_INPUT_CONTRACT_FROZEN` | `Product progress=EVAL_METRIC / G07_CANDIDATE_CONTRACT_FROZEN` | `Governance ratio=candidate contract, gate adapter and test command only; no product runtime semantic change` | 提交并push`G07.candidate-contract.json`；在精确tip执行G0/G1原生全量、G2 PostgreSQL和后续snapshot/live/browser/performance矩阵 | 本subject尚无PostgreSQL、snapshot、live Provider、真实browser执行、50链、三角色、ultra、blind或最终HardeningDecision回执；H1/公网/生产/商业仍`NOT_RUN` | 回读耐久回执后切换为G0/G1本地候选执行切片，不拼接本合同subject为最终PASS |
| 2026-09-01 | 首轮完整后端候选回归真实执行并暴露两个合同集成问题；G06历史产品指纹已恢复，G07浏览器命令已迁入独立候选runner，历史治理路径改为精确Git回读 | candidate-contract receipt checkpoint `98f1f15a8970e08c6f4eee3dd8a22eb96d325f5a`；本G1 repair subject待提交 | 首轮原生全量`2554 passed, 44 skipped, 2 failed`；两个失败分别为误改产品package脚本导致G06指纹漂移、已归档Goal只检查当前树；修复后精确两节点`2 passed`、候选/协议/G06定向`66 passed`、Ruff PASS、候选runner可发现`46 tests / 7 files`、scope与core-mainline PASS | `LOCAL_AUTOMATED / FAILURE_PRESERVED_AND_REPAIRED / G1_RETEST_PENDING` | `Product progress=EVAL_METRIC / NO_PRODUCT_RUNTIME_CHANGE` | `Governance ratio=G07 candidate command and historical-path verifier only` | 提交并远端回读repair subject；在该精确clean tip重跑完整后端套件，零失败后生成G1耐久回执并进入G2 PostgreSQL | 当前只证明定向修复；首轮全量失败不得记为PASS，完整精确tip复验、PostgreSQL、真实browser、Provider、50链、复审、blind和最终manifest均未完成 | diff review、提交push/readback后启动完整pytest，不改写G06历史回执或产品指纹 |
| 2026-09-01 | G07文档/schema检查与完整离线后端候选套件已在同一干净subject原生零失败；首轮两项失败及其原因完整保留，未改写历史回执 | G1 repair subject `4afb86210aafe03a6cebe4dac3da9cf2365a6c58`；tree `34a7c8463f6f745a93cf1628061dae303c03a7ef`；耐久回执为本checkpoint | 远端subject/tree回读一致；G0定向`38 passed`、core-mainline PASS；G1全量`2556 passed, 44 skipped, 0 failed`，Ruff全仓PASS；候选browser runner发现`46 tests / 7 files`；产品指纹保持`c1fa88882727aca7967a4fb3ee64f10f40ca0c31febc4412d74d26877e29017b` | `LOCAL_AUTOMATED_EXACT_TIP / REMOTE_READBACK / G0_G1_CHECKPOINT_PASS` | `Product progress=EVAL_METRIC / CANDIDATE_VERIFICATION` | `Governance ratio=checkpoint receipt and atomic slice transition only` | 提交、push并回读`G07.g0-g1-local-checkpoint.json`；从该新tip执行G2真实本地PostgreSQL fresh/upgrade、事务、CAS、幂等、lease、restart和legacy检查 | 44项skip不计PASS；G0/G1仍需在最终候选subject重跑；G2～G7、live Provider、真实browser、50链、复审、blind、manifest与HardeningDecision仍未完成 | 校验receipt与v3 registry，提交checkpoint，然后探测本机PostgreSQL/Docker并运行冻结G2命令 |
| 2026-09-01 | 候选版已在独立真实PostgreSQL 16.12上完成fresh/旧库升级、事务、并发、幂等、恢复和G01～G06持久化回读；无测试skip且未触碰现有项目数据库 | G0-G1 receipt checkpoint `34fc08d2677ffda42cd8560a01880b17d28da5e6`；tree `86ecb3e4c4aae97040149e1ba540a0a38dc7feda`；G2耐久回执为本checkpoint | 固定pgvector镜像、无卷临时容器、独立端口55433；16个`*_postgres.py`加截图集成`21 passed`，fresh+seeded upgrade+二次幂等迁移`2 passed`；34个migration至`034_trip_understanding_screenshot_batches.sql`；残留仅`postgres/travel_agent`，临时容器已移除，既有容器仍运行 | `LOCAL_CONTROLLED_POSTGRESQL_EXACT_TIP / G2_CHECKPOINT_PASS` | `Product progress=EVAL_METRIC / CANDIDATE_VERIFICATION` | `Governance ratio=G2 receipt and atomic G3 transition only` | 提交、push并回读`G07.g2-postgresql-checkpoint.json`；从新tip执行G3固定snapshot、确定性重放与config drift拒绝 | G0～G2仍需最终同subject复验；G3～G7、live Provider、真实browser、50链、复审、blind、manifest与HardeningDecision仍未完成；H1/公网/生产/商业继续`NOT_RUN` | 校验G2 receipt、registry与治理定向，提交checkpoint后运行三项冻结snapshot测试 |
| 2026-09-01 | 候选版的Provider与建议链固定快照已确定性重放，网络调用为0、36项工件无重放差异，绑定漂移与live/fixture混用均被拒绝 | G2 receipt checkpoint `3685c6cb1f39c9fb6469da1aed85d8ffb5f018d4`；tree `76652a6ccd0a0a3108fea89ac60406fd2dfe2c53`；G3耐久回执为本checkpoint | 三项冻结snapshot测试`31 passed, 0 skipped, 0 failed`；provider integrity、三城suggestion与链式snapshot的文件hash/payload ID均精确绑定；未捕获路线继续返回不可用 | `LOCAL_AUTOMATED_FIXED_SNAPSHOT_EXACT_TIP / G3_CHECKPOINT_PASS` | `Product progress=EVAL_METRIC / CANDIDATE_VERIFICATION` | `Governance ratio=G3 receipt and atomic G4 transition only` | 提交、push并回读`G07.g3-fixed-snapshot-checkpoint.json`；从新tip只读核验既有零增量Provider授权/外部环境并执行G4 live runner | snapshot不等于live；G0～G3仍需最终同subject复验；G4～G7、真实browser、50链、复审、blind、manifest与HardeningDecision仍未完成 | 校验G3 receipt和registry，提交checkpoint；不读取或输出密钥值，只检查外部环境就绪状态并运行冻结live命令 |
| 2026-09-01 | G4 live合同测试与仓库外凭据就绪检查已完成；发现正式runner仍硬编码历史P6上游，当前G07 subject会被正确拒绝，未复用或改写历史分支 | G3 receipt checkpoint `127dbc9c9f11b6215f4fcfe115da1949d4a0e5a8`；本G4 adapter repair subject待提交 | live证据合同`9 passed`；仓库外Provider环境含AMap/QWeather所需键，旧根checkout的非Git `.env`含既有Qwen/AMap/QWeather键；未输出值；正式P6 builder/validator/readback均只接受`origin/codex/trip-check-p6-candidate-evidence`，与当前`origin/codex/g07-candidate`不匹配 | `LOCAL_AUTOMATED / LIVE_READINESS_KEYS_ONLY / FORMAL_BINDING_GAP` | `Product progress=EVAL_METRIC / NO_PRODUCT_RUNTIME_CHANGE` | `Governance ratio=G4 formal adapter repair only` | 新增独立G07 exact-subject spec/runner，不修改冻结P6；错误ref、migration或root继续fail-closed；定向和全量回归后提交push | 尚未发出live调用；凭据存在不等于Provider PASS；Qwen、18次地图/天气调用及脱敏回读均`NOT_RUN` | 实现最小独立适配、定向验证scope/diff；提交远端subject后才生成外部spec和运行live矩阵 |
| 2026-09-01 | 首版G4适配的全量复验真实失败并已放弃：不再修改任何G01冻结P6资产；同时将G0～G3候选验证如实归类为EVAL_METRIC | rejected adapter subject `3903a30f0613c24143e0625a03ebecdc20fb1c97`；当前独立runner修复待提交 | 首版定向P6`88 passed`、G07治理`58 passed`，但exact-tip全量`2559 passed, 44 skipped, 2 failed`；失败精确为连续`NONE`账本分类错误与两份P6冻结资产漂移；两份P6文件现已恢复baseline原字节 | `LOCAL_AUTOMATED / FAILED_STRATEGY_PRESERVED / FROZEN_ASSET_PROTECTION` | `Product progress=EVAL_METRIC / NO_PRODUCT_RUNTIME_CHANGE` | `Governance ratio=replace rejected shared-contract strategy with isolated G07 adapter` | 完成独立G07 spec/runner和fail-closed测试，确认G01-S0 frozen diff 0及两个失败节点恢复，再提交新subject重跑全量 | 本checkpoint不接受G4；live调用仍为0，3903a30不得作为候选PASS或live subject | 定向运行新G07 runner、G01-S0、账本测试、P6全套、scope和Ruff；全部通过后提交替代subject |
| 2026-09-01 | 独立G07 live spec/runner已替代失败策略；历史P6字节与G01-S0冻结面零差异，完整后端候选回归恢复原生零失败 | independent adapter subject `a59a43c7a024ab2838421122acae3e73dd72a5c7`；tree `8a8b6e6bcc9cc940cb1d1b8b574a4deae12583cc` | 远端subject/tree回读一致；新G07 spec/runner及两个原失败节点`9 passed`，P6全套`88 passed`，G01-S0 PASS/frozen diff 0；exact-tip全量`2563 passed, 44 skipped, 0 failed`，Ruff与scope PASS；产品指纹不变 | `LOCAL_AUTOMATED_EXACT_TIP / REMOTE_READBACK / G4_ADAPTER_READY` | `Product progress=EVAL_METRIC / NO_PRODUCT_RUNTIME_CHANGE` | `Governance ratio=independent G07 evidence adapter only` | 原子收紧registry并提交新tip；从该tip生成写一次仓库外G07 spec，随后执行18次地图/天气live矩阵与selected Qwen dev+validation调用 | 本checkpoint仅证明runner就绪，不接受G4；live Provider调用仍为0，最终同subject全Gate仍待运行 | 删除P6临时allowed paths、更新slice base并验证；提交push/readback后生成外部spec和运行正式G4 |
| 2026-09-01 | 候选版三城地图/天气与选定Qwen模型已在同一远端subject真实调用并完成脱敏回读；地图/天气18次、Qwen 72次均无Provider失败、fixture fallback或runner error | G4 live subject `770a6d2b36d7916a34fa75e6d1e4a7fc446d7a38`；tree `675df410ad6f77bd695252c48aad66ca1559ee0b`；耐久回执为本checkpoint | G4合同`16 passed`、Ruff PASS；AMap路线12次、QWeather预报3次/预警3次；Qwen `qwen3.7-flash-2026-07-15` dev+validation 72/72 schema-valid、repair 0、P95 `3657.351ms`；raw只在仓库外，blind读取0、secret leak 0 | `LIVE_PROVIDER_EVIDENCE_EXACT_REMOTE_SUBJECT / G4_CHECKPOINT_PASS` | `Product progress=EVAL_METRIC / CANDIDATE_VERIFICATION` | `Governance ratio=G4 receipt and atomic G5 transition only` | 提交、push并回读`G07.g4-live-provider-checkpoint.json`；从新tip补齐独立G07浏览器/性能runner，执行7文件46项真实浏览器矩阵和50条受控应用链 | Qwen非blind质量分与sealed blind仍`NOT_RUN`；估算列表价用量`¥0.051516`，实际增量账单Provider未暴露，只能声明使用既有owner-attested零增量授权；G0～G4仍需最终同subject复验，G5～G8未完成 | 校验回执hash、scope与治理合同，提交checkpoint；先检查现有P6 runner的历史绑定并实现不改冻结P6的独立G07 G5 runner |
| 2026-09-01 | G5首轮候选浏览器真实执行被正确拒绝：39/46通过，7项需要后端的旅程一致失败；失败根因是新runner只启动前端、未复用主线CI的隔离数据库/API/worker服务，而非七个独立产品缺陷 | G5 runner subject `70dc6aea65077093f789e17c7c3e9bba5e2e3c3f`；tree `3b3126ab21e5315d0f5a7d6851541c29b5f4ccf4`；仓库外首轮失败报告保留 | frontend build PASS、46 tests/7 files发现PASS；正式运行`39 passed, 7 failed`，全部失败请求均因`127.0.0.1:8999 ECONNREFUSED`；其余前端fixture测试原生通过；runner返回`G07_G5_BROWSER_MATRIX_FAILED` | `LOCAL_CONTROLLED_BROWSER_FAILED / ENVIRONMENT_HARNESS_GAP_PRESERVED` | `Product progress=EVAL_METRIC / NO_PRODUCT_RUNTIME_CHANGE` | `Governance ratio=G5 harness repair only` | 在独立G07 runner内按现有CI合同创建/迁移专用PostgreSQL，启动FastAPI和两个worker，等待health后执行浏览器，终态停止进程并删除专用数据库；不修改7个旅程或产品代码 | 本轮不得作为G5 PASS；失败产物不删除、不覆盖；50条live链、G6～G8仍未运行 | 完成服务harness与凭据隔离反例，定向回归后提交新subject；用新subject和新仓库外目录执行第二种策略 |
| 2026-09-01 | G5第二轮候选浏览器在启动隔离服务时被正确拒绝；专用数据库已迁移且终态清理，但Windows默认Proactor事件循环与异步Psycopg不兼容，后端未能进入健康态 | fixture-service subject `34cca0967504bca2830ebd23d3ead683a845258f`；仓库外第二轮失败日志保留；Selector/checkpointer repair待提交 | schema迁移完成；后端日志精确报`Psycopg cannot use the ProactorEventLoop`并以`PostgreSQL Checkpointer unavailable`退出；runner返回`G07_G5_FIXTURE_SERVICE_EXITED`；清理回读无`breezetravel_g07_browser_%`残留数据库；修复后定向`9 passed`、Ruff与diff check PASS | `LOCAL_CONTROLLED_BROWSER_FAILED / WINDOWS_EVENT_LOOP_DIAGNOSIS_PRESERVED` | `Product progress=EVAL_METRIC / NO_PRODUCT_RUNTIME_CHANGE` | `Governance ratio=G5 Windows service adapter and schema setup only` | 让三个fixture服务子进程在Windows使用Selector策略，并在启动服务前复用正式migration入口初始化迁移与LangGraph checkpointer；提交新exact subject后以新目录执行，不覆盖前两轮失败 | 本轮不得作为G5 PASS；浏览器用例尚未真正开始，50条live链、G6～G8仍未运行；两次失败策略均已达到预算并完成差异化独立诊断 | 提交、push/readback Selector/checkpointer修复；仅在新exact subject执行第三次正式browser矩阵，若仍失败则保留证据并停止重复同类策略 |
| 2026-09-01 | G5第三轮已真实运行全部46项并发现两个累积产品合同回归；44项通过，未重跑或放宽失败项，正式失败报告、截图和trace均保留 | Windows service repair subject `3e204e682c55d07302e4ffa7fb25058f8124deb5`；tree与远端三方回读一致；产品修复待提交 | `44 passed, 2 failed, 0 skipped, 0 flaky`；匿名链的行程结果把独立住宿任务`PREPARING`动态叠加到原始结果，违背原v3稳定投影；登录链在G03R页面找不到既有可访问名称`新增地点到 Day 1`，页面快照确认按钮只剩视觉文案；专用数据库终态清理PASS | `LOCAL_CONTROLLED_BROWSER_FAILED / PRODUCT_REGRESSION_REPRODUCED` | `Product progress=RUNTIME+UI / CANDIDATE_REPAIR` | `Governance ratio=existing-contract regression repair, no new feature/schema/provider` | 保持46条浏览器旅程与断言原样；让`GET result`保留已持久化住宿投影、动态状态继续由独立住宿端点提供，并恢复Day级新增按钮aria-label；先做内存/PostgreSQL/API/UI定向复验再创建新subject | 本轮不得作为G5 PASS；G0～G4 checkpoint仍仅是历史切片，产品字节变化后最终同subject必须重跑；50链、G6～G8未运行 | 切换为G5产品回归修复切片，新增确定性反例并运行定向/全量回归；未形成clean远端subject前不再启动正式浏览器矩阵 |
| 2026-09-01 | 两个G5产品回归已按既有合同最小修复；动态住宿建议仍由专用端点提供，选择后的住宿仍随新revision回读，Day级新增按钮恢复明确可访问名称 | repair基于`3e204e682c55d07302e4ffa7fb25058f8124deb5`，新subject待提交 | 内存/API/治理定向`43 passed, 1 integration skipped`；专用PostgreSQL反例`1 passed`；前端build PASS；首轮全量`2568 passed, 44 skipped, 1 failed`，唯一失败为G06历史回执错误对比当前产品树；改为从G06绑定commit重建指纹后治理定向`35 passed`、Ruff/scope/diff PASS；另一次非正式G03R诊断`28 passed, 1 timed out`，正式第三轮中的同29项原为全通过，诊断失败不计PASS且不覆盖 | `LOCAL_AUTOMATED_REPAIR / HISTORICAL_RECEIPT_BINDING_FIXED / EXACT_TIP_RETEST_PENDING` | `Product progress=RUNTIME+UI / CANDIDATE_REPAIR` | `Governance ratio=historical Git-object fingerprint readback plus exact repair scope` | 提交、push/readback新subject；完整后端零失败后，以新仓库外目录重跑46项正式浏览器，不复用失败或诊断产物 | 尚无新exact-tip全量或browser PASS；44项skip不计PASS；产品字节变化使最终G0～G8必须在后续最终同subject重跑 | 校验完整diff与产品指纹，提交远端repair subject；运行原生全量后端，若零失败再进入新的正式browser执行 |
| 2026-09-01 | G5第四轮在新exact subject完成全部46项真实浏览器执行并收敛到一个兼容性回归；住宿稳定投影和Day级新增入口均已通过，旧地点详情的“移到后一天”快捷入口在G03R重设计后缺失 | repair subject `243c2c63919a4c0008ebdd22ba10117ca7a09671`；tree `f2e84a0552e54b965ac15b263618d96433196493`；第四轮失败报告、截图和trace保留在仓库外独立目录 | exact-tip全量后端`2569 passed, 44 skipped, 0 failed`、Ruff、frontend build、治理与PostgreSQL反例均PASS；正式browser `45 passed, 1 failed, 0 skipped, 0 flaky`，唯一失败为地点详情找不到既有可访问名称`移到后一天`，页面仍有新版`移动位置`；专用数据库终态清理PASS | `LOCAL_CONTROLLED_BROWSER_FAILED / SINGLE_PRODUCT_COMPATIBILITY_REGRESSION` | `Product progress=UI / CANDIDATE_REPAIR` | `Governance ratio=restore existing user shortcut without changing command contract` | 保持旅程、断言和通用“移动位置”原样；仅在非末日地点详情恢复“移到后一天”，复用既有`ACTIVITY_MOVE`命令并移动到下一日末尾；build与scope通过后创建新subject | 本轮不得作为G5 PASS；50条live性能链、G6～G8及最终同subject全Gate仍未运行 | 提交、push/readback最小UI修复；仅以新exact subject和新仓库外目录执行第五轮完整browser矩阵 |
| 2026-09-01 | G5第五轮再次完成46项真实浏览器执行并证明“移到后一天”已恢复；唯一剩余失败是同一旧地点详情旅程随后找不到“删除这张卡片”快捷入口，新版卡片菜单和删除确认面板仍存在 | shortcut subject `a7fc87ff838f3828bfb745444f9154b8a7c182dd`；tree `bd893a71ef0b4b3c8ca72a9f17e00e709b0a597d`；第五轮失败报告、截图和trace保留在仓库外独立目录 | frontend build、scope、治理定向`33 passed`、Ruff PASS；正式browser `45 passed, 1 failed, 0 skipped, 0 flaky`，失败发生在成功执行下一日移动后的删除步骤，等待可访问名称`删除这张卡片`超时；专用数据库终态清理PASS | `LOCAL_CONTROLLED_BROWSER_FAILED / SECOND_DETAIL_SHORTCUT_REGRESSION` | `Product progress=UI / CANDIDATE_REPAIR` | `Governance ratio=restore second existing detail shortcut without removing accessible replacement` | 保留新版卡片删除入口与可访问二次确认面板；在地点详情恢复原有“删除这张卡片”及浏览器确认语义，复用`ACTIVITY_DELETE`；不改旅程、命令或阈值 | 本轮不得作为G5 PASS；第五轮无browser receipt；50链、G6～G8和最终同subject全Gate仍未运行 | build、scope与交互定向通过后提交新subject；以新仓库外目录执行第六轮完整browser矩阵 |
| 2026-09-01 | G5已在同一远端subject完成完整浏览器与50条真实应用链；旧快捷入口与新版可访问操作并存，46项用户旅程全通过，关键等待时间低于冻结阈值 | G5 subject `87bb9767bbddd08f4ca038d2612bd9a73c4af27d`；tree `f51b7e0e2a129069a2d7da5eab9618b0740f7ab5`；耐久回执为本checkpoint | browser `46 passed, 0 skipped, 0 flaky`；50/50真实Qwen+AMap+PostgreSQL链成功，Qwen 50次、路线300次、repair 0、泄漏0；首进度P95 `21.555ms <= 500ms`，可编辑卡片P95 `4252.347ms <= 8000ms`；专用数据库和两个任务容器均已删除 | `LOCAL_CONTROLLED_BROWSER_FIXTURE + DEV_LIVE_PROVIDER_APPLICATION_CHAIN / G5_CHECKPOINT_PASS` | `Product progress=USER_VISIBLE_RUNTIME+UI / CANDIDATE_VERIFICATION` | `Governance ratio=G5 receipt and atomic G6 transition only` | 提交、push并回读`G07.g5-browser-performance-checkpoint.json`；进入G6同subject manifest聚合器预检，补齐独立component receipts输入与准确NOT_RUN披露后再形成最终candidate subject | 本checkpoint不等于最终候选PASS；G0～G5仍需在最终同一subject重跑；G6～G8、三角色、ultra与sealed blind未完成；实际增量账单Provider未暴露；H1/公网/生产/商业保持NOT_RUN | 校验G5回执、scope与治理合同并提交；检查G6 builder现状，以fail-closed方式实现外部component receipt聚合且不把editable matrix状态铸成PASS |
| 2026-09-01 | G6清单聚合器已补齐严格外部component receipt输入；清单可以证明四类回执是否齐全同版，但仍不会把清单或可编辑矩阵状态冒充最终候选PASS | G5 checkpoint `05adba22369ea46a0612b7a39eca5ac0167b5ee3`；G6 adapter subject待提交 | G07 manifest/registry定向`10 passed`、Ruff、scope与diff check PASS；完整四组件、缺组件、重复组件和跨subject反例均覆盖；输入只能来自仓库外并按commit/tree/config/data/contract与文件hash验证 | `LOCAL_AUTOMATED / G6_MANIFEST_ADAPTER_READY` | `Product progress=EVAL_METRIC / NO_PRODUCT_RUNTIME_CHANGE` | `Governance ratio=manifest component aggregation only` | 提交、push/readback新subject；在clean exact tip生成无组件baseline manifest确认准确NOT_RUN，再进入G8 HardeningDecision与G7三角色/ultra/blind的实际组件准备 | 尚无真实四组件回执，G6正式PASS、G7/G8及最终同subject重跑均NOT_RUN；未实现或启用外部签名、broker、远端anchor或OCI | exact-tip运行manifest定向/全量治理和baseline CLI；确认clean、远端一致及无secret后保存G6 preflight证据并继续G8决策 |
| 2026-09-01 | 最终候选门已补上HardeningDecision与候选威胁模型的精确hash校验，错误或跨版本决策不能再进入最终聚合 | G6 adapter `60c01ac5db8b527a7cc613d790f5e55c5b6d7188`；threat-binding subject待提交 | 候选门/治理定向`10 passed`、Ruff、scope与diff check PASS；正确绑定、错误threat hash、已有控制选择矛盾均fail-closed覆盖 | `LOCAL_AUTOMATED / G8_BINDING_REPAIR` | `Product progress=EVAL_METRIC / NO_PRODUCT_RUNTIME_CHANGE` | `Governance ratio=candidate contract only; frozen final_gate and other agent-gate paths unchanged` | 提交、push/readback新subject；冻结该subject后再生成角色输入包、三份隔离审查与fresh ultra裁决，并根据实际finding生成最终HardeningDecision | 三角色、ultra、blind和真实四组件仍NOT_RUN；当前只证明决策绑定校验器，不代表`NOT_REQUIRED_WITH_RATIONALE`已经成立 | exact-tip重跑候选合同后建立fresh clean只读审查checkout，先执行三角色，裁决后再决定控制而非沿用provisional文字 |
| 2026-09-01 | threat-binding subject的首次完整后端复验发现冻结协议闭包hash未同步；已只刷新`candidate_gate.py`精确hash并保持历史G01 Qwen prompt绑定原样 | threat-binding subject `1d2b41959b12c8f2882594c93705aaffd27c9524`；protocol refresh待提交 | 首轮全量`2569 passed, 44 skipped, 2 failed`，两项均为`protocol_contract.json`代码hash漂移；生成器一度同时提出历史G01 prompt hash刷新，已明确放弃并恢复原字节；两个原失败节点加候选门`7 passed`、Ruff、scope、diff check PASS | `LOCAL_AUTOMATED / IMMUTABLE_PROTOCOL_HASH_REPAIR / FAILED_FULL_RUN_PRESERVED` | `Product progress=EVAL_METRIC / NO_PRODUCT_RUNTIME_CHANGE` | `Governance ratio=one generated protocol hash plus checkpoint only` | 提交、push/readback协议hash；在新exact tip重跑完整后端零失败，再建立新的clean review checkout；旧`1d2b419`审查checkout不得用于正式review | 本轮不得作为G1 PASS；完整复验尚未运行，1d2 subject及其review checkout因协议字节变化失效 | 提交最小hash刷新；移除旧review worktree并从新subject建立clean checkout，同时启动第二次完整后端复验 |
| 2026-09-01 | 首轮三角色与fresh ultra裁决已完成：产品体验通过，语义与可靠性失败；裁决采纳两个P1和一个当前范围P2，三项均已加入反例并做最小产品修复 | review subject `2c7513e7b455b734350c8ed73ee4642c7c246514`；tree `2bfcc368672c09fbb812b9eb66698131c79cf821`；三份角色回执与ultra裁决保存在subject专属仓库外目录；repair subject待提交 | 裁决`FAIL`且采纳`AGF-SEMANTIC-DESCRIPTION-CARD-001`、`AGF-PROVIDER-PARTIAL-FACT-LOSS-002`、`AGF-SHARE-PATH-ACCESS-LOG`；修复后精确反例`5 passed`、语义/API/Qwen扩大回归`60 passed`、范围定向`47 passed`、Ruff与diff check PASS；首轮全量`2574 passed, 44 skipped, 1 scope failure`完整保留，原子切换review repair范围且未放宽验证器后第二轮全量`2575 passed, 44 skipped, 0 failed` | `MULTI_AGENT_SIMULATED_REVIEW_FAIL / LOCAL_AUTOMATED_REPAIR / NATIVE_FULL_REGRESSION_ZERO_FAILURE` | `Product progress=RUNTIME / PRIVACY+SEMANTIC+RELIABILITY_REPAIR` | `Governance ratio=active slice ownership and checkpoint only` | 提交、push并远端回读新subject；按新commit/tree/config/data绑定重建G0～G7及fresh三角色/ultra证据 | 旧subject所有G0～G7证据均仅作诊断，不得拼接；44项skip不计PASS；sealed blind与可验证component receipts仍未完成；H1、公网、生产、商业、发布、部署与main保持NOT_RUN或未请求 | 最终复核diff与敏感信息扫描；创建可恢复commit并push/readback，再将所有正式证据切换到新subject |
| 2026-09-01 | 四类候选组件不再接受手写PASS摘要：v2回执绑定仓库外原始工件路径/hash、Git固定验证器path/hash和重算摘要hash；候选Gate与发布清单调用同一重验证入口 | component-verifier implementation `a3a6a10cafa212f7217f5bf027f128f5902fa92a`；tree `894d8e718a1717d32bfdbae9337eb348ced775d4` | 防伪与聚合定向`70 passed`；完整后端`2579 passed, 44 skipped, 0 failed`；生成合同逐字回读、Ruff、scope、diff与敏感值扫描PASS；上一候选真实G4/G5工件由新验证器重算为地图/天气18、Qwen72、浏览器46、性能链50，但因跨subject只作验证器诊断 | `LOCAL_AUTOMATED / FIXED_RAW_COMPONENT_REVALIDATION / HISTORICAL_ARTIFACT_DIAGNOSTIC_ONLY` | `Product progress=EVAL_METRIC / CANDIDATE_EVIDENCE_INTEGRITY` | `Governance ratio=v2 component receipt, fixed verifier, builder and exact tests only` | push并远端回读本实现与checkpoint；在新exact subject执行自动化组件、G4/G5 live重建、fresh三角色/ultra和唯一sealed blind，所有v2回执均重新生成 | 当前尚无绑定新subject的四组件回执；sealed custodian执行器、最终HardeningDecision、G0～G8与Candidate Gate均未完成；H1、公网、生产、商业、发布、部署和main未运行或未请求 | 实现只读blind custodian执行器和原始输入/真值/预测重评分；定向与全量通过后冻结最终subject，再开始一次性正式证据重建 |
| 2026-09-01 | 唯一sealed blind执行器已冻结：18条输入先做角色中立AMap目录，再逐例执行选定Qwen；双xhigh参考与fresh ultra裁决只能引用同批类型化Provider事实，最终固定验证器逐例重算Qwen/AMap绑定与全部blind阈值 | sealed runner implementation `7ce4e8c2bb582b62586b94479bdfea8f35c5306e`；tree `a8ca579ac6b3cb98e147d979e5734263d36a385a`；本治理checkpoint待提交 | 封板/协议/候选聚合定向`71 passed`；最终冻结命令`2583 passed, 44 skipped, 0 failed`；Ruff、scope、core-mainline与diff检查PASS；另一次完整运行在历史P4 solver子进程读取线程处超时，缩小复现`1 passed`后最终完整命令通过 | `LOCAL_AUTOMATED / SEALED_RUNNER_READY / REMOTE_READBACK` | `Product progress=EVAL_METRIC / FINAL_EVIDENCE_RUNNER_FROZEN` | `Governance ratio=sealed runner, exact raw revalidation and checkpoint only; no product runtime/API/migration change` | 提交、push并回读本checkpoint；以新exact subject在fresh clean checkout重建自动化、live、browser/performance、三角色/ultra和唯一sealed blind证据，再生成四份v2组件与HardeningDecision | 当前仍无绑定新subject的四组件PASS或sealed结果；44项skip不计PASS；非确定性P4子进程超时保留为风险；H1、公网、生产、商业、发布、部署与main未运行或未请求 | 先冻结治理subject并远端回读；随后只在独立clean checkout和仓库外目录执行正式证据，不读取blind逐例内容回主任务 |
| 2026-09-02 | 首次最终subject的G4 live与Qwen 72条均通过，但G5浏览器在45/46处发现测试定位器把活动“前门大街”和含该子串的酒店标题同时命中；产品目标活动真实存在，失败属于可访问名称子串选择歧义 | final evidence subject `0e7fb2b54e934a968a4fdb045da761f49ddcb005`；tree `7d087de3e8b135a657b5db9f82998697182fd982`；修复subject待提交 | G4地图/天气18 PASS；Qwen72/72 schema-valid、repair 0、P95 `4272.869ms`；首次browser因干净checkout缺node_modules而0项执行，安装锁定依赖后第二次`45 passed, 1 failed`，唯一失败为`getByRole heading name=前门大街`严格模式命中活动与酒店两标题 | `LIVE_PROVIDER_DIAGNOSTIC_ONLY / CONTROLLED_BROWSER_FAILED / TEST_LOCATOR_DEFECT` | `Product progress=EVAL_METRIC / NO_PRODUCT_RUNTIME_CHANGE` | `Governance ratio=one exact Playwright locator plus failure checkpoint` | 将该断言改为`exact: true`，不改产品、旅程、覆盖或期望标题；定向、build、scope和全量通过后提交新subject，0e7所有工件不再晋级 | 0e7的live调用与失败浏览器只作诊断；新subject的自动化、live、browser/performance、fresh panel、sealed和四组件仍全部待重建；实际增量账单未暴露 | 完成最小断言修复并验证；提交push/readback后创建新clean checkout，从空仓库外根重新执行全部正式证据 |
| 2026-09-02 | 新subject的46项浏览器旅程全部通过，但50条真实应用链的可编辑卡片P95为`8044.806ms`，比冻结`8000ms`门槛慢`44.806ms`；失败证据保留且没有重跑、删样本或降门槛 | performance subject `b705f29cd60e340ae693cdd443c697ec89297397`；tree `98c510b964b4534a7dc502346758f155d9c2e071`；仓库外browser PASS与performance FAIL回执均保留；连接复用修复subject待提交 | browser `46 passed`；performance 50/50链完成、Qwen 50次/repair 0、首进度P95 `54.021ms`、卡片P95 `8044.806ms`、路线调用298、一个`PARTIAL_RESULT`；定位并修复每次AMap查询各建HTTP client的问题；生命周期/候选定向最终`66 passed`，完整后端`2584 passed, 44 skipped, 0 failed`，Ruff、scope、core-mainline、G01冻结diff均PASS；中途一个旧治理断言拒绝产品路径，已收紧为仅允许三个连接生命周期文件后通过 | `CONTROLLED_BROWSER_PASS_DIAGNOSTIC_ONLY / DEV_LIVE_PROVIDER_PERFORMANCE_FAIL / LOCAL_AUTOMATED_REPAIR_PASS` | `Product progress=RUNTIME_PERFORMANCE / CANDIDATE_REPAIR` | `Governance ratio=connection lifecycle plus exact raw verifier consistency; no threshold/model/sample change` | 提交并远端回读连接复用修复；从新exact subject先重跑46项browser和50条performance，只有二者均通过才继续G4、自动组件、fresh panel与sealed | b705不得晋级；当前本地测试不替代新subject真实性能；产品字节变化后G0～G8均须重建；sealed一次性评分仍未启动 | 创建可恢复commit并push/readback；更新clean正式checkout到新subject，从全新仓库外证据根执行browser/performance |
| 2026-09-02 | `18597b1`的browser、50链、live与自动组件均通过，但fresh三角色/ultra裁决接受4个P1并正式拒绝候选；普通活动误成地点、用餐同名酒店误匹配、旧分享内部字段泄漏和public兼容接口暴露均已做最小修复并完成本地全量回归 | rejected subject `18597b108de2bc1ba2e6a0a69f2932115b66eced`；tree `291c3c2a613cd8be48a5bb4c7adb4b349370411b`；三份review与ultra裁决冻结在subject专属仓库外目录；repair subject为本checkpoint | 裁决`FAIL / accepted P1=4 / scenario union complete`且schema校验PASS；扩大定向`131 passed`；完整后端`2591 passed, 44 skipped, 0 failed`；frontend typecheck/build PASS；冻结候选browser仍精确发现`46 tests / 7 files`；Ruff全仓、scope、core-mainline、G01 S0 frozen diff与diff check全部PASS。修复后普通活动Provider调用0，用餐语境选择餐厅，旧分享DTO无revision/hash/report/finding/rule/status/severity/地点ID，陈旧CAS token返回409；public OpenAPI仅保留登录/资料、v3主链和最小health，不再挂载旧chat/workspace/audit/repair/share/rooms/itineraries/test-login/metrics或内部架构说明 | `MULTI_AGENT_SIMULATED_REVIEW_FAIL / LOCAL_FULL_REGRESSION_PASS / PUBLIC_PROJECTION_HARDENING` | `Product progress=SEMANTIC+PRIVACY+PUBLIC_RUNTIME / CANDIDATE_REPAIR` | `Governance ratio=active scope and failure checkpoint only; no Gate/threshold/blind change` | 提交push/readback新subject；随后从全新证据根重建自动、live、browser/performance和fresh panel，全部通过后才允许唯一sealed blind | `18597b1`所有PASS组件因panel FAIL且后续产品字节变化只能作诊断；本地全量仍不等于新subject完整Gate；新subject的真实PostgreSQL、live Provider、browser/performance、fresh panel和sealed均未运行；H1/公网/生产/商业/发布/部署/main继续NOT_RUN或未请求 | 最终diff与敏感信息复核后创建可恢复commit并远端回读；再更新fresh clean checkout并从空证据根执行正式矩阵 |
| 2026-09-02 | `400ce17`的自动、live、browser与50链全部通过，但第二轮修复复核三角色均FAIL；fresh ultra裁决采纳4个P1和2个当前范围P2，证明住宿公开字段、资料错误提示、多城/引用/非原子语义及attempt fencing仍直接阻断用户结果 | rejected subject `400ce176149634d23cadb46288abaf3b20c80f8d`；tree `4fd4f8fb25b0401fbd10fb07651a27caf97ec400`；三份review SHA分别为`84d97e99`、`43f55845`、`09bea500`，ultra裁决SHA为`4ab4d02a`；第三轮repair subject待提交 | review/adjudication schema、commit/tree/config/data、input/prompt/schema/evidence hash、任务隔离和时间顺序全部PASS；裁决`FAIL / accepted P1=4 / accepted in-scope P2=2 / scenario union complete`；主任务逐条复现：多城和南京reference城市错误、引用/预约说明/比较句/泛称酒店产生Provider调用或卡片、`evidence_gap`进入公开OpenAPI/JSON/client/DOM、资料页透传后端code、同worker ID旧attempt可把attempt-2置为SUCCEEDED | `MULTI_AGENT_SIMULATED_REVIEW_FAIL / EXACT_LOCAL_REPRODUCTION / THIRD_REVIEW_REPAIR_AUTHORIZED_BY_PROTOCOL` | `Product progress=RUNTIME+SEMANTIC+PRIVACY+RELIABILITY / CANDIDATE_REPAIR` | `Governance ratio=third-round scope and stop condition before product edits` | 只做六项最小修复并加入反例；完整本地/PostgreSQL/前后端验证后提交新subject，重建全部正式证据并执行最后一轮fresh受影响复审 | 本轮是协议允许前提记录后的第三轮；若fresh复审仍接受当前范围P0/P1则停止，不启动第四轮或sealed；`400ce17`全部PASS组件仅作诊断且不得拼接；sealed仍从未启动 | 原子更新repair-3范围与停止条件后实施修复；保持Provider、migration、模型、冻结输入、阈值、oracle和blind字节不变 |
| 2026-09-02 | 第三轮六项最小修复已完成：多城/其他城市与引用内容不再错搜或成卡，公开住宿与资料失败提示完成脱敏，三类任务的同worker旧attempt均被fencing；公开OpenAPI exact绑定已同步 | repair基于`400ce176149634d23cadb46288abaf3b20c80f8d`；新subject为本checkpoint | 新反例`5 passed`、公共隐私`6 passed`、语义/地图/Provider扩大回归`77 passed`；真实PostgreSQL understanding/map/stay路径`2 passed`且临时数据库零残留；frontend typecheck/build与client typecheck/build PASS；首轮完整后端`2589 passed, 44 skipped, 6 failed`，六项精确为OpenAPI旧hash和上一轮无runtime假设，更新exact hash与本轮精确范围后manifest/registry/Agent Gate/G01定向`73 passed`、Ruff与G01 frozen diff PASS | `LOCAL_RUNTIME_REPAIR / CONTROLLED_POSTGRESQL / FAILED_FULL_RUN_PRESERVED / GOVERNANCE_EXACT_BINDING_REPAIRED` | `Product progress=RUNTIME+SEMANTIC+PRIVACY+RELIABILITY / CANDIDATE_REPAIR` | `Governance ratio=OpenAPI exact hash、第三轮精确范围与失败checkpoint；未改Gate/阈值/blind` | 提交、push并远端回读新subject；在clean exact tip重建自动、live、browser/performance与第三轮fresh panel | 首轮完整回归不得记为PASS；44项skip不计PASS；新subject的全量、live、browser、panel、四组件、sealed和最终Gate均待重建；若第三轮panel接受任何范围内P0/P1即停止 | 完成diff/敏感信息复核并创建可恢复subject；从空证据根执行最终矩阵，panel PASS前不启动sealed |
| 2026-09-02 | `252f43d`自动化、live Provider、46项浏览器与50条应用链均通过，但第三轮final fresh panel正式FAIL；ultra裁决接受4个P1、3个当前范围P2和1个P3，当前候选被停止条件拒绝 | rejected subject `252f43d758761edcd131ba651d8275f09b7f3791`；tree `d08b6d303de48a8adf7475679ce785b14c06628c`；自动组件SHA `25604a7a`、live组件SHA `bf6b5f78`、三份review SHA `6db28744`/`772c888b`/`5876a9b5`、裁决SHA `2c881962`；本治理stop-checkpoint commit提交后由远端readback记录 | fresh exact-tip隔离全量`2595 passed, 44 skipped, 0 failed`；真实PostgreSQL repair路径`2 passed`；frontend/client typecheck+build与Ruff PASS；自动组件与live组件均PASS，browser `46 passed`，50链卡片P95 `5499.14ms <= 8000ms`；8/8来源finding无遗漏无重复、scenario union complete、checks_not_run=0；正式panel组件生成器以exit 2拒绝把FAIL铸成PASS且未产生输出 | `AUTOMATED_TEST_PASS + LIVE_PROVIDER_EVIDENCE_PASS + MULTI_AGENT_SIMULATED_REVIEW_FAIL / STOP_CONDITION_TRIGGERED` | `Product progress=RUNTIME / CANDIDATE_REJECTED` | `Governance ratio=stop checkpoint only; no product/Gate/threshold/blind change` | 当前合同下无后续自主产品动作；只提交、push并远端回读`G07.final-panel-stop-checkpoint.json`后停止 | 成立问题包括跨城错配、引用/预约说明误成行程、非原子/二选一误成卡、mixed-role丢失计划地点、资料读取失败伪装空资料、stale map cache副作用与推理Provider未关闭；sealed/final Gate均`NOT_RUN`，G07未完成且不归档 | 校验stop receipt、治理/范围与远端subject；禁止第四轮、sealed、H1、公网、生产、商业、发布、部署和main |
| 2026-09-02 | 项目所有者明确批准开启第二候选修复周期；新分支从远端交接提交建立，上一周期FAIL和停止条件完整保留为历史边界 | baseline `76f92b1f9ad1592cee256417658c13c3a5c858e7`；branch `codex/g07-candidate-cycle-2`；本合同重绑定subject待提交 | fresh fetch、三远端ref readback和分支祖先关系已核对；产品测试、Provider、browser、panel与sealed在新周期均`NOT_RUN` | `OWNER_AUTHORIZATION / REMOTE_HANDOFF_BASELINE / CONTRACT_REBINDING` | `Product progress=NONE / GOAL_TRANSITION` | `Governance ratio=second-cycle authorization and exact branch binding only` | 完成RunSpec与Gate适配重绑定、定向测试、提交push和远端readback；随后激活第一轮产品修复切片 | 上一周期所有组件不得复用；最新accepted findings尚未在本机以新周期用例重新复现；H1/公网/生产/商业/发布/部署/main仍未运行或未请求 | 验证合同fail-closed后切换到第一轮语义、恢复与资源生命周期修复，不运行sealed |
| 2026-09-03 | 第二候选周期机器合同已在GitHub形成独立可回读subject；开始第一轮恢复与资源生命周期修复 | contract subject `a602d5a79580779d018d12dbf67d8a8a6eb397c5`；tree `ed7d203efa811c989940969c82aaebe812059bde`；branch `codex/g07-candidate-cycle-2` | 合同定向共`18 passed, 8 failed`；8项均由本机仅有Codex runtime Git而无协议允许的系统Git触发fail-closed，未改写为PASS；core-mainline与diff check PASS；远端commit/tree/parent readback一致 | `OWNER_AUTHORIZATION / REMOTE_READBACK / FORMAL_GATE_BLOCKED_EXTERNAL` | `Product progress=RUNTIME+UI+RELIABILITY / CANDIDATE_REPAIR` | `Governance ratio=active repair scope only; no Gate/threshold/blind change` | 复现并修复资料读取失败、stale地图缓存副作用和推理Provider生命周期，运行后端/前端定向与可用的主线校验 | 正式Agent Gate继续受本机缺少受信任系统Git阻断；语义类accepted findings留到后续切片；live/browser/performance/panel/sealed均NOT_RUN | 先加入三项失败反例，再做最小运行时/UI修复；不降低host tool policy，不运行sealed |
| 2026-09-03 | 资料读取失败改为明确可重试状态且不再显示空表单；stale地图attempt在lease fencing前不再写缓存；流水线关闭会释放Qwen与地点Provider | repair-1 subject `3e1b0cdf168d7f291e0096d21cb600abab4e9245`；tree `2f0509792200f9b8dbe6f4f9c72e8ed896331c45` | 资源/缓存/资料失败精确反例`5 passed`；扩大Trip Understanding与Qwen回归`54 passed, 1 skipped`，skip为未启用受控PostgreSQL；Ruff、frontend TypeScript与Next production build、commit-bound core-mainline PASS；GitHub commit/tree/parent回读一致 | `LOCAL_AUTOMATED / UI_BUILD / REMOTE_READBACK / CANDIDATE_REPAIR` | `Product progress=RUNTIME+UI+RELIABILITY / CANDIDATE_REPAIR` | `Governance ratio=active scope/checkpoint only; no Gate/threshold/blind change` | 激活并实现语义保守解析修复切片；先用新周期反例复现四类accepted findings | 真实PostgreSQL、live、browser/performance、fresh panel与sealed均NOT_RUN；跨城、引用预约、非原子二选一和mixed-role findings仍待新周期修复 | 只改现有确定性语义边界并加入反例；不复用上一周期组件 |
| 2026-09-03 | 多城Provider返回异城地点时保守拒绝自动匹配；预约说明内嵌Day动作保持引用；二选一不再生成确定卡片；同句计划+参考分别保留正确角色 | 基于repair-1 `3e1b0cdf168d7f291e0096d21cb600abab4e9245`；semantic repair subject为本checkpoint | 四项精确反例先`4 failed`后`4 passed`；Trip Understanding + G03R语义`54 passed`，Ruff与diff check PASS；扩大AMap/地点解析共`113 passed, 2 failed`，两项均为未改AMap候选计数字段的既有断言差异，未记为PASS且独立保留 | `LOCAL_AUTOMATED / EXACT_FAILURE_REPRODUCTION / CANDIDATE_REPAIR` | `Product progress=RUNTIME+SEMANTIC / CANDIDATE_REPAIR` | `Governance ratio=scope/checkpoint only; no prompt/model/Provider/data/Gate/threshold/blind change` | 提交、运行commit-bound core-mainline、push并远端回读；随后在新exact tip执行可用的扩大回归并准备fresh候选验证 | 正式系统Git、真实PostgreSQL、live、browser/performance、fresh panel与sealed仍NOT_RUN；AMap两项既有计数断言差异待独立归属确认 | 先冻结并远端回读本语义subject，不因无关计数断言扩展本切片或弱化Gate |

## Auto-advance

- Candidate Gate与Agent Gate通过后只可归档G07并标记`VNEXT_CANDIDATE_READY_AGENT_VERIFIED`；
- 不自动创建或激活H1 Goal，不自动部署、公网、release、商业或合并`main`；必须等待用户明确批准。

## Completion record

- Status / Subject commits / Remote branch：`IN_PROGRESS / 第二候选周期repair-1已远端回读为3e1b0cd，语义修复已完成本地验证并待提交 / origin/codex/g07-candidate-cycle-2`；
- Verification / Evidence / Gate result / `structurally_valid`：`repair-1精确反例5 passed、扩大回归54 passed/1 service skip、Ruff、TypeScript与Next build PASS；正式Agent Gate受本机缺少协议允许的系统Git阻断，其余真实PostgreSQL、live、browser、performance、panel、sealed与最终Gate均NOT_RUN / LOCAL_AUTOMATED + UI_BUILD + FORMAL_GATE_BLOCKED_EXTERNAL / HARDENED_CANDIDATE_GATE_NOT_RUN / 合同fail-closed`；上一周期结果保持`AUTOMATED_TEST_PASS + LIVE_PROVIDER_EVIDENCE_PASS + MULTI_AGENT_SIMULATED_REVIEW_FAIL`且不可复用；
- H1 / production / commercial：`NOT_RUN / NOT_RUN / NOT_RUN`；H1、公网、生产、商业：`NOT_RUN`，release、deploy和`main`未请求；
- User-visible result / Remaining risks / Goal archived：`G06已交付；G07 repair-1已修复资料失败、stale map副作用、Provider生命周期、跨城、引用预约、非原子二选一和mixed-role / 必须在新exact tip执行扩大回归、真实服务、live/browser/performance与fresh panel；P4 Windows子进程一次非确定性超时和AMap两项既有计数断言差异仍保留 / false`；
- Next Goal activated：固定`NO_PENDING_HUMAN_APPROVAL`；
- Promotion decision：`NOT_REQUESTED`，除非用户另行批准H1。

## Stop conditions

- 需要新增产品功能才能通过；
- 需要降低任何Gate；
- 需要拼接历史证据；
- 需要新增Provider权限/费用，或隐私/事实矛盾只能通过改变Gate解决；
- 需要公网部署、H1、付费、release或`main`；
- 需要降低candidate blocker门槛而非继续技术诊断。
- 第一候选周期的停止条件已由`252f43d`触发，状态永久保留于`G07.final-panel-stop-checkpoint.json`；该历史状态不得被改写为PASS。
- 第二候选周期同一finding最多执行两种可验证修复策略；仍失败时采用诚实保守降级或请求owner决定，不得降低Gate。
- 第二候选周期final fresh panel若仍接受任何当前范围P0/P1，则候选继续FAIL并在sealed blind前停止。
