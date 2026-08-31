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
- Canonical implementation branch / worktree：`codex/g07-candidate` / `D:/munto/code/claudeProject/agentTravel-g07-candidate`，已从fresh `origin/develop`创建
- Upstream / remote readback：`origin/develop` / `ff36a10ecae98088742e9722da3f4bf3676f6d04`，2026-08-31 fresh fetch、`rev-parse`与`ls-remote`三方一致
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

当前registry只激活唯一集成者的G07候选加固切片。exact-binding切片已完成并由`G07.exact-binding.json`绑定；当前切片冻结候选RunSpec、G0～G8矩阵和威胁模型，移除自动合同中的历史pytest跳过与不存在命令，并让G07候选门向后兼容地读取当前v3治理绑定。任何未实际运行的层级保持`NOT_RUN/NOT_READY`。之后由集成者串行执行可靠性/隐私材料→性能收口→同commit全量E2E/Gate→`HardeningDecision`、manifest和远端readback。

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
- 候选实现baseline：`origin/develop@ff36a10ecae98088742e9722da3f4bf3676f6d04`；branch/worktree：`codex/g07-candidate` / `D:/munto/code/claudeProject/agentTravel-g07-candidate`；候选依赖锁、OpenAPI/migration/provider/model/dataset版本由后续RunSpec切片冻结；
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

## Auto-advance

- Candidate Gate与Agent Gate通过后只可归档G07并标记`VNEXT_CANDIDATE_READY_AGENT_VERIFIED`；
- 不自动创建或激活H1 Goal，不自动部署、公网、release、商业或合并`main`；必须等待用户明确批准。

## Completion record

- Status / Subject commits / Remote branch：`IN_PROGRESS / preflight e3db50d、exact-binding 2c90ea5、receipt 7324957、candidate-contract 91fee72、candidate-contract receipt 98f1f15、G1 repair 4afb862、G0-G1 receipt 34fc08d、G2 receipt 3685c6c、G3 receipt待提交 / origin/codex/g07-candidate@3685c6c远端回读一致`；
- Verification / Evidence / Gate result / `structurally_valid`：`G0定向38 passed；G1完整2556 passed, 44 skipped, 0 failed且Ruff PASS；G2 PostgreSQL 23 passed；G3 snapshot 31 passed且network_call_count=0 / LOCAL_AUTOMATED_POSTGRESQL_AND_FIXED_SNAPSHOT_CHECKPOINTS / HARDENED_CANDIDATE_GATE_NOT_RUN / true`；snapshot和checkpoint均不等于live或最终同subject候选通过；
- H1 / production / commercial：`NOT_RUN / NOT_RUN / NOT_RUN`；H1、公网、生产、商业：`NOT_RUN`，release、deploy和`main`未请求；
- User-visible result / Remaining risks / Goal archived：`G06已交付；G07已移除G04方案A执行路径、恢复普通后端零失败并冻结可执行候选合同 / G0～G8尚未在同一最终subject全部通过，live Provider、50链、复审、blind与最终HardeningDecision仍未运行 / false`；
- Next Goal activated：固定`NO_PENDING_HUMAN_APPROVAL`；
- Promotion decision：`NOT_REQUESTED`，除非用户另行批准H1。

## Stop conditions

- 需要新增产品功能才能通过；
- 需要降低任何Gate；
- 需要拼接历史证据；
- 需要新增Provider权限/费用，或隐私/事实矛盾只能通过改变Gate解决；
- 需要公网部署、H1、付费、release或`main`；
- 需要降低candidate blocker门槛而非继续技术诊断。
