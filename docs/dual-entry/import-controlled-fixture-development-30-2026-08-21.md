# Import controlled fixture development 30（2026-08-21）

## 结论

本轮建立并真实执行了 30 条 Import 公开 HTTP development gate。唯一允许的声明是
`controlled_fixture_development_30`；它不是 G2 nightly frozen、不是 live Amap，也不解除
最终发布门禁。

最终受控 HTTP artifact：

`backend/evidence/runs/20260821T080522826426Z-pr_offline-e0e2b464`

门禁结论保持 `INVALID / REJECT`。拒绝不是 runner 异常或少跑 case：30/30 均完成，
Parser、Entity 和所有可执行 Finding 真值已准确通过；红灯来自 6 个旧 case 缺受控 POI
覆盖而无法形成 applied revision，以及 6 个 Repair case 都没有可评分的 postcheck。
缺证据时没有把 UNKNOWN、未执行 Audit 或无可行 Repair 冒充成通过。

## 30 条选择与覆盖

RunSpec：`backend/evals/run_specs/dual-entry-import-controlled-30.json`

| 维度 | 覆盖 |
|---|---:|
| 北京 / 上海 / 杭州 | 11 / 9 / 10 |
| 2 / 3 / 4 / 5 天 | 15 / 10 / 2 / 3 |
| pilot / dev / regression | 3 / 24 / 3 |
| Parser oracle | 20 case |
| Entity oracle | 19 case |
| Blocker/HIGH Finding oracle | 21 case，其中 15 case 本轮可执行 |
| Repair postcheck oracle | 6 case，当前 0 case 可评分 |

新增的 18 条按每城 6 条组成同构矩阵：

| 场景 | 每城 case | 核心断言 |
|---|---:|---|
| 正常餐饮与酒店 2 日 | 1 | 精确实体、餐饮/酒店身份不串类、缺 route fact 保持 HIGH/UNKNOWN |
| 时间重叠与路线间隔 2 日 | 1 | `TIME_CHAIN_BROKEN` + 相邻路线 UNKNOWN，不把缺路线证据当可达 |
| 闭馆冲突 2 日 | 1 | `OUTSIDE_OPENING_HOURS` 命中正确 canonical POI |
| 固定预约 3 日 | 1 | 已预约地点保持 fixed/locked，审计不移动承诺 |
| 重复地点 3 日 | 1 | 同 canonical ID 重复被发现，不由实体错配制造假 duplicate |
| 占位符 / 不存在 5 日 | 1 | `待定/待确认/未知地点` 为 NOT_FOUND，不调用 Provider 补造候选 |

原有 12 条继续覆盖 Day header、TSV/pipe table、简称歧义、wrong-city、西湖别名、
固定返程和 fixture provenance。杭州高铁返程 case 的 Parser 真值已全中，但当前 fixture
没有杭州东站等 6 个 POI receipt，因此只证明解析，不证明 Audit/Repair。

## 本轮修复及其反例

### Parser

旧反例 `dev.sh.import.table-span` 把 `天数\t时间\t地点\t备注` 当 POI，并把
`上海博物馆\t已预约`、`上海虹桥站\t高铁返程不可改` 连成地点名。现在 TSV/pipe 表头被
跳过，备注只形成 fixed/return 语义，source span 仍逐字回读；`杭州4日游，5位朋友`
不会被数字误判为时间。

### Entity 与 receipt

旧实现把实体解析复用给推荐排序，实际产生过 `圆明园→颐和园`、
`北京南站→北京瑰丽酒店`、`北京饭店→餐厅`。现在 local fixture entity adapter 只按
公开请求的 `target city + raw query` 从受控 fixture 选择身份兼容行，再由统一置信度排序；
不读取 label 或 expected ID。

wrong-city 反例“上海行程导入西湖”现在会返回实际 fixture 中的杭州强名称命中，resolver
将其从可确认候选中移除并保存完整 `WRONG_CITY` rejected receipt。若 fixture 没有名称
命中，结果仍为空，不补造 rejected candidate。

Runner 只有当 case 同时声明 `confirm` step、显式 `confirmation_instructions`，且指定
canonical ID 在候选中唯一出现时才发确认 PATCH。普通 AMBIGUOUS 永不默认选择第一项。

### Audit Finding

Finding metric 固定为 `exact-set-blocker-high-v1`，显式范围为 `BLOCKER,HIGH`。MEDIUM
不进入该版本分母，HIGH/UNKNOWN 仍正常计 precision/recall，UNKNOWN 从未视为 pass。

本轮新增 15 个可应用行程产生 48 个稳定 finding signature，48/48 精确命中：

- 缺 route receipt：`ROUTE_GAP_EVIDENCE_UNKNOWN / UNKNOWN / HIGH`，subject 为相邻地点对；
- 输入重叠：`TIME_CHAIN_BROKEN / VIOLATED / HIGH`；
- 晚于营业时间：`OUTSIDE_OPENING_HOURS / VIOLATED / HIGH`，subject 为 canonical POI；
- 重复实体：`DUPLICATE_PLACE / VIOLATED / HIGH`，subject 为 canonical POI。

同时修复 Provider 常见值 `全天` 被误报为 `OPENING_HOURS_UNPARSEABLE` 的问题。

### Repair 与未执行阶段

Audit 依赖 applied revision。导入仍有 AMBIGUOUS/NOT_FOUND 时，runner 现在输出：

```json
{"status":"NOT_EXECUTED_NO_APPLIED_REVISION","reason_code":"AUDIT_REQUIRES_APPLIED_REVISION"}
```

Repair 随后输出 `NOT_EXECUTED_NO_AUDIT`，不再把产品 404 记成 HTTP workflow crash。
产品返回 `422 REPAIR_NO_FEASIBLE_OPTION` 时同样作为结构化产品结果保存。

三个新增重叠 case 都准确发现时间冲突，但由于缺 route fact 的 HIGH/UNKNOWN 尚未解除，
Repair 引擎拒绝生成会绕过高风险 postcheck 的方案。这是正确 fail-closed 行为；只有接入
entity/route/weather frozen snapshot 后才能形成真正 Repair postcheck 证据，不能靠放宽规则过门禁。

## 最终逐指标结果

| 指标 | 分数 | 覆盖 | 结论 |
|---|---:|---:|---|
| Parse F1 | 1.0000（96 TP / 0 FP / 0 FN） | 20/20 | PASS |
| Entity P/R/F1 | 1.0000（82 TP / 0 FP / 0 FN） | 19/19 | PASS |
| Blocker/HIGH Finding P/R/F1 | 1.0000（48 TP / 0 FP / 0 FN） | 15/21 | INVALID：6 个旧 case 无 applied revision |
| Repair postcheck | N/A | 0/6 | INVALID：3 个无 Audit，3 个无可行 Repair |
| Provider receipt contract | 30/30 | 100% | PASS |
| Offered receipt | 29/29 | 100% | PASS |
| Materialized receipt | 16/16 | 100% | PASS |
| Wrong-city rejected receipt | 1/1 | 100% | PASS |
| HTTP case count | 30/30 | 100% | PASS |

最终 9 个 bad case 分两类：

- 6 个旧 case：`pilot.bj/sh/hz`、`dev.hz natural/duplicate-alias`、
  `reg.hz canonical-duplicate`，当前 fixture 缺实体或自然段无法安全唯一解析，未应用 revision；
- 3 个新增 overlap case：Finding 3/3 全准，但 Repair 因 route UNKNOWN 无可行方案。

相较首轮 artifact `20260821T071710745586Z-pr_offline-0f02e8e1`，表格 Parse F1 从
0.444 修到最终整体 1.0；实体由推荐错配修到 1.0；wrong-city rejected receipt 由 0/1
修到 1/1；Finding 在可执行 case 上由大量实体污染造成的假阳性，收敛为 48/48。

## 冻结 nightly / 最终门禁边界

`backend/evals/run_specs/dual-entry-import-nightly-frozen-blocked.json` 只提供最小可执行 seam，
要求 `import-frozen-entity-route-weather-v1` 同时提供 ENTITY_RESOLUTION、ROUTE_TIME、WEATHER。
当前只有 13 个选择、没有 snapshot artifact/hash/baseline，preflight 必须
`BLOCKED_MISSING_ARTIFACT`。本轮不得写成 G2 Import frozen PASS。

最终发布仍需：至少 30 条真实 frozen Import、同一 artifact 的完整回放、可评分 Repair
postcheck、外部 blind bundle、weekly live 与真人校准。受控 fixture 的 30 条结果不能替代这些证据。

## 复现命令

```powershell
$env:PYTHONPATH="backend"
python backend/scripts/expand_import_controlled_30.py
python backend/scripts/validate_dual_entry_testset.py
python -m pytest backend/tests/test_itinerary_imports.py backend/tests/test_opening_hours_boundaries.py backend/tests/test_continuous_http_import_runner.py backend/tests/test_dual_entry_metric_scorer.py backend/tests/test_dual_entry_testset_contract.py backend/tests/test_frozen_blind_scorer_isolation.py -q
python -c "from pathlib import Path; from evals.continuous import run_import_http; r=run_import_http(Path('backend/evals/run_specs/dual-entry-import-controlled-30.json')); print(r.run_dir); print(r.gate)"
```

执行真实 HTTP 前必须先确认 `/health` 为 `runtime_profile=local_fixture / amap_mock=true /
demo_mode=false`，且不存在 frozen snapshot adapter 环境变量。执行结束后停止本轮启动的
Docker 服务。
