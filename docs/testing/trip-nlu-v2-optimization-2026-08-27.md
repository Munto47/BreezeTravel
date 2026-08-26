# Trip NLU v2 DeepSeek V4 Flash 评测与优化证据

> 日期：2026-08-27
>
> 分支：`codex/trip-nlu-v2-optimization`
>
> 实现候选：`b051486dffd1f2ef81b7148b4b3672ab3a4f74d0`
>
> 最终晋级结论：`REJECT`

## 交付边界

本阶段已经交付真实模型 prediction 闭环，而不是把 gold label 当 prediction：

- `TripIntakeSemanticDraft` 只承载语义值、quote 和重复索引；Unicode code point span 由服务端编译并逐字验证；
- `deepseek-v4-flash` 使用非思考 JSON Output、temperature=0、4096 token、4.5 秒 deadline、零重试；
- hybrid 合并合法模型字段和显式规则字段，冲突降级为不确定，非法字段丢弃，失败立即走 deterministic fallback；
- 默认运行模式仍为 deterministic，只有本轮 runner 显式使用 hybrid；
- RunSpec/receipt 绑定 commit、dataset、model、prompt、schema、config、prediction、token、时延、fallback 和成本，不保存密钥；
- 120 条数据、oracle、split、配额和 sealed blind 均未修改。

## 绑定

| 项目 | SHA-256 / 值 |
|---|---|
| dataset manifest | `cab1056d3a435f7a4c576a97f0d6d75ef17b8d4ed6833721ea038b64db52b0ab` |
| Dev inputs | `72f005360409bd82a9c1fbbd71f7aa2d330157dfd4fc9af80d19984babb120d5` |
| prompt | `fa0d63ab4ce625af2e52f52da4a2952d13de70867bf24f0d468db388f8de4a42` |
| semantic schema | `06444eb3bc0430b77a1609a96895e397f9a886e23fb39d4e2aed797af5300e1c` |
| public extraction schema | `fe5f80bb8d173079021751aaac78b54703b49ef435e2a4fffc8c29b9f64d3b4f` |
| config | `10a7a9d9c935057e4d3ded1e4d23a89c6e4ab6ba2b7b3b859d4c7e7b73a81158` |
| scorer | `88f263dd2b43839859d9cd10ba1da04d465075a4f427c6bd603d8604f0ccf8c4` |
| validator | `92ef656c69d153c2c6f56ee1ee488f9112c5420470c4d738a71a4ade6858c6e2` |

原始运行产物保存在仓库外忽略目录：

`D:/munto/code/claudeProject/agentTravel-trip-nlu-v2-optimization/.local-artifacts/trip-nlu-v2-optimization/`

## 真实评测结果

| Run | Gate | Locations | Party | Duration | Preferences / requirements | Contract | Hard key fields | 零容忍错误 |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| 初始 deterministic Dev，72 条 | REJECT | 0.5266 | 0.5371 | 0.7148 | 0.0428 | 0.2264 | 0.6313 | hallucination=1，old-plan reversal=10 |
| 最终 hybrid Dev，72 条 | PASS | 0.9912 | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 0.9931 | 0 |
| Validation attempt 1，24 条 | REJECT | 0.9692 | 0.9884 | 1.0000 | 1.0000 | 0.9952 | 1.0000 | 0 |
| Validation attempt 2，24 条 | REJECT | 0.9615 | 0.9884 | 0.9956 | 1.0000 | 0.9904 | 1.0000 | hallucination=1 |
| Frozen blind，24 条 | NOT_RUN | — | — | — | — | — | — | 未读取标签 |

第一次 Validation 的核心 F1 与安全项过线，但 contract controls 不是要求的 1.0。第二次复跑同一冻结候选仍失败，并出现 1 个零容忍 hallucination。因此不能选择较好的一次结果拼成 PASS，也不能进入 blind。

最终 Dev prediction SHA-256：`ea97e635d6ad89e35a464805202eba6bf690da57597de9fcfb745ec3780eeff5`。两次 Validation prediction SHA-256 分别为 `1e95689e8dbaceed5758494b8f0a59dac94fabcf81bcdd7e50f3330ac3cd843b` 和 `c5c41c4b08f3d8ff74fff70573757597137b34db34d279fc2859d0e98a2d0bb7`。

## 时延、fallback 与费用

| Run | P95 | Fallback | 失败分类 |
|---|---:|---:|---|
| 最终 hybrid Dev | 4515.241 ms | 9/72 | schema_invalid=5，timeout=4 |
| Validation attempt 1 | 4519.187 ms | 2/24 | timeout=2 |
| Validation attempt 2 | 4517.488 ms | 4/24 | schema_invalid=2，timeout=2 |

- 单并发端到端 P95 均小于 5 秒；
- 总调用 297/300，累计输入 429731 tokens、输出 146648 tokens；
- 按冻结单价与 1 USD=8 CNY 核算，估算 5.10609568 CNY / 30 CNY；
- 正式 blind 未调用，因此 one-shot blind ledger 未消费。

## 工程验证

- Trip Intake hybrid/runner 定向测试：39 passed；
- Ruff 定向检查：PASS；
- frontend production build：PASS；
- dual-entry validator：`structurally_valid=true`、`release_ready=false`，历史 release blockers 未被当前阶段覆盖；
- Trip NLU v2 validator：120/120、证据 100%、固定分布匹配、`blind_labels_read=false`；
- backend 首次全套：1987 passed / 32 skipped / 1 failed，唯一失败为 `CURRENT_GOAL.md` 缺少治理回归要求的 `structurally_valid=true` 声明；
- 修正文档并推送 `bd3702e` 后完整重跑：1988 passed / 32 skipped，Ruff PASS。工程回归通过，但不能覆盖 Validation NLU Gate 的 `REJECT`。

## 最终边界

- O1/O2 实现与 runner 闭环已完成；最终 Dev Gate 通过；
- O3 Validation Gate 未通过，O4 frozen blind 按合同保持 `NOT_RUN`；
- G0～G6、OCR 真人数据、live Provider、全国覆盖、公开 E2E、H1 和真人证据均为 `NOT_RUN`；
- 默认 deterministic 模式、公共 API、数据库 migration 和 120 条冻结数据均未改变；
- 下一轮若继续优化，只能从 Dev/Validation 形成独立 regression 并批准新的 Goal；不得修改当前 blind 或降低 Gate。
