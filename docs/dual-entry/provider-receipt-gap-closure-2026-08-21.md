# Dual Entry Provider receipt 缺口真实性收口（2026-08-21）

## 结论

旧 validator 将 Builder input 中没有 `provider_receipt_id/receipt_id` 的路径统称为“Provider-derived subject 缺 receipt”，得到 25 case /111 subject。这个口径把合成测试输入、真实 Provider 调用和无证据状态混在了一起。

本轮只接受以下三类结论：

| 分类 | 旧 111 subject | 可声明范围 |
|---|---:|---|
| 现有 Amap snapshot 精确 receipt 绑定 | 0 | 无；没有任何旧缺口同时具备 canonical identity、不可变 source artifact 和原 receipt bytes |
| `CONTROLLED_FIXTURE_EXECUTION` | 90 | 只证明 development case 的 input subtree bytes；没有 Provider 调用 |
| `UNAVAILABLE` | 21 | frozen-blind repository input 没有历史调用 artifact，必须由外部 bundle/run 补齐 |

禁止的“收口”方式包括：按地点名称关联 Amap、按坐标近似匹配、用当前时间补 `observed_at`、把 `snap-*`/`rcpt-*` 字符串当证据、把官方路线或 Wikivoyage 结构先验当 POI/current fact/route receipt。仓库中的真实 Amap suggestion snapshot 仅作为 canonical identity 对照；validator 对旧 111 条算出的 exact identity overlap 和 exact receipt binding 都是 0。

## 25 case 逐项分类

| Case | 城市 | 旧缺口 | Amap 精确绑定 | Fixture | Unavailable |
|---|---|---:|---:|---:|---:|
| `pilot.bj.builder.forbidden-city-line` | 北京 | 5 | 0 | 5 | 0 |
| `pilot.sh.builder.bund-line` | 上海 | 5 | 0 | 5 | 0 |
| `pilot.hz.builder.west-lake-line` | 杭州 | 5 | 0 | 5 | 0 |
| `dev.bj.builder.insertion-thresholds` | 北京 | 4 | 0 | 4 | 0 |
| `dev.bj.builder.hard-gate-diversity` | 北京 | 5 | 0 | 5 | 0 |
| `dev.bj.builder.atomic-accept-anchor` | 北京 | 4 | 0 | 4 | 0 |
| `dev.bj.builder.drag-concurrency` | 北京 | 4 | 0 | 4 | 0 |
| `dev.sh.builder.route-intent-ranking` | 上海 | 5 | 0 | 5 | 0 |
| `dev.sh.builder.stale-suggestion-set` | 上海 | 4 | 0 | 4 | 0 |
| `dev.sh.builder.restart-events` | 上海 | 4 | 0 | 4 | 0 |
| `dev.hz.builder.westlake-ranking` | 杭州 | 5 | 0 | 5 | 0 |
| `dev.hz.builder.member-hard-gate` | 杭州 | 4 | 0 | 4 | 0 |
| `dev.hz.builder.insert-edge` | 杭州 | 4 | 0 | 4 | 0 |
| `dev.hz.builder.mobile-undo` | 杭州 | 4 | 0 | 4 | 0 |
| `dev.sh.builder.insert-edge-four-tiers` | 上海 | 2 | 0 | 2 | 0 |
| `dev.hz.builder.insert-edge-context` | 杭州 | 2 | 0 | 2 | 0 |
| `reg.sh.builder.missing-edge-unknown` | 上海 | 4 | 0 | 4 | 0 |
| `reg.hz.builder.drag-no-llm` | 杭州 | 4 | 0 | 4 | 0 |
| `reg.bj.builder.drag-button-equivalence-recovery` | 北京 | 8 | 0 | 8 | 0 |
| `reg.hz.builder.drag-button-restart-undo` | 杭州 | 8 | 0 | 8 | 0 |
| `blind.bj.builder.01` | 北京 | 5 | 0 | 0 | 5 |
| `blind.bj.builder.02` | 北京 | 4 | 0 | 0 | 4 |
| `blind.sh.builder.01` | 上海 | 4 | 0 | 0 | 4 |
| `blind.hz.builder.01` | 杭州 | 4 | 0 | 0 | 4 |
| `blind.hz.builder.02` | 杭州 | 4 | 0 | 0 | 4 |
| **合计** | — | **111** | **0** | **90** | **21** |

逐路径记录位于 `backend/eval_data/dual_entry_v1/subject_receipt_registry.jsonl`，上表不是手工 gate 输入。

## 扩展检查发现的漏项

新的 registry 不再只枚举“没有 ID 的 Builder path”，而是覆盖所有可能被误认为 Provider 事实的静态 input subtree：

| 完整静态 subject | 数量 |
|---|---:|
| `CONTROLLED_FIXTURE_EXECUTION` | 242 |
| `UNAVAILABLE` | 35 |
| 真实 Provider | 0 |
| 合计 | 277 |

35 条 unavailable 中，21 条来自旧 Builder 缺口；另外 14 条是旧算法漏掉的 6 个 blind seed 和 8 个 blind Import controlled-fact subject。没有 controlled fact 的纯 Import raw text 不是 Provider subject，因此不会为了凑 receipt 而创建记录。

每条 registry record 都固定：case、split、entry、subject path/type、subject SHA-256、case normalized input SHA-256、声明中的旧 receipt ID（可空）和证据类别。每条 case ref 再固定 receipt ID 与 canonical record SHA-256，manifest 固定 registry 文件 SHA-256。相同 POI 在不同 stop/path 出现时 receipt ID 必须不同。

Fixture record 的固定边界是：

- `provider=controlled_fixture`；
- `provider_call_attempted=false`；
- `current_fact_authority=false`；
- `live_provider_evidence=false`；
- `observed_at=null`、`source_artifact=null`；
- `claim_scope=DATASET_INPUT_BYTES_ONLY`。

因此它不是伪装过的 Amap receipt，也不能解除 frozen/live RunSpec 的运行时 receipt 要求。

## Import 与 Builder 运行时要求

Manifest 将以下对象固定为运行期 `provider_receipts.jsonl` 必须覆盖的独立 subject：

- Import offered candidate；
- Import rejected candidate；
- Import materialized place；
- Builder candidate；
- Builder route leg；
- Builder current fact。

Development label 中当前有 1 个明确 `NOT_FOUND` case，需要 HTTP 输出保存 wrong-city rejected candidate 的完整 receipt。静态 controlled facts、source archive 或选中候选 receipt 均不能替代 rejected candidate receipt。Builder candidate 中的 route leg/current fact 同样必须在实际 frozen/live run 中重新收集；静态 fixture receipt 只证明测试输入被固定。

## Fail-closed 结果

当前 validator 结果：`structurally_valid=true`、`release_ready=false`、`errors=[]`。发布 blocker 使用 `FROZEN_BLIND_STATIC_SUBJECT_EVIDENCE_UNAVAILABLE`，并继续保留 Import/Release Provider snapshot、不足规模、外部 blind bundle、G2、weekly live 和 human calibration 红灯。

负向测试覆盖：registry 文件 bytes 篡改、攻击者同时重封 registry 文件 hash 后的 subject hash 篡改、跨两个 stop/path 复用 receipt、source/fixture 升格为 Provider current fact，以及 normalized input/跨 split 污染。当前定向结果为 `22 passed`，相关 runner/foundation 联合结果为 `74 passed`，Ruff 通过。
