# G2 三城连续四站真实快照与公开 HTTP 回放证据（2026-08-21）

## 结论

北京、上海、杭州各完成一条固定真实 Anchor 出发的连续 3 次接受链，形成 4 站；修复后的公开 HTTP 回放为 `PASS/PROMOTE`。该结论只覆盖本次执行的 3 个三城 session，不替代 G2 发布门禁要求的 9 个 session，也不代表 frozen blind、weekly live 或人工校准已经完成。

最终 run：

`backend/evidence/runs/20260821T074254336723Z-nightly_snapshot-24edafa9`

- gate：`PASS`
- decision：`PROMOTE`
- reason：`BUILDER_HTTP_SLICE_COMPLETE`
- 公开产品 HTTP：74 次，`200 × 62`、`201 × 12`，无 4xx/5xx
- 直接领域调用：0
- SQL seed：0
- accept 携带客户端 Place body：0
- G5 重启读回：`PASS / RESTART_EVIDENCE_VALID`
- `builder_ndcg_at_5 = 0.9834635871994558`
- `builder_recall_at_5 = 1.0`

## 一次性真实 Provider 捕获

仅执行一次新的 live capture，没有重试或第二轮补抓：

- profile：`local_real`
- `AMAP_MOCK=false`
- retry：0
- 请求预算：90
- 实际请求：89（候选查询 36、步行路线 53）
- 城市/轮次：北京 3/3、上海 3/3、杭州 3/3
- 每轮可见候选：北京 6/6/6、上海 6/6/6、杭州 6/6/5
- 原始 Provider payload 未写入证据；只保留脱敏后的 canonical entity、current fact、route receipts 与 request/response hash

快照：

`backend/evidence/real_provider_local_authorized/suggestion_chain_snapshot_2026-08-21-v2.json`

- file SHA256：`7a5f24a7cb764d1fc603329bb7db59de497802bac4bdf48f234f54d21697bdfa`
- payload SHA256 / snapshot ID：`8ba312f87ec21454d0fc84521cd2fbba916a8f2e9b6ec46c9eabd935e7bed9b5`
- Provider capture status：`PASSED`
- 快照自身的 public-ASGI replay 声明保持 `NOT_RUN`；G2 产品回放由独立 RunSpec 产物证明，快照不能自证。

## 三城精确接受链

| 城市 | 固定 Anchor → 接受 1 → 接受 2 → 接受 3 |
|---|---|
| 北京 | `B000A7BD6T → B000A7J7WD → B000A84ZLR → B000A6D5C3` |
| 上海 | `B00155H52F → B00156R4O6 → B0FFF00NA3 → B00157HXJU` |
| 杭州 | `B0FFHZ0001 → B0FFKETFI9 → B0FFHY5A9S → B0FFFDG53P` |

最终回放的 9 个 round 均满足：

- runner 从 schema 1.1 的 `selected_chain_place_ids` 读取目标；
- dismiss 不得消费本轮 chain accept 目标；
- accept 必须精确匹配 canonical Place ID，缺失时 fail closed；
- 接受后的新 stop 成为下一轮 Anchor；
- `accepted_canonical_place_id == expected_captured_chain_place_id`；
- 每轮 4–6 个真实可见候选；
- Top3 无 wrong-city、HARD 或 UNKNOWN；
- frozen create/readback hash、Anchor context 与 revision 原子递增检查通过。

## Receipt 完整性与 fail-closed

最终产品输出共 53 个可见候选：

- 53/53 有真实捕获并冻结回放的 entity receipt；
- 53/53 有真实步行路线 receipt；
- 51 个存在 current fact 的候选全部有 fact request/response hash；
- 2 个缺 current fact 的北京候选没有被补造事实，均落为 `UNKNOWN + hard_gate=false + INFEASIBLE`，未进入 Top3 或被接受。

杭州第 3 轮仍保留真实审计语义：

- `ADJACENT_CATEGORY_REPEATED` 仍是 `VIOLATED/MEDIUM` finding，并显示 `AUDIT_GATE_NONBLOCKING_WARNING`；它不再被错误提升成 HARD blocker；
- `OUTSIDE_OPENING_HOURS` 仍是 `VIOLATED/HIGH`，保持 `hard_gate=false + INFEASIBLE` 并排在 blocked 分段；
- UNKNOWN、BLOCKER、HIGH 仍 fail closed。

## 迭代证据（不得改写历史结果）

1. `20260821T072713009719Z-nightly_snapshot-f314e64f`：`REJECT`。旧 runner 的 dismiss fallback 误消费北京下一轮 chain target，导致链偏移。
2. `20260821T073459496365Z-nightly_snapshot-28d1c360`：`REJECT`。exact-chain runner 修复后，北京、上海通过；杭州第 3 轮的 MEDIUM 节奏 finding 被旧 gate 错误硬阻断。
3. `20260821T074254336723Z-nightly_snapshot-24edafa9`：`PASS/PROMOTE`。最小语义修复后，三城 9/9 round 与 G5 重启门禁均通过。

前两次 REJECT 证据保留，不覆盖、不重写为 PASS。

## 最终证据哈希

| 文件 | SHA256 |
|---|---|
| `run_spec.json` | `14612cfc9719f5875e2b84a6dbb573fd5df1d77199341d073f1e24c4d8535288` |
| `gate.json` | `3e7c5c29528bc53b018fdfe5fa7d55a516d7ee6e4193ddf73535bebe65816057` |
| `deterministic_scores.json` | `91da1e21f8628e964eb36d4433ab9b356d6ee21ae798699e21c95a7b9e6f625f` |
| `product_outputs.jsonl` | `8d1f86331cba32b1e9c9639e7f421e7eb3bb5310ec569d783afd8b6838c1530d` |
| `provider_receipts.jsonl` | `05063b5dddd7d4f5aa63527c150880fad2f677901db407c975fe97884d947a1c` |
| `http_transactions.jsonl` | `253d766c15ca8ca524a47c6d24003e91c441c1019e14c533c70138c9a35d8463` |
| `recommendation_events.jsonl` | `e7faea289a0e2ed16ef537b681677d828166eb63d1570bf66410f7715d8aa175` |
| `restart_gate.json` | `3dd582f9d2fcb7dcac10f68b57feccba5290fbf08b91bbce06496d47c68b5d9b` |

冻结 ranking oracle：

- 文件：`backend/eval_data/dual_entry_v1/builder_oracles/three_city_chained_suggestion_ranking_v1.json`
- file SHA256：`78a5d9dc611a31721a10049fbe242c9952b9000f706d86d1f406b6a35521f156`
- content SHA256：`f5e407db01214d2c9514a10069b68fa491ac3459b8292873f7e23677261fc08b`

## 验证结果与剩余边界

- 合并定向测试：`89 passed`
- 真实 ASGI recovery：`1 passed`
- Ruff：`All checks passed!`
- 专用 RunSpec preflight：10/10 checks PASS，`ACCEPT_PREFLIGHT`
- 全测试集结构：96 case，结构错误 0；`release_ready=false`
- G2 session 覆盖：3/9，仍缺 6 个独立 session，不能将本次三城单链切片写成完整 G2 发布门禁已通过。
- frozen blind、weekly live、30 条人工校准继续保持独立门禁状态。
