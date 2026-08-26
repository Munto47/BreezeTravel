# REJECTED GOAL：O1～O4 Trip NLU v2 真实抽取与优化

## Metadata

- Goal ID：`TC-NLU-O1-O4`
- Program ID：`TC-INTAKE-V2-2026`
- Status：`REJECTED`
- Branch：`codex/trip-nlu-v2-optimization`
- Baseline：`d967e774e8eab208234c2af9fb677a90877a1766`
- Approved by / at：User / 2026-08-26

## Outcome

让 `deepseek-v4-flash` 的真实 hybrid prediction 跑通固定 Trip NLU v2 数据，建立可复现 deterministic/hybrid baseline，在 blind 隔离下优化并交付 Validation 与 Frozen blind 的准确率、安全、时延、成本和绑定回执。

## Scope

- 内部 semantic draft、Unicode 证据编译、DeepSeek OpenAI-compatible JSON 客户端和 hybrid merge；
- 默认 deterministic、显式 hybrid 的应用服务注入与 fail-closed fallback；
- dev/validation 通用 scorer、真实 prediction runner、RunSpec、调用/成本/时延预算；
- 72 dev baseline、最多两轮各 60 条 dev 回放、最多两次 validation、一次 frozen blind；
- 单元、API/应用集成、全量回归和最终证据报告。

## Non-goals

- 修改 120 条数据、oracle、split、template/mutation family 或 blind labels；
- 新公共 API/schema、migration、生产默认启用 DeepSeek、其他模型 bake-off；
- real OCR、live Provider、全国覆盖、公网、H1、production、main merge 或 release；
- 用文本 NLU 结果证明行程合理性、Provider 事实或真人可用性。

## Invariants

- 模型不调用工具、不验证地点真假；未知、冲突和无效证据不得变成确定事实；
- 模型只提出语义草稿，服务端按原文 Unicode code point 编译并逐字验证 evidence span；
- hybrid 冲突降级为不确定并产生 issue，失败使用 deterministic fallback 且不注入默认值；
- 运行回执绑定 commit/dataset/model/prompt/schema/config/predictions，但不记录密钥；
- blind labels 只由隔离 scorer 读取，正式输出不含逐例 truth；每个候选只正式评分一次。

## Verification

- Validation 与 blind：schema/evidence/coverage 100%，六类关键错误为 0；
- locations、party size、duration micro-F1 ≥0.95，preferences/requirements ≥0.90，contract controls=1.0，hard key fields ≥0.90；
- 单并发端到端 P95 ≤5 秒；实际模型调用 ≤300，估算费用 ≤30 CNY；
- backend pytest、Ruff、frontend build、dual-entry validator、Trip NLU v2 validator 全部重跑。

## Budget / HITL / Stop

- 固定模型：`deepseek-v4-flash`；非思考 JSON 模式，temperature=0，max output=4096，单请求 deadline=4.5 秒；
- 调用预算：dev baseline 72、两轮 dev 回放 120、两次 validation 48、blind 24、预热 3、dev 故障余量 33，总上限 300；
- 成本按运行时冻结的官方单价和内部 `1 USD = 8 CNY` 计算，总上限 30 CNY；
- 任一预算到达、需要修改 blind、扩大付费范围、降低 Gate 或发生隐私/证据矛盾时立即停止；
- blind 失败不得用逐例结果优化；只能从 dev/validation 生成独立 regression，并在新 blind 版本获批后重新晋级。

## Completion record

- Implementation subject：`b051486dffd1f2ef81b7148b4b3672ab3a4f74d0`，已推送到 `origin/codex/trip-nlu-v2-optimization`；
- O1/O2：semantic draft、Unicode evidence compiler、hybrid/fallback、真实 prediction runner、RunSpec、预算与通用 scorer 已完成；
- O3：最终 72 条 Dev 全量 Gate=`PASS`，两次 Validation Gate 均为 `REJECT`；
- O4：因 Validation 前置 Gate 未通过，正式 frozen blind=`NOT_RUN`，blind labels 未被评分进程读取；
- 数据集 validator：`structurally_valid=true`、120/120、evidence span validity=1.0、`blind_labels_read=false`；
- `INTAKE_V2_DEVELOPMENT_READY=false`：首次完整 backend 回归为 1987 passed / 32 skipped / 1 failed，唯一失败是本 Goal 当时缺少上述 `structurally_valid=true` 状态声明；补写后必须重跑失败测试和全套回归，旧结果不得直接记为 PASS；
- `V1_CANDIDATE_READY=false`：本 Goal 不得改写或替代既有 Candidate Gate，不得因此宣称 `V1_CANDIDATE_READY`；
- Budget：297/300 次，估算 5.10609568 CNY / 30 CNY；
- Promotion decision：`REJECT`，不进入 frozen blind、G0～G6、H1、公网、release 或 main merge；
- Evidence report：`docs/testing/trip-nlu-v2-optimization-2026-08-27.md`。
