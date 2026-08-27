# REJECTED GOAL：O5/O6 DeepSeek 修复晋级与本地端到端稳定性

## Metadata

- Goal ID：`TC-NLU-O5-O6-DEEPSEEK-STABILITY`
- Program ID：`TC-INTAKE-V2-2026`
- Status：`REJECTED`
- Branch：`codex/trip-intake-deepseek-stability`
- Baseline：`39bde2d1d62c622a1f27bd826f9470bf0fdc4395`
- Approved by / at：User / 2026-08-27

## Outcome

继续使用 `deepseek-v4-flash`，把已暴露的 Validation 失败转成非盲回归，修复地点角色、完整证据和安全别名边界；在新的隔离 Validation 通过后，对原 24 条 frozen blind 执行一次正式评测。随后以真实 DeepSeek、PostgreSQL 和冻结 Provider 快照完成北京、上海、杭州三条本地主链，形成可回读的稳定性证据。

## Scope

- Trip Intake 确定性护栏、hybrid merge、DeepSeek 运行回执与失败语义；
- `OBSERVE_ONLY` 调用 ledger：无调用数/费用硬上限，但持续记录调用、Token、时延、失败分类和估算费用；
- 3 条已暴露失败 regression、24 条新 family-isolated Validation 及其生成/校验 receipt；
- Dev、Validation、一次性 frozen blind 晋级链；
- 真实 DeepSeek + 冻结地图/天气 Provider 快照的本地 API/浏览器 E2E；
- 完整 backend、Ruff、frontend build、dual-entry 与 Trip NLU validator 回归。

## Non-goals

- 不切换 Qwen，不改 Router、RAG、Planner、Synthesizer、Editor 或旧 Agent；
- 不修改原 120 条输入、frozen blind、blind oracle、评分门槛或公共 API/schema；
- 不新增 migration、生产依赖、Provider、基础设施或实时 Provider 调用范围；
- 不部署公网、不合并 `main`，不进入 H1、真人、production 或 release；
- 不把 fixture Provider、自动评测或本地 E2E 称为真人/生产证据。

## Authority and baseline

权威顺序沿用根 `AGENTS.md`。本 Goal 是用户对 `PROGRAM.md` 的显式 O5/O6 扩展授权，并覆盖旧 O1～O4 的 300 次/30 元上限，但只覆盖 Trip Intake 开发评测与本地 E2E。

历史 baseline：72 条 Dev Gate=`PASS`；旧 Validation 两次 `REJECT`（contract controls `0.9952`、`0.9904`，第二次含 1 个 hallucination）；frozen blind=`NOT_RUN`；工程回归 `1988 passed / 32 skipped`。

## Invariants

- 默认运行仍为 deterministic；hybrid 只由评测和本地 E2E 显式启用；
- DeepSeek 单请求非思考 JSON、temperature=0、零重试；模型不调用工具、不验证地点事实；
- 语义草稿必须通过 Unicode evidence compiler；冲突降级为待确认，失败 fallback 不注入默认事实；
- `OBSERVE_ONLY` 只取消预算阻断，不取消调用审计、成本估算、时延和错误回执；
- 已暴露 Validation 只能进入 regression；新 Validation 每个候选只正式评分一次；
- frozen blind 输入与外部标签不修改、不提前读取；Validation PASS 前禁止 blind；
- 正常真实调用 E2E 必须 `fallback=0` 并回读实际模型标识；fallback 只算降级验证；
- Provider snapshot、真实 DeepSeek、本地 E2E、live Provider、human 和 public evidence 分级披露。

## Verification

- regression、Dev、Validation、blind：schema/evidence/coverage 100%，六类关键错误为 0；
- locations、party size、duration micro-F1 ≥0.95，preferences/requirements ≥0.90，contract controls=1.0，hard key fields ≥0.90；
- 单并发 DeepSeek 抽取 P95 ≤5 秒；
- 新 Validation：24 条、8 个三例 family，与原 120 条 near-duplicate ratio <0.90；
- 三城 E2E：文本 Intake → 确认 → materialize → 地点消歧 → Audit → Advice → 新 revision → postcheck，3/3 PASS；
- E2E 同时覆盖刷新恢复、幂等 replay、SSE 重连、DeepSeek timeout/schema invalid fallback、Provider 局部失败与 UNKNOWN 保留；
- backend pytest、Ruff、frontend build、dual-entry validator、原 120 条 validator 全部重跑。

## Budget

- 模型固定 `deepseek-v4-flash`；用户授权调用次数与费用无硬上限；
- ledger 固定 `budget_enforcement=OBSERVE_ONLY`，`max_calls=null`、`max_cost_cny=null`；
- 每次请求仍先持久化 reservation，完成后记录 Token、估算费用、时延、实际模型与错误分类；
- 禁止隐藏 retry、并发扩张或把失败调用从 ledger 删除；
- 连续两个候选无改善时停止常规迭代，执行独立故障诊断。

## Execution plan

1. 冻结 Goal、remediation 数据合同与 OBSERVE_ONLY receipt；
2. 修复确定性角色、完整证据、错别字别名和 commitment merge；
3. 定向 regression 与完整 Dev，通过后执行新 Validation；
4. Validation PASS 后在外部 one-shot ledger 上运行原 frozen blind；
5. 完整工程回归；
6. 启动 fixture-safe 本地栈，以真实 DeepSeek 完成三城 API/浏览器 E2E；
7. 归档同 commit/config/data/model/receipt 证据并更新 Goal 状态。

## HITL and stop conditions

- blind/oracle hash 漂移、标签泄漏、secret/隐私泄漏或 receipt 绑定矛盾；
- 需要新 Provider、migration、生产依赖、公共 API 变更、实时外部数据扩张或公网部署；
- 新 Validation 或 blind 失败不得降低 Gate；blind 失败后不得按逐例结果优化；
- 连续两个候选无改善且独立诊断仍要求扩大范围；
- 请求合并 `main`、H1、真人、production 或 release。

## Completion record

- Implementation：`COMPLETED`；overall Gate：`REJECTED`；
- Goal contract：`structurally_valid=true`；
- remediation regression：7/7，Gate=`PASS`，全部质量指标 1.0，关键错误 0，P95=4.210 秒；
- Dev：72/72，Gate=`PASS`，全部质量指标 1.0，关键错误 0，P95=4.109 秒；
- independent Validation v2：24/24，Gate=`PASS`，全部质量指标 1.0，关键错误 0，P95=4.513 秒；
- 调用审计：Validation 结束时累计 202 次、324333 input tokens、112720 output tokens、估算 1.87374432 CNY；调用层失败和 fallback 均保留在 ledger；
- 工程回归：backend `2015 passed / 32 skipped`；PostgreSQL fresh/existing migration `2 passed`；Ruff、frontend build、dual-entry validator、原 120 条 validator、remediation validator 均通过；
- frozen blind：唯一一次产品预测已在 subject `d4fd9aafcb9dc12156e2ba4f0199c822f15f7c41` 完成，24/24、P95=3.691 秒、actual model 24/24、fallback=1、prediction SHA-256=`52294de76511ec144caf94b22e2325388e942518c567256a3f2b3559c64b9d11`；仓库外标签 SHA-256 与 seal 完全匹配；隔离评分 Gate=`REJECT`，hallucination=19、negation reversal=4、locations=0.4667、party=0.8583、duration=0.8711、preferences=0.5808、contract controls=0.7583；未输出逐例 truth，不得再次运行产品预测或基于 blind 调参；
- migration 027：经用户明确批准，删除 `trip_intake_revisions_room_id_intake_id_key`；全新 PostgreSQL volume 从 001 迁移到 027，027 已登记且冲突约束计数为 0；
- local real-DeepSeek E2E：subject `ee686a517e37019c06a3fa4c9ddb87b2355567ea`，北京/上海/杭州 3/3=`PASS`，真实模型 readback、正常链 fallback=0、revision 1→2、完整 postcheck、幂等/SSE/fault/UNKNOWN、unexpected 5xx=0 均通过；
- `INTAKE_V2_DEVELOPMENT_READY=false`：工程与本地 E2E 已通过，但 frozen blind Gate=`REJECT`，不得晋级；
- 后续若继续优化，必须创建新 Goal 和新治理 blind 版本，不得修改本次 oracle、降低门槛、读取逐例 truth 或重试本次 blind。
- 本 Goal 的开发证据不得改写或替代发布门禁，不得因此宣称 `V1_CANDIDATE_READY`。
