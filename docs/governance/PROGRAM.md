# TC-INTAKE-V2-2026 Program

> 状态：`APPROVED`
>
> 授权日期：2026-08-26

## Outcome

交付 `文本/截图 → TripIntakeRevision v2 → 用户确认 → 幂等物化 → 既有 TripCheck` 的纵向闭环。解析覆盖任意国内地点、任意人数和天数表达；一个权威 workspace 仍为单城市，且只有精确城市、日期和正整数人数经确认后才能进入事实采集。

## 固定阶段

| 阶段 | 交付 | 退出条件 |
|---|---|---|
| I1 | 权威文档、v2 schema、迁移与不可变仓储 | 合同/迁移 Gate |
| I2 | LLM 结构化抽取、确定性 validator、revision/confirm/materialize API | 后端与 PostgreSQL Gate |
| I3 | 文本/截图 Intake UI、恢复与既有主链兼容 | 浏览器 Gate |
| I4 | 120 条 NLU 数据、scorer、sealed blind 与 NLU Gate | NLU Gate |
| O1 | DeepSeek V4 Flash 客户端、语义草稿、证据编译与混合抽取 | 抽取器合同与失败语义 Gate |
| O2 | 真实预测 runner、RunSpec、预算/时延回执与通用 scorer | 可复现 baseline Gate |
| O3 | 仅基于 dev/validation 的两轮优化和候选冻结 | Validation NLU/性能 Gate |
| O4 | 一次性 sealed blind 评分与候选证据汇总 | Frozen blind NLU Gate |

任何时刻只允许 `CURRENT_GOAL.md` 中一个 Goal 为 `APPROVED/IN_PROGRESS`。本 Program 不自动进入公网、真人、生产 release 或跨城。

## 预批准事项

- 新增 `025_trip_intake_v2.sql`、Intake schema/repository/API、字段证据与 materialization receipt；
- 放宽当前主链的城市白名单、2～5 人、2～5 天和 `day_index <= 4` 数据约束；
- 复用现有 OpenAI-compatible LLM 依赖做无工具 schema extraction，并提供无密钥/失败时的 fail-closed 结果；
- 生成 120 条 synthetic text NLU 数据，使用开发子代理独立复核并封存 blind；
- 在独立开发分支运行离线、PostgreSQL 和本地浏览器验证并 commit/push。
- 使用 `deepseek-v4-flash` 做最多 300 次、总估算费用不超过人民币 30 元的真实开发评测；模型请求使用非思考 JSON 模式且不调用工具；
- 在不改变公共 Intake v2 API/schema/migration 的前提下增加可配置 hybrid extractor、真实 prediction runner 和聚合 blind scorer；
- O1～O4 的阶段性能门槛为单并发端到端 P95 ≤5 秒；V1 Candidate Gate 原 P95 ≤3 秒保持不变。

## Non-goals 与停止条件

- 不支持一个 workspace 内跨城，不扩建 Builder/Planner/RAG/LoRA/Yjs，不新增消息队列或基础设施；
- 不自动扩大 live Provider 调用，不把三城历史 receipts 当作全国证据；
- 不开展公网、真人、生产部署、main 合并或 release；
- 需要新增付费 Provider、修改 frozen blind、降低 Gate、扩大外部数据或发生隐私/证据矛盾时停止并请求人工批准。
- 本次 DeepSeek 调用授权只覆盖上述 300 次/30 元开发预算，不自动授权 production 默认启用、扩大额度或其他模型；
- frozen blind 每个冻结候选只允许一次正式评分；失败后不得按 blind 结果调参，必须生成新版本 blind 后才能再次晋级。
