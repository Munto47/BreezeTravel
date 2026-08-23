# CURRENT GOAL：P5-G01 统一评测、隔离盲测与消融决策

## Metadata

- Goal ID：`TC-P5-G01-evaluation-ablation`
- Program ID：`TC-V1-INTERVIEW-2026`
- Phase：`P5`
- Status：`IN_PROGRESS`
- Branch：`codex/trip-check-p5-evaluation-ablation`
- Baseline commit：`c8a5a0f6df3b4cef0d707742fa616eb5652ca6cc`
- P4 Gate subject：`85368777ca8d2d4e77cf053fc9a74018f9f9fc9a`
- Approved by / at：User / 2026-08-23
- Predecessor gate：P4 phase `PASS`；CP-SAT admission `REJECT`
- Required gate：`Evaluation Gate`

本 Goal 已获授权进入 P5 实现与测试；授权不包含修改 frozen blind/oracle、进入 P6、公网或真人阶段。

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
- 将 P4 的 18 pilot、180 dev、72 regression 只读纳入 P5 manifest，新增 90 条 frozen blind，使总数达到 360，三城各 120；
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

- Case contract：`trip-check-p5-eval-case-v1`；
- Dataset manifest：`trip-check-p5-dataset-manifest-v1`；
- RunSpec：`trip-check-p5-run-spec-v1`；
- Variant adapter：`trip-check-p5-variant-adapter-v1`；
- Deterministic score：`trip-check-p5-score-v1`；
- Judge bundle：`trip-check-p5-judge-bundle-v1`；
- Blind seal / external bundle：`trip-check-p5-blind-seal-v1` / `trip-check-p5-blind-bundle-v1`；
- Gate manifest：`trip-check-p5-gate-manifest-v1`；
- Dataset increment：`+90 frozen_blind`，总计 `360`；
- Evidence output：`backend/evidence/trip_check_v1/p5/`。

任何合同在首次正式 A/B/C 运行后不得原地修改；必须提升版本并使旧运行失效。

## Baseline

- 分支/commit：规划分支基于 P4 evidence checkpoint `c8a5a0f6df3b4cef0d707742fa616eb5652ca6cc`；制定本 Goal 前工作树为 clean；
- 已有数据：18 pilot、180 dev、72 regression、0 frozen blind；
- 已有 P4 结论：`bounded_repair_v1` 成功率 66.7%，`cp_sat_v1` 50.0%，CP-SAT admission `REJECT`；
- 已有评测资产：通用 EvaluationRunner、旧 adapters、blind fail-closed scorer、Judge panel 脚本和 P1～P4 runner；它们尚未组成 P5 的 TripCheck 360 A/B/C Gate；
- 已记录但本轮未重跑：P4 completion record 中 backend `1313 passed, 28 skipped`、Ruff、frontend build、PostgreSQL、浏览器、18 pilot、P2/P3 regression 和 P4 manifest 均 PASS；
- 当前证据等级：controlled fixture、PostgreSQL integration、controlled browser fixture 为 PASS；frozen blind、P5 Evaluation Gate、G4 live Provider、public E2E、human evidence 为 `NOT_RUN`；candidate readiness 为 `REJECT`。

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
- pilot/dev/regression 沿用 P4 冻结 hash；P5 不通过复制或改写旧案例制造 360；
- frozen blind 每城 30，覆盖文本/截图、clean/medium/hard、地点歧义、路线、酒店、天气/风险、偏好/强度、Advice/Repair 和固定 fault profile；
- 同源/变异案例不跨 split；重复、分布、oracle 完整性、隐私和 secret scan 全部 PASS；
- 首次候选运行前冻结 blind inputs、case ID commitment、label commitment、RunSpec 和 seed；之后修改任一项必须使 Gate `REJECT`；
- 仓库内不存在 blind label payload、答案路径或可从 seal 逆推出标签的明细；isolated scorer 只返回聚合结果和 Gate 状态。

### B. A/B/C 公平执行

- 三个 variant 对 360 个 case 均产生一条终态输出，共 1080 条；异常、超时和不支持能力也必须落机器可读终态，禁止缺行；
- 三个 variant 的 RunSpec 除 `variant_id / adapter_version / repair_strategy` 外完全一致；
- 每个运行都绑定 commit、dirty tree、dataset、case IDs、config、model/prompt、rule、provider snapshot、fault、budget、seed 和 output hash；
- 相同 snapshot 重放结果 hash 100% 一致；重复运行不会复用旧输出拼接新报告；
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
- 生成 `backend/evidence/trip_check_v1/p5/p5_gate_manifest.json` 并执行 artifact/hash/readback/secret scan；
- Gate PASS、clean tree、远端 checkpoint 和 evidence readback 全部成立后，才可归档 P5 并生成 P6 draft；不得自动进入公网或候选发布。

计划窗口为 4 周或 20 个专注开发日，以 Gate 结果而不是日历日期判定完成。每个切片仍不得超过 60 分钟没有可恢复的本地与远端 checkpoint。

## Verification

计划新增并实际执行以下层级；命令在实现后以脚本 `--help` 和 Gate manifest 中记录的最终版本为准：

```powershell
cd backend
python -m pytest tests/test_trip_check_p5_dataset_contract.py tests/test_trip_check_p5_variant_adapters.py tests/test_trip_check_p5_scorer.py tests/test_trip_check_p5_blind_isolation.py -q
python scripts/validate_trip_check_p5_dataset.py
python scripts/run_trip_check_p5_eval.py --lane nonblind --variants legacy,core,solver
python scripts/run_trip_check_p5_eval.py --lane frozen-blind --variants legacy,core,solver
python scripts/run_trip_check_p5_gate.py

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
- blind label 泄漏、commitment 不一致、同源跨 split、需要修改 blind/oracle 才能追绿；
- A/B/C 无法在同一输入/RunSpec 下公平比较，或只能通过把 Core 结果喂给 Legacy 才能完成适配；
- Judge 被要求裁决确定性事实、运行时模型自评，或一个模型成为唯一 Judge；
- Solver 出现新增 BLOCKER/HIGH/UNKNOWN，或需要绕过 P4 admission 才能晋级；
- 连续两个切片不能改善同一门禁，独立故障诊断后仍需扩大范围或降低 Gate；
- evidence 绑定矛盾、成本超限、隐私事故或 secret 泄漏；
- 请求进入 P6 公网候选、H1、合并 `main`、release 或 deploy。

## Auto-advance

- Required gate：`Evaluation Gate`；
- Next Goal template：`TC-P6-G01-candidate-evidence`；
- 自动生成 P6 draft 必须同时满足：本 Goal 获批、全部 acceptance PASS、clean tree、P5 commit 已推送且 upstream 可确认、evidence 可回读、无 Stop condition；
- 生成 P6 draft 不等于获批公网、live Provider、release、H1 或合并 `main`。

## Completion record

- Commits：`NOT_STARTED`；
- Remote branch / upstream：`NOT_STARTED`；
- Verification results：`NOT_RUN`；
- Evidence paths：`NOT_GENERATED`；
- Gate result：`NOT_RUN`；
- Next Goal generated：`NO`；
- Remaining red lights：frozen blind、P5 Evaluation Gate、G4 live Provider、P6 Candidate Gate、public E2E、human evidence；
- Promotion decision：`NOT_REQUESTED`。
