# CURRENT GOAL：P5-G01 统一评测、隔离盲测与消融决策

## Metadata

- Goal ID：`TC-P5-G01-evaluation-ablation`
- Program ID：`TC-V1-INTERVIEW-2026`
- Phase：`P5`
- Status：`BLOCKED`
- Branch：`codex/trip-check-p5-evaluation-ablation`
- Baseline commit：`c8a5a0f6df3b4cef0d707742fa616eb5652ca6cc`
- P4 Gate subject：`85368777ca8d2d4e77cf053fc9a74018f9f9fc9a`
- Approved by / at：User / 2026-08-23
- Predecessor gate：P4 phase `PASS`；CP-SAT admission `REJECT`
- Required gate：`Evaluation Gate`

本 Goal 已获授权进入 P5 实现、物化、封存与正式 Evaluation Gate。用户于 2026-08-24 先批准 P5 v4 修复两条 non-blind 路线物化，随后在 v4 external oracle/payload 语义矛盾被 fail-closed 证实后，明确授权 P5 v5。P5 v5 只允许修正 external blind oracle 中已确认的 `specific_place_allowed` 语义矛盾；v4 的 label-free blind inputs/materializations 必须逐字节继承，v4 bundle、commitment、seal、运行和诊断保持不可变。v5 必须由隔离 custodian 生成新的 external bundle commitment，并由独立 reviewer 证明差分仅限获授权字段。任何 v4 正式运行、评分、Judge 或 Gate 均不能作为 v5 当前证据。

### P5 v4 remediation authorization（2026-08-24）

- 授权基线：`fbcad2509517fb8a1c0267cea441dda34d47cf8d`，其定向诊断已将 pilot hard miss 收敛到上述两条 `TRAVEL_TIME_GAP`；
- 唯一允许的数据修复：把两条 non-blind materialization 的路线时长恢复为对应 P1 tracked snapshot 中的 `90` 分钟，并重新计算由该变化传导的 non-blind hash/manifest；
- frozen blind：v3 inputs/materializations 作为 v4 envelope 的不可变字节源，内容、顺序、hash 集与外部 label/review commitment 均不得变化；
- 证据状态：v3 dataset/seal 只保留审计资格，已产生的 v3 run/score/Judge/Gate 一律为 `INVALID_EVIDENCE`；v4 在新 manifest、新 seal、active-contract readback 和同 commit 正式重跑完成前为 `RUNNING`；
- 推进边界：P5 Evaluation Gate PASS 前不得进入 P6；本授权不允许进入 H1、真人测试、生产 release、合并 `main` 或对外能力声明。

### P5 v5 oracle remediation authorization（2026-08-24）

- 授权基线：`d2ace7fdd50036b5aac3f314eeb7f160595c6d80`，其 fail-closed 守卫已把 v4 aggregate 的 60 个 deterministic/receipt failures 唯一归因为 external oracle 的 `specific_place_allowed` 与公开 label-free payload policy 不兼容；
- 唯一允许的 oracle 修复：隔离 custodian 按公开语义合同纠正已诊断的不兼容行；不得修改 case input、product materialization、expected terminal、Finding、route、receipt、difficulty、fault profile 或任何其他 oracle 字段；
- blind payload：v5 inputs/materializations 必须逐字节复制 v4，顺序、case ID、hash 集与 lineage 均不得变化；v4 external bundle 和 commitment 不得覆盖或改写；
- 重新封存：v5 使用新的 external bundle、commitment、custodian receipt、独立 review receipt、dataset manifest、seal 与 active contract；仓库只接收聚合计数和不可逆 hash，不接收 label/case 明细；
- 证据状态：v4 保留 `INVALID_EVIDENCE` 审计资格；v5 在同一 subject commit 完成正式 non-blind、一次性 blind、三轮 Judge、完整回归与 Evaluation Gate 前为 `RUNNING/NOT_RUN`；
- 推进边界：不得降低任何门槛；Evaluation Gate PASS 前不得进入 P6；本授权不允许进入 H1、真人测试、生产 release、合并 `main` 或对外能力声明。

### User-directed contraction checkpoint（2026-08-24）

- 用户要求收缩并记录当前成果，停止新增优化；因此本 Goal 从 `IN_PROGRESS` 转为 `BLOCKED`，不再自动实现、集成、封存、运行 blind/Judge/Gate 或推进 P6；
- 停止指令前，根分支已集成且已远端保存的最后一个功能/治理 checkpoint 为 `8dd67fd1f649cc79e442c3eb09468596aca9edb1`；该提交只记录 P5 v5 授权，不构成 v5 dataset、seal、run 或 Gate 证据；
- `codex/p5-v5-contract` 保留 10 个未跟踪草稿文件，`codex/p5-v5-runner-gate` 保留 27 个未跟踪草稿文件；两者均仍停在基线 `d2ace7fdd50036b5aac3f314eeb7f160595c6d80`，没有 commit、upstream 或可引用验证结果，未合并到当前分支；
- 隔离 custodian 只完成变换方案准备，没有生成 v5 external bundle、correction receipt、review receipt 或 commitment；没有读取结果回传到根代理，v4 external bundle/commitment 保持不可变；
- 上述 WIP 只作为恢复线索，不是已完成优化、正式证据或 Gate PASS。除非用户再次明确恢复本 Goal，否则不得继续处理或把 WIP 提升为 checkpoint。

### User-directed resume authorization（2026-08-24）

- 用户已明确要求实施 P5～P6 收口计划，因此解除本 Goal 的人工 `BLOCKED`，恢复为 `IN_PROGRESS`；
- 恢复只授权按下述顺序完成 P5 v5 最小合同、隔离 oracle 纠正、同 subject 正式重跑与 Evaluation Gate，不授权新增产品能力、评分规则、prompt、Provider、migration、生产依赖或基础设施；
- 两个 v5 worktree 仍是未验证草稿，必须经选择性集成、定向验证、diff 审计、clean commit 和远端 checkpoint 后才可成为当前实现；
- P5 Evaluation Gate 实际 `PASS` 前，P6、live Provider、公网部署、Candidate Gate 和 H1 继续保持 `NOT_STARTED/NOT_RUN`。

### Resume order and next target（仅记录，不执行）

1. **P5 v5 最小合同收口**：审阅并选择性集成两个 WIP worktree，只保留 v5 evidence envelope、byte-identity guard、oracle 差分白名单和 fail-closed tests；目标是得到 clean、pushed、pending-seal candidate commit，而不是增加新的评分规则或产品能力；
2. **隔离纠正与重新封存**：custodian 只修正获授权的 `specific_place_allowed` 矛盾，独立 reviewer 证明 60 条目标差分、0 条非目标差分和 blind payload/materialization byte identity；目标是新 commitment、review receipt、v5 seal 与 active contract 全部绑定同一 commit；
3. **P5 正式闭环**：同 subject 重跑 810 non-blind terminal/replay、270 blind terminal/replay、聚合评分、三轮独立 Judge、P1～P4 回归和 Evaluation Gate；目标是 Evaluation Gate 实际 `PASS`，否则保持 `REJECT/BLOCKED/INVALID_EVIDENCE`；
4. **P6 后续目标**：只有 P5 PASS 后才生成并激活 `TC-P6-G01-candidate-evidence`，执行同 commit G0～G6 与受控 snapshot 候选；目标仍是 Candidate Gate，而非 H1、正式发布、`main` 合并或真人测试。

### P5 v5 formal stop checkpoint（2026-08-24）

- v5 candidate-freeze 最终绑定 `691a0499cfb84e522dff8cca38b316e24e0024cc`；external correction/review 证明授权目标差分恰好 `60`、非目标差分 `0`、v4 label-free blind inputs/materializations 字节一致，privacy/disclosure findings 为 `0`。当前 sealed dataset manifest hash 为 `2ab59fc31814c45eb6110223773741813523d5ae695868c164767eb258dc9136`，seal 文件 SHA-256 为 `770468927e72d9357c0c504e90900749b846ad7f8519f90180c6c073a8e5a7be`；
- 正式运行先后暴露并 fail-closed 修复了两个只存在于真实产物集成中的 v5 Gate 合同缺口：blind lane schema 被错误按 non-blind schema 校验，以及 Gate 把 Judge projection hash 错当 source rubric canonical hash。修复 checkpoint 分别为 `8d1184aed113859c889fce7f5bc4344c3c8a301e` 和当前 subject `5d97775af2cd13001eeb1a1991a5ef0109443055`；所有更早 subject 的 run、score、Judge、verification 与 Gate 产物均为 `INVALID_EVIDENCE`；
- 当前 subject `5d97775af2cd13001eeb1a1991a5ef0109443055` 从 dataset formal validation 重新开始：non-blind `810/810` terminal/replay、blind `270/270` terminal/replay、replay mismatch `0`、blind label read `false`；isolated scorer 为 `PASS`，Core B=`100.0`、Solver C=`100.0`、Legacy A=`53.0`，零容忍 `11/11 PASS`，privacy count=`0`；
- 三轮 fresh、互相隔离、无 API Judge 已实际完成；panel report hash 为 `8d737dbf747600d785708bca62a10476a5844c04611f1c8763c6f7e715dcbb5c`，verdict agreement=`1.0`，但 `evidence_boundary_expression` agreement=`0.674074074074074`，低于 `0.85` 门槛，panel 状态为 `BLOCKED`。Core B 与 Solver C 的 majority pass 均为 `90/90`，Legacy A 为 `0/90`；该维度分歧不能通过重抽 Judge、修改 rubric 或临时补规则追绿；
- 当前 subject 的 P1～P3 verification receipt 为 `PASS`。P4 原命令两次在 CP-SAT worker 回读处被全局 pytest `60s` timeout 截断；诊断时宿主 CPU 为 `94%～98%`，`vmmemWSL` 占约 `27～29` 核，旧 PASS 同命令耗时 `47s`。失败 receipt 均保留；未改变 timeout、测试、实现或环境配置。由于 Judge 门槛已决定性失败，停止后续 P4 retry、完整 backend、Ruff、frontend、dual-entry、formal aggregate 与 Evaluation Gate；这些项目在当前 subject 下为 `NOT_RUN`；
- 当前 Goal 因有效 Judge agreement 失败转为 `BLOCKED/REJECT`，默认决策为 `REJECT_ALL_CANDIDATES`。P6 Goal 不生成，G0～G6、live Provider、公网候选、Candidate Gate、human evidence、H1、部署和 `main` 合并全部保持 `NOT_STARTED/NOT_RUN`。恢复不得复用本 subject 的部分证据；任何改变 Judge/rubric/Gate 或重新采样的方案都必须先取得新的明确授权并从新 subject 完整重跑。

### User-directed full retry authorization（2026-08-24）

- 用户明确要求“重试”，因此解除上述人工 `BLOCKED`，本 Goal 恢复为 `IN_PROGRESS`；本授权只允许在新的 clean、pushed subject 上按固定顺序完整重跑 P5，不允许单独重抽 Judge、复用 `5d97775...` 的 run/score/Judge/verification、查看 blind 明细或拼接部分证据；
- 产品代码、数据、external oracle、rubric、Judge/Gate 门槛、variant、prompt、Provider、依赖与基础设施全部冻结；若重试仍出现质量门槛失败，保持 `REJECT/BLOCKED`，不得通过改规则追绿；
- 正式顺序仍为 dataset formal validation → 810 non-blind terminal/replay → non-blind score → 一次性 nonce → 270 blind terminal/replay → isolated aggregate → 三轮 fresh 独立无 API Judge → panel aggregate → P1～P4、完整 backend、Ruff、frontend、dual-entry → formal receipt → Evaluation Gate；
- 只有本次新 subject 的 Evaluation Gate 实际 `PASS` 后才允许生成 P6 Goal；本授权仍不包含公网 schema、部署、live Provider、H1、真人、release、`main` 合并或任何对外声明。

### P5 v5 full retry stop checkpoint（2026-08-24）

- 新的 clean、pushed formal subject 为 `7cecf5d62e5015aa777b167530aef8a9b971de07`；本次没有复用 `5d97775...` 的 run、score、Judge 或 verification。dataset formal validation、non-blind `810/810` terminal/replay、blind `270/270` terminal/replay 均为 `PASS`，replay mismatch=`0`，blind label read=`false`；
- isolated aggregate 为 `PASS`：Core B=`100`、Solver C=`100`、Legacy A=`53`，零容忍检查 `11/11 PASS`，privacy count=`0`。blind aggregate 文件 SHA-256 为 `6c27fa86899793b4f73f35163dd6954d9c35d88a548ba2404d6e4c2bb39a4cb2`，report hash 为 `c100bd9a0d6807e9ebbe845a5c934c79b38ae25d00c08295af67855f8e6b9239`；
- 三轮有效 Judge 均为 fresh、互相隔离且无 API：round 1/2/3 文件 SHA-256 分别为 `93645444eeca38ffe251ab4c75e203526711eb02b8985e9ef371de5459aa85b9`、`a57e91bc01482bd659b93bdbbbb3c3812f9e537d7712a2bc521a0ef6a93e2f83`、`c4f99c35d08cb8e5818978a29517a705724221ef846247939b48a0dee1e97b30`。首次 round 3 在产出结果前自报读到旧治理聚合信息，已立即作废并由新的隔离 evaluator 重做，作废轮次未参与聚合；
- 有效 panel report hash 为 `6c8f6cac5439647aff8859b4a866e8c2824e5df545eaed6cf069d04d270f271d`，状态为 `BLOCKED`：verdict agreement=`0.3333333333333333`、actionability agreement=`0.6444444444444445`、clarity agreement=`0.6444444444444445`，均低于 `0.85`；evidence-boundary agreement=`1.0`，unsupported candidates=`0`。Core B majority pass=`90/90`，但 unanimous rate=`0`，不能据此覆盖一致率硬门槛；
- P1～P4 verification 均为 `PASS`；P4 在宿主低负载下沿用原命令和 `60s` timeout，于 `42s` 完成，证明前一 subject 的两次 P4 timeout 属于宿主资源竞争，不是产品或测试修复成果；
- panel 已形成决定性质量失败后，按停止条件中断正在运行的完整 backend suite；当前 subject 的完整 backend 为 `INTERRUPTED/NOT_COMPLETED`，Ruff、frontend build、dual-entry、formal aggregate 和 Evaluation Gate 为 `NOT_RUN`。不重抽 Judge、不改 rubric/Gate、不拼接旧证据；Goal 回到 `BLOCKED/REJECT`，默认决策为 `REJECT_ALL_CANDIDATES`，P6 不生成。

## Outcome

在同一个 commit、RunSpec、数据合同和 oracle 下，对以下三个候选系统完成可重放的 360 例对照评测，并得到一份能解释“为什么保留或改变默认方案”的消融结论：

- `Legacy A`：冻结的 Router → Planner → Critic 旧链，只通过只读适配器参加比较；
- `Core B`：当前权威 TripCheck 主链，Repair 策略固定为 `bounded_repair_v1`；
- `Solver C`：与 Core B 使用相同输入、Evidence、Audit 和 postcheck，仅把 Repair 策略替换为隔离的 `cp_sat_v1` 实验路径。

最终交付不是一个笼统分数，而是四类可回读产物：

1. 18 pilot / 180 dev / 72 regression / 90 frozen blind 的版本化数据与隔离证明；
2. A/B/C 每例的产品输出、Trace、receipt、耗时、token/成本和 replay hash；
3. 确定性 oracle 评分与无 API 的独立 Judge 辅助评分；
4. 默认运行时决策、失败分桶、可复现命令和 P5 Gate manifest。

P5 只回答“哪个候选在当前固定范围内更可靠、代价更合适，以及证据是否足够”。它不把自动 Judge、controlled snapshot 或消融结果冒充 live Provider、公开或真人证据。

## Scope

- 新增 `backend/evals/trip_check_v1/p5/` 下的数据合同、A/B/C 适配器、runner、scorer、blind seal 和报告生成器；
- 新增与 P5 对应的定向测试、Gate runner 和 evidence readback；
- 将 P4 的 18 pilot、180 dev、72 regression 原文件与 hash 只读纳入 P5 manifest；另建可执行的 P5 归一化输入层并新增 90 条 frozen blind，使总数达到 360，三城各 120；
- 冻结统一 case contract、RunSpec、metric definitions、预算、fault profile、variant version 和 seed；
- 对三个候选运行相同输入和 oracle；不支持的能力必须输出机器可读的 `UNSUPPORTED_CAPABILITY` 并计为失败，不能静默跳过；
- 建立 blind 输入、标签 commitment、外部 label bundle 和隔离 scorer 的 fail-closed 链；
- 对事实正确性、Finding、UNKNOWN、CandidateSet/receipt 和 postcheck 使用确定性评分；对表达清晰度、行动性和 unsupported claim 使用独立 Judge 辅助评分；
- 输出 paired comparison、分桶指标、P50/P95、失败类、replay 一致性和默认方案决策。

允许修改的范围固定为：

- `backend/evals/trip_check_v1/p5/**`；
- `backend/scripts/*trip_check_p5*`；
- `backend/tests/*trip_check_p5*` 及 blind 隔离回归；
- `backend/evidence/trip_check_v1/p5/**`；
- 本 Goal、P5 评测说明和必要的 manifest/version 配置。

如发现现有只读适配器不足，可在 `backend/evals/**` 内做兼容性收敛；不得借 P5 重构产品主链。

## Non-goals

- 不扩城、不跨城，仍只支持北京、上海、杭州的 2～5 人、2～5 天单城市行程；
- 不新增产品运行时 Agent、RAG、GraphRAG、消息队列、Kubernetes、模型微调或评测后台；
- 不修改公共 HTTP API/schema，不新增 migration，不增加生产依赖；
- 不调用 LLM Judge API，不使用付费 Provider，不扩大外部数据来源；
- 不因 P5 结果重新打开 P4 frozen bake-off/oracle；CP-SAT 的 P4 `REJECT` 必须带入 P5；
- 不用 blind 失败直接调参、补规则或改标签；失败只能生成不泄漏标签的 dev/regression 复现；
- 不运行或宣称 G4 live Provider、P6 Candidate Gate、public E2E、release、H1 或真人证据；
- 不合并 `main`，不部署，不改变仓库可见性。

## Authority

- `AGENTS.md`；
- `docs/product/PROJECT_CHARTER.md`；
- `docs/product/TRIP_CHECK_SPEC.md`；
- `docs/product/TRIP_CHECK_API_CONTRACT.md`；
- `docs/governance/PORTFOLIO_MISSION.md`；
- `docs/governance/PROGRAM.md`；
- `docs/governance/ROADMAP.md`；
- `docs/governance/RELEASE_GATES.md`；
- 已完成的 `TC-P4-G01-advice-candidate-repair` 与其 P4 Gate manifest。

冲突时服从上述权威顺序。旧 dual-entry、RAG、Router blind、RAGAS 或历史 Judge 资产只能作为实现参考，不能直接计为 P5 证据。

## Contract versions

- Active target：`trip-check-p5-v5`；dataset manifest、RunSpec、adapter/output、score、Judge、blind seal 与 Gate manifest 使用独立 v5 envelope，不能覆盖 v4 文件；case/materialization 继续由冻结 schema 校验，v5 non-blind 与 label-free blind payload/materialization 逐字节复制 v4，仅 external blind oracle bundle 允许发生上述授权差分；
- P5 v1/v2：`SUPERSEDED`，只保留审计资格，任何 v5 formal runner/scorer/Gate 必须拒绝；
- P5 v3：`INVALID_EVIDENCE`，保持不可变；不能复用其 seal、external commitment、non-blind manifest 或任何正式结果；
- P5 v4：`INVALID_EVIDENCE`，保持不可变；v5 只继承其 label-free dataset 字节，不继承 external commitment、seal 或任何正式结果；
- Dataset increment：`+90 frozen_blind`，总计 `360`；
- Evidence output：`${P5_ARTIFACT_ROOT}/p5-v5-formal/<subject_commit>/`（仓库外只读目录）；正式产物不写入 tracked tree，避免 Gate 自引用改变 subject commit。`.local-artifacts` 只允许非正式诊断，不得进入 formal receipt。归档提交只记录 subject、artifact hash 与外置路径，不改变被评测代码/合同/数据。

任何合同在首次正式 A/B/C 运行后不得原地修改；必须提升版本并使旧运行失效。

## Baseline

- 分支/commit：P5 v5 remediation 起始基线为 `d2ace7fdd50036b5aac3f314eeb7f160595c6d80`；当前已远端保存 v5 envelope WIP `ff58268750decb9c79065b9da941768961a810c5`、formal artifact/nonce 回读 `f509175a6e58950933e0038dca55b6fcb3bfcf0e`、原子 seal 实现 `64ff361c5dc539607a62f78d02fcc10dfcb73372`、candidate-freeze `aee7e9af632a6e8f470a71fbf5358dd1cda71923` 与 seal activation `610a4f2805e4578dc633340981aaa21aff6dc659`；
- 已有数据：18 pilot、180 dev、72 regression、90 frozen blind；P5 v4 label-free payload/materialization 是 v5 的不可变输入约束，标签 payload 只存在于仓库外隔离 bundle；
- 已有 P4 结论：`bounded_repair_v1` 成功率 66.7%，`cp_sat_v1` 50.0%，CP-SAT admission `REJECT`；
- 已有评测资产：通用 EvaluationRunner、旧 adapters、blind fail-closed scorer、Judge panel 脚本和 P1～P4 runner；它们尚未组成 P5 的 TripCheck 360 A/B/C Gate；
- 已记录但本轮未重跑：P4 completion record 中 backend `1313 passed, 28 skipped`、Ruff、frontend build、PostgreSQL、浏览器、18 pilot、P2/P3 regression 和 P4 manifest 均 PASS；
- 当前证据等级：controlled fixture 与既有 P1～P4 证据仍按其原 subject 保留；P5 v3/v4 及 `7cecf5d62e5015aa777b167530aef8a9b971de07` 之前的 v5 run/score/Judge/Gate 均不构成本次 Gate 证据。v5 dataset/external correction/review/seal 为 `PASS`；当前 subject 的 terminal/replay、deterministic aggregate 与 P1～P4 verification 为 `PASS`，Judge panel 为 `BLOCKED`，完整 backend 为 `INTERRUPTED/NOT_COMPLETED`，Evaluation Gate 为 `NOT_RUN`，Goal/candidate readiness 为 `REJECT`。G4 live Provider、public E2E、human evidence 为 `NOT_RUN`。

## Invariants

- AuditEngine 始终是 Finding 唯一权威；Legacy、Solver 和 Judge 都不能删除、降级 Finding 或把它标记为已解决；
- `UNKNOWN/UNAVAILABLE` 不得计为 PASS；预期为 UNKNOWN 的字段若被候选系统猜测为确定事实，计入安全失败；
- 错城/错 POI 自动接受、HARD 漏检、虚构已验证候选、缺少地点或路线 receipt、Repair 新增 BLOCKER/HIGH/UNKNOWN 均为零容忍；
- CandidateSet、EvidenceSnapshot、revision、postcheck 和 receipt lineage 必须可回读；
- A/B/C 使用相同 case input、RunSpec、oracle、Provider snapshot/fault profile、budget 和 seed；只允许 variant adapter 与明确列出的策略差异；
- Legacy A 只能只读执行，不得写权威 workspace/revision 或被包装为 V1 权威链；
- Solver C 继续在隔离子进程运行，且 P4 admission `REJECT` 不因 P5 总分较高而自动解除；
- frozen blind 输入可供产品 runner 使用，标签只存在于仓库外 bundle；仓库只保存 case IDs、commitment、seal 和 aggregate score；
- 开发 runner、运行模型、Judge 和普通测试在读任何 blind 标签前必须 fail closed；
- Judge 只评价语义表达，不裁决 POI、路线、天气、Finding 或 postcheck 等确定性事实；与 oracle 冲突时以 oracle 为准；
- token、成本和耗时必须来自真实计量或明确写 `NOT_MEASURED`，不得估算后冒充实测；
- 任一代码、配置、数据、oracle、prompt 或 variant 变化都使旧 Gate manifest 失效。

## Metric contract

每个 variant 至少输出以下指标，并保留 case-level 原始结果供非 blind 分析：

| 指标 | 判定来源 | 关键规则 |
|---|---|---|
| `task_success` | 确定性 oracle | 必须完成该 case 的必需链路，且无零容忍失败 |
| `wrong_city_or_poi` | receipt + oracle | 非零即阻断 |
| `hard_finding_miss` | Audit oracle | 非零即阻断 |
| `unknown_preservation` | oracle | 预期 UNKNOWN 不得被猜成 PASS |
| `repair_postcheck_success` | revision + report lineage | 新 revision 完整 postcheck 且不新增严重 Finding |
| `unsupported_claim_rate` | 规则 + Judge 辅助 | Judge 不能覆盖规则判定 |
| `candidate_receipt_coverage` | CandidateSet/receipt | 具体地点必须 100% 可追溯 |
| `replay_hash_match` | replay | 固定 snapshot 重放 100% 一致 |
| `latency_ms` | runner receipt | 报告 P50/P95，超时单独计数 |
| `token_count / cost` | 调用 receipt | 无调用为 0；不可计量则 `NOT_MEASURED` |

汇总必须同时给出 overall、三城、输入类型、难度、Finding 类型、fault profile 和 repair 类型分桶；不能用 overall 掩盖硬门槛失败。

## Acceptance cases

### A. 数据与隔离

- 数据精确为 18 pilot / 180 dev / 72 regression / 90 frozen blind，共 360；北京、上海、杭州各 120；
- pilot/dev/regression 源文件沿用 P4 冻结 hash；P5 归一化层必须为三者生成可执行 product input、显式 lineage 和独立 input hash，不得靠添加 split 前缀掩盖内容重合；
- P4 的 72/72 fixture/oracle 重合作为 `legacy_overlap_debt` 写入 manifest；P5 的 normalized product input、content family 和 mutation ancestry 跨 split 重合必须为 0；
- frozen blind 每城 30，覆盖文本/截图、clean/medium/hard、地点歧义、路线、酒店、天气/风险、偏好/强度、Advice/Repair 和固定 fault profile；
- 同源/变异案例不跨 split；重复、分布、oracle 完整性、隐私和 secret scan 全部 PASS；
- 首次候选运行前冻结 blind inputs、case ID commitment、label commitment、RunSpec 和 seed；之后修改任一项必须使 Gate `REJECT`；
- 仓库内不存在 blind label payload、答案路径或可从 seal 逆推出标签的明细；isolated scorer 只返回聚合结果和 Gate 状态。

### B. A/B/C 公平执行

- 三个 variant 对 360 个 case 均产生一条终态输出，共 1080 条；异常、超时和不支持能力也必须落机器可读终态，禁止缺行；
- 三个 variant 的 RunSpec 除 `variant_id / adapter_version / repair_strategy` 外完全一致；
- 每个运行都绑定 commit、dirty tree、dataset、case IDs、config、model/prompt、rule、provider snapshot、fault、budget、seed 和 output hash；
- 相同 snapshot 重放结果 hash 100% 一致；重复运行不会复用旧输出拼接新报告；
- 截图 case 的正式执行固定为 `FROZEN_ACTUAL_OCR_RECEIPT_REPLAY`：actual PaddleOCR 3.7.0 只由冻结 materialization/formal receipt 证明；本轮仍执行 render、临时写入、图片字节 hash 命中、产品解析与本轮 cleanup，但 `fresh_actual_ocr_execution=NOT_RUN`、fresh prediction=0。run manifest 与 terminal 必须邻接记录精确 preload/hit/miss/fallback/receipt-match/cleanup 计数，任一 miss、fallback、fresh prediction 或计数漂移使 Gate REJECT；
- Legacy A 的不支持项计为失败，并单独显示能力边界；不得通过预先喂入 Core B 结果伪装为 Legacy 能力。

### C. Core B 最低质量线

- 零容忍项全部为 0；
- 综合分 ≥88，地点与城市事实 ≥90，时间/路线/酒店衔接 ≥90，其他建议性分桶均 ≥80；
- 非 PASS Finding 行动建议覆盖率 100%，具体候选 CandidateSet/receipt 覆盖率 100%；
- 18 pilot 继续 18/18 PASS；72 regression 全部产生符合 oracle 的终态。P4 只证明了这 72 条的数据合同，P5 首次正式运行后才建立可比较的执行基线，不能倒推“P4 已执行通过”；
- blind aggregate 未达到同一硬门槛时 P5 Gate `REJECT`，但不得查看 case label 或改 blind/oracle 追绿。

### D. 默认方案决策

- 先比较零容忍、安全、UNKNOWN 和 postcheck，再比较 paired task success，最后比较 P95、token/成本和实现复杂度；
- challenger 只有在所有安全硬门槛通过、paired improvement 的置信区间不跨 0，且没有重要分桶回归时才有资格替代 Core B；
- 若效果差异不确定或持平，选择更简单、可解释、成本更低的 Core B；
- Solver C 即使 P5 指标更高，也必须先满足既有 Solver Admission Gate 才能成为默认；本 Goal 不修改 P4 frozen bake-off/oracle；
- 最终报告必须明确写 `KEEP_CORE_B / PROMOTE_ADMITTED_CHALLENGER / REJECT_ALL_CANDIDATES` 之一，并给出逐门槛证据。

### E. Independent Judge

- Judge 输入只含脱敏 case、候选输出、允许引用的 Evidence 摘要和 rubric，不含 variant 名称、确定性 oracle、blind label 或其他 Judge 结论；
- 使用三个不同 evaluator ID/round 的独立 Codex 子代理评审，不调用外部 LLM API；运行时模型不得评价自己的输出；
- 聚合报告保留分歧率、一致率、错误数和 majority 结果；一致率门槛为 ≥85%；
- Judge 结论标记为 `automated_proxy_judge`，`human_calibration_performed=false`；
- Judge 失败或分歧只能阻断语义评分，不能把确定性失败改为通过，也不能晋级为真人证据。

## Execution plan

### Milestone 0：激活、集成基线与合同冻结（第 1～2 天）

- 本 Goal 已由现场目标激活为 `IN_PROGRESS`；
- 确认 P4 final checkpoint 已 fast-forward 到 `codex/trip-check-v1-program`，再固定 P5 baseline；
- 盘点旧 eval/blind/Judge 资产，只复用经过 contract test 的部分；
- 冻结 metric definitions、A/B/C 差异白名单、RunSpec、case schema、预算和 evidence index；
- Checkpoint：合同测试、manifest readback 和基线审计 PASS。

### Milestone 1：90 条 blind 的隔离生产与封存（第 3～5 天）

- 由开发子代理生成候选输入，另一独立子代理复核覆盖、同源隔离、隐私和 oracle；
- blind custodian 将 label bundle 保存在仓库外，只把不可逆 commitment、seal 和 inputs 写入仓库；
- 开发 runner 在任何标签读取前用测试证明 fail closed；
- Checkpoint：360 数据合同、三城 120/120/120、blind leak scan 和 tamper tests PASS。

### Milestone 2：A/B/C 统一适配器与可重放 runner（第 6～9 天）

- 实现三个只读 variant adapter 和统一终态 schema；
- 先在 18 pilot 上验证公平输入、异常落盘、timeout、replay 和无权威写入；
- 再跑 180 dev + 72 regression；发现实现缺陷只修 dev/regression，不触碰 blind；
- Checkpoint：270 non-blind × 3 全部产生终态输出，零缺行，replay 100% 一致。

### Milestone 3：确定性评分与失败诊断（第 10～12 天）

- 实现事实、Finding、UNKNOWN、CandidateSet、repair/postcheck 和 receipt 的确定性 scorer；
- 输出 paired comparison、置信区间、三城/难度/故障分桶和失败分类；
- 对 non-blind 失败形成可复现命令；真实修复追加 regression 并重新冻结版本；
- Checkpoint：Core B 在 non-blind 达到最低质量线，旧 P1～P4 回归持续 PASS。

### Milestone 4：一次性 blind 执行与独立 Judge（第 13～15 天）

- 先冻结 A/B/C 的 270 non-blind 版本、commit 和 config；
- 三个 variant 运行 blind inputs 并封存产品输出，之后才由隔离 scorer 读取外部 bundle；
- 对允许 Judge 的语义维度执行三轮独立、无 API 的 blind review；
- blind 只产出聚合分桶和 Gate 结论；失败不得回看明细调参；
- Checkpoint：blind binding、输出 hash、Judge provenance、aggregate score 和 leak scan 可回读。

### Milestone 5：消融决策与 Evaluation Gate（第 16～20 天）

- 生成 A/B/C 消融表、失败类型表、成本/性能表和默认方案决策；
- 同 commit 重跑完整 backend、Ruff、frontend build、P1～P4 regression、P5 dataset/runner/scorer/blind isolation；
- 在仓库外 `${P5_ARTIFACT_ROOT}/p5-v5-formal/<subject_commit>/p5_gate_manifest_v5.json` 生成 Gate manifest，并执行 artifact/hash/readback/schema/secret scan；仓库内仅允许 `.local-artifacts` 作为非 tracked 的诊断输出位置；
- Gate PASS、clean tree、远端 checkpoint 和 evidence readback 全部成立后，才可归档 P5 并生成 P6 draft；不得自动进入公网或候选发布。

Gate 采用两步 envelope：先在 clean subject commit 上生成外置只读证据；PASS 后只允许一个 governance-only 归档提交记录 subject、manifest hash 和外置路径。归档 diff 白名单仅允许 Goal/completed-goal 状态文件，不得改代码、配置、数据、oracle、prompt、variant 或 frozen contract；否则旧 Gate 立即失效并必须重跑。

计划窗口为 4 周或 20 个专注开发日，以 Gate 结果而不是日历日期判定完成。每个切片仍不得超过 60 分钟没有可恢复的本地与远端 checkpoint。

## Verification

计划新增并实际执行以下层级；命令在实现后以脚本 `--help` 和 Gate manifest 中记录的最终版本为准：

```powershell
python -m pytest backend/tests -q -k "p5 and v5"
python backend/scripts/validate_trip_check_p5_dataset_v5.py --formal
python backend/scripts/run_trip_check_p5_v5_nonblind.py --output-root <EXTERNAL_ROOT>
python backend/scripts/score_trip_check_p5_v5_nonblind.py --run-dir <RUN_DIR> --output <EXTERNAL_JSON>
python backend/scripts/correct_trip_check_p5_v5_external_oracle.py --help
python backend/scripts/review_trip_check_p5_v5_external_oracle.py --help
python backend/scripts/seal_trip_check_p5_blind_v5.py --help
python backend/scripts/mint_trip_check_p5_v5_blind_nonce.py --output <EXTERNAL_NONCE> --receipt-output <EXTERNAL_MINT_RECEIPT>
python backend/scripts/run_trip_check_p5_v5_blind.py --output-root <EXTERNAL_ROOT> --consumption-dir <EXTERNAL_DIR> --nonce-file <EXTERNAL_NONCE> --run-id <RUN_ID>
# isolated custodian only: score_trip_check_p5_v5_blind.py
# three isolated rounds: export/aggregate_trip_check_p5_v5_judges.py
python backend/scripts/manage_trip_check_p5_v5_receipts.py --help
python backend/scripts/run_trip_check_p5_v5_gate.py --help

cd backend
python -m pytest tests/ -q
python -m ruff check app evals scripts tests

cd ../frontend
npm run build

cd ..
python backend/scripts/validate_dual_entry_testset.py
```

必须回读：

- dataset manifest、blind seal、external bundle commitment；
- 1080 条 variant 终态输出计数与 hash；
- non-blind case-level scores、blind aggregate score；
- RunSpec、variant diff whitelist、Trace/receipt/replay；
- Judge 三轮 provenance、panel agreement 和 `human_calibration_performed=false`；
- P5 Gate manifest 的 subject commit、dirty tree、artifact index、sha256 和 secret scan。

P5 完成时仍必须明确保持：G4 live Provider、P6 G0～G6 同 commit 候选重跑、public E2E、release 和 human evidence 为 `NOT_RUN/REJECT`，除非后续 Goal 获得独立授权并实际执行。

## Budget and checkpoints

- 外部 API/LLM 增量费用：0；Judge 使用开发子代理，不调用 API；
- Provider：只使用冻结 snapshot/controlled fixture；不运行新的付费或扩大范围的 live Provider；
- 数据：只新增 90 frozen blind；不扩大城市、人数、天数和输入类型；
- 运行规模：3 variant × 360 case = 1080 个终态输出；重试只针对 runner 基础设施故障，每 case 最多 1 次，并保留首轮失败 receipt；
- 任何正式输出变更都必须提升 run ID，不得覆盖旧 evidence；
- 每个 milestone 至少一个独立 checkpoint；每个切片执行“定向验证 → diff/staged diff → 显式暂存 → `git diff --cached --check` → commit → push”。

## Pre-approved actions

- P5 固定范围内的 eval 代码、测试、90 blind inputs/seal、synthetic/dev/regression 复现、独立复核、无 API Judge、隔离本地服务、Gate 和 evidence 生成；
- 复用已有 schema/API、P1～P4 数据与 snapshot，不新增 migration；
- 在 P5 开发分支 checkpoint commit/push；P5 Gate PASS 后按 Program 规则 fast-forward 到集成分支；
- 连续两个切片无法改善同一门禁时，执行一次独立故障诊断。

本节已随 Goal 进入 `IN_PROGRESS` 生效。

## HITL and stop conditions

以下任一情况立即停止自动推进：

- 需要新增/修改公共 schema/API、migration、生产依赖或基础设施；
- 需要真实/付费 Provider、新账号、绑卡、扩大外部数据范围或产生增量费用；
- blind label 泄漏、v5 与 v4 label-free blind payload/materialization 不一致、v5 oracle 出现超出本次授权字段的差分、独立 review 失败、同源跨 split，或再次需要修改 blind/oracle 才能追绿；
- A/B/C 无法在同一输入/RunSpec 下公平比较，或只能通过把 Core 结果喂给 Legacy 才能完成适配；
- Judge 被要求裁决确定性事实、运行时模型自评，或一个模型成为唯一 Judge；
- Solver 出现新增 BLOCKER/HIGH/UNKNOWN，或需要绕过 P4 admission 才能晋级；
- 连续两个切片不能改善同一门禁，独立故障诊断后仍需扩大范围或降低 Gate；
- evidence 绑定矛盾、成本超限、隐私事故或 secret 泄漏；
- P5 Evaluation Gate PASS 前请求进入 P6；请求进入 H1、真人测试、合并 `main`、production release 或超出已授权蓝绿 snapshot 候选范围的 deploy。

## Auto-advance

- Required gate：`Evaluation Gate`；
- Next Goal template：`TC-P6-G01-candidate-evidence`；
- 自动生成 P6 draft 必须同时满足：本 Goal 获批、全部 acceptance PASS、clean tree、P5 commit 已推送且 upstream 可确认、evidence 可回读、无 Stop condition；
- 生成 P6 draft 不等于获批公网、live Provider、release、H1 或合并 `main`。

## Completion record

- Commits：P5 v4 数据、runner/scorer、blind/Judge/Gate、正式 receipt、seal 与 active contract 已形成不可变 checkpoint；P5 v5 candidate-freeze `aee7e9af632a6e8f470a71fbf5358dd1cda71923` 已远端保存，并完成 label-free envelope、runner/scorer/Judge/Gate、formal artifact/nonce 原文件回读、oracle correction/review 以及 seal/active-contract 原子切换；
- Remote branch / upstream：`codex/trip-check-p5-evaluation-ablation` / `origin/codex/trip-check-p5-evaluation-ablation`；每个已报告 checkpoint 均在 push 后回读 HEAD/upstream；
- Verification results：subject `34ac550731a0ff6d8414b42be189110b5f5652f2` 的 formal dataset validation 为 `PASS`；270 non-blind × 3 得到 810/810 terminal 与 replay，Core B 270/270、综合分 100、全部 non-blind 硬门槛 `PASS`；90 blind × 3 得到 270/270 terminal 与 replay，但 isolated aggregate 的 Core B `deterministic_failure_count=60`、`candidate_receipt_failure_count=60`，其余零容忍计数均为 0，故原 aggregate 为 `REJECT` 且 Judge 未运行；
- Failure diagnosis：blind custodian 只输出聚合分类，60/60 均为 `specific_place_policy_mismatch`；`projection_loss=0`、CandidateSet hash mismatch=0、产品 terminal receipt propagation failure=0、未解释 scorer failure=0。根因是 sealed external oracle 与 label-free payload 的地点许可语义矛盾，状态升级为 `INVALID_EVIDENCE`，不是 Core B 产品失败；诊断 receipt `blind_failure_diagnostic_v4.json` 的文件 SHA-256 为 `06cf558ff16dced7187b3435c30c29bf83235bcf8548452cbe06205c10e04a4d`，内容 hash 为 `e168af5476c8f7afbedb042872ea86d28b1d48200ebf527f72c77bfbeb1196c4`，disclosure scan `PASS`；
- Regression verification：候选 receipt 正向与三类缺失变异联跑 `22 passed`；oracle/payload 守卫、blind scorer、v4 receipt regression 与 non-blind scorer 联跑 `48 passed`；对应 Ruff `PASS`。守卫会在 case scorer 前把任何不兼容 external oracle 统一判为 `BLIND_ORACLE_PAYLOAD_SEMANTIC_MISMATCH / INVALID_EVIDENCE`，不输出 case/label 明细；
- V5 custody/seal verification：初始 candidate-freeze 的 v5 contract 定向矩阵 `61 passed`、Ruff `PASS`；Custodian/Reviewer 聚合证明目标差分 `60`、非目标差分 `0`、privacy/disclosure findings `0`、blind payload 未变。该初始 seal 后因 Windows canonical hash 绑定修复而失效；当前有效 candidate-freeze、manifest 与 seal 以 `P5 v5 formal stop checkpoint` 记录的 `691a0499...`、`2ab59fc3...` 和 `77046892...` 为准；
- V5 formal failure diagnosis：activation subject `610a4f2805e4578dc633340981aaa21aff6dc659` 曾完成 dataset formal receipt、810/810 non-blind terminal/replay、270/270 blind terminal/replay 与 isolated blind aggregate；Core B 两条 lane 均为满分且零容忍项为 0。但 Judge export 因 exporter 错误拒绝合同允许的 `postcheck=None` 而 fail-closed；聚合只读诊断证明 270/270 terminal schema PASS、230 个 postcheck 为合法 None、40 个为 Mapping。修复后该 subject 的全部 formal 产物自动失效，不得补导或复用；对应 Judge/exporter 定向验证 `23 passed`、Ruff `PASS`；
- Evidence boundary：上述旧正式运行只绑定各自旧 subject，不能拼接到当前 Gate；v3/v4 blind payload 与各自 external bundle commitment 保持不可变；当前 v5 commitment/seal 仍绑定 candidate-freeze `691a0499cfb84e522dff8cca38b316e24e0024cc`。只有 subject `7cecf5d62e5015aa777b167530aef8a9b971de07` 的本轮产物保留当前诊断资格，且其 Judge panel 已 `BLOCKED`；
- Gate result：P5 v4=`INVALID_EVIDENCE`；P5 v5 Goal=`BLOCKED`、Judge panel=`BLOCKED`、Evaluation Gate=`NOT_RUN`。本次 verdict/actionability/clarity agreement 分别为 `0.3333333333333333`、`0.6444444444444445`、`0.6444444444444445`，均低于 `0.85`，因此 Evaluation Gate 未运行，P6=`NOT_STARTED`；
- Next Goal generated：`NO`；
- Remaining red lights：当前 subject 已完成完整 non-blind/blind、deterministic score、三轮有效 Judge 与 P1～P4 verification，但 Judge agreement 门槛失败；完整 backend 已按停止条件中断，Ruff/frontend/dual-entry/formal aggregate/Evaluation Gate 未执行。P6 G0～G6、live Provider、公网候选、Candidate Gate、human evidence 全部保持 `NOT_RUN`；
- Promotion decision：`REJECT_ALL_CANDIDATES`（Evaluation Gate 未成立，不能晋级或自动生成 P6 Goal）。
