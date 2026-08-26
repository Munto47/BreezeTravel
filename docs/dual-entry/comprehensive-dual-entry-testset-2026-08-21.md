# BreezeTravel 双入口全面测试集 v1

> 日期：2026-08-21  
> 对应基线：`BreezeTravel_双入口可验证行程产品与架构重构最终方案_2026-08-20.md` Final 2.0  
> 当前状态：测试数据与 G0 合同已结构验证；产品链、Provider、Judge、浏览器、真人与发布门禁尚未因此通过

## 1. 本轮交付

本轮先交付“开发优化测试集 + 最终门禁合同”，暂不把测试集存在误写成产品能力完成。

机器可读资产：

- `backend/eval_data/dual_entry_v1/manifest.json`：范围、数量、证据边界和 release blockers；
- `case.schema.json` / `label.schema.json`：输入与真值分离；
- `pilot/dev/regression/frozen_blind.inputs.jsonl`：产品输入；
- 对应 `*.labels.jsonl`：确定性真值；
- `source_registry.jsonl`：官方、开放数据和 Provider 文档的用途边界；
- `backend/evals/run_specs/dual-entry-*.json`：PR、快照、Live、Blind 四条 lane；
- `backend/scripts/validate_dual_entry_testset.py`：G0 结构与污染预检；
- `backend/tests/test_dual_entry_testset_contract.py`：可自动执行的测试集合同回归。

当前共 96 个 case，三城严格平衡：

| 维度 | 数量 |
|---|---:|
| 北京 | 32 |
| 上海 | 32 |
| 杭州 | 32 |
| 导入 / Audit / Repair | 55 |
| Builder / Suggestion / 拖拽 | 41 |
| pilot | 6 |
| dev | 60 |
| regression | 12 |
| frozen blind bootstrap | 18 |

这些 case 是第一批可执行核心，不是足以做统计发布结论的最终 blind 规模。manifest 会明确拒绝 release ready，直到至少补齐 90 个 blind import、45 个 blind builder、24 个 fault/recovery，以及 Import/Release Provider snapshot 覆盖、blind 隔离存储和真人门禁。Builder slice 已绑定 2026-08-21 三城真实候选与步行路线快照并通过 G0 hash preflight；它只证明 local-authorized entity/route 观察，不替代营业/预约/无障碍、public E2E、PostgreSQL 或真人证据。

## 2. 测试设计原则

### 2.1 输入、事实和体验评分分离

```text
inputs.jsonl
  只含用户输入、Provider 场景引用和执行步骤

labels.jsonl
  只供确定性 scorer 使用：解析、实体、Evidence、Audit、Repair、事务、候选和事件真值

GPT-5.6-sol Judge
  只评价节奏、最小修改、画像贴合、解释和实用性

human calibration
  只记录授权真人的误报、漏报、采纳、拒绝和体验
```

`UNKNOWN` 不得进入 `must_pass`；UGC、官方路线先验和模型常识不得证明营业、预约、票价、无障碍、过敏安全或当前路线。

### 2.2 同一测试同时约束“该报什么”和“不该报什么”

每个 label 至少包含：

- `must_pass`：事实与合同必须成立；
- `must_fail`：必须发现的确定性违规 reason code；
- `must_be_unknown`：Evidence 不足/过期/冲突时必须诚实保留的 UNKNOWN；
- `must_not_happen`：静默错配、把未知当通过、移动锁定项、客户端篡改、SQL seed、LLM/Planner 误调用等禁止行为；
- 需要时提供 `expected_parse`、`expected_resolutions`、`expected_findings`、`repair_oracle`、`suggestion_oracle`。

此外每份 label 都强制提供 `metric_oracles`，不再从 `must_pass` 文本猜测评分真值：

| 指标 | 结构化评分单元 | 当前 APPLICABLE / N/A |
|---|---|---:|
| parse F1 | NFC/空白归一后的 stop name 集合 | 20 / 58 |
| entity precision/recall | `raw_name + status + canonical_place_id` | 19 / 59 |
| finding precision/recall | `reason_code + status + subject + affected_member` | 31 / 47 |
| repair/postcheck | 最大方案数、允许操作和三个布尔谓词 | 10 / 68 |
| Builder nDCG@5 | candidate 的 0～5 级相关性 | 11 / 67 |
| Builder Recall@5 | relevant candidate ID 集合 | 13 / 65 |

不具备精确结构化真值的指标只有 `N_A + reason_code`，不得补造。78 条 development label 均为 synthetic/public-source-seeded 开发真值，授权真人真值仍为 0，因此这些数字表示“可计算覆盖”，不是质量分数，更不是 human calibration。

finding 不比较随机 UUID，而比较稳定签名：

```text
rule_id + rule_version + status + severity + reason_code
+ affected stop/member semantic selector
+ required Evidence fact type/freshness/provider
```

### 2.3 正例与受控变异成对

具备可追溯父 case 的 clean baseline 与 controlled mutation 必须在同一 split。除了变异直接影响的 finding 外，不允许新增其他 BLOCKER/HIGH/UNKNOWN。这能同时测 precision 和 recall，避免“永远报警”刷高召回。当前 corpus 没有可核验的父子生成记录，因此不把相似 case 冒充成这种配对；相关 lineage 先保持 N/A。

## 3. 三城主路线与主要地点

地点名称只用于覆盖和实体真值；实际路线分钟、营业、预约和天气由 case 绑定的 snapshot/live receipt 决定。

| 城市 | 主路线族 | 远距/边界 |
|---|---|---|
| 北京 | 故宫—景山—北海/什刹海；天坛—前门—大栅栏；颐和园—圆明园；国博—北京南站 | 慕田峪/八达岭换日、故宫固定预约、到达/返程、同名简称 |
| 上海 | 外滩—南京东路—人民广场；豫园—城隍庙；新天地—南昌路—思南路；武康路 CityWalk | 迪士尼远距、朱家角跨区、虹桥/浦东返程、分馆/同名 |
| 杭州 | 西湖—断桥—白堤—雷峰塔；灵隐—茶博；河坊街—大运河；西溪 | 良渚/宋城换日、杭州东站返程、西湖泛称、天气与户外 |

Builder 必须围绕当前 Anchor 或插入边查询，而不是继续使用“城市 + 类别”Provider 原始顺序。主推荐保持 4～6 个；不足时返回实际数量和 shortage/failure reason，不得补造。

## 4. 导入、Audit 与 Repair 用例

### 4.1 Parser 与输入安全

覆盖：自然段、列表、Tab/表格复制、中英文 Day 标题、缺时间、地点简称、同一行多个地点、全天、空文本、超长文本、50/51 stop、越界天数、Prompt Injection 和 API key 字样。

关键断言：

- 日标题、表头和分隔线不能变成 POI；
- `第6天` 在 5 天范围中不得静默落到 day 0；
- 解析失败返回可编辑 draft，不丢原文字段；
- structured fallback 每 case 最多一次；
- stop/date/time/fixed commitment F1 ≥0.95；
- source span 回读率 100%。

### 4.2 实体消歧与 apply 事实连续性

覆盖：简称、别名、同名 POI、分馆/分店、错城、交通节点、NOT_FOUND、Provider 空结果、Provider timeout、低置信度确认、客户端提交未提供候选。

关键断言：

- 高置信度自动匹配 precision ≥0.98；
- 静默错配为 0；
- 低置信度必须人工确认；
- wrong-city candidate 不能进入 revision；
- apply 同一事务写入 canonical POI、坐标、Provider receipt、source span、revision 和 map projection；
- 任一步失败全部回滚；
- 测试禁止手工 `INSERT room_places` 或其他 SQL seed。

### 4.3 Audit L0–L5

| 层 | 主要 case |
|---|---|
| L0 输入 | 日期/天数/时间/固定承诺缺失、未消歧地点、到达与返程边界 |
| L1 地点与事实 | 不存在、错城、重复、营业冲突、预约、Evidence FRESH/STALE/UNAVAILABLE/CONFLICTING |
| L2 时空 | 时间重叠、空档小于真实路线时间、跨区折返、固定预约、返程冲突、酒店返回 |
| L3 成员与节奏 | 老人步行、儿童午休、餐食、休息、最晚返回、过敏、轮椅事实不足 |
| L4 动态 | 雨/大风/高温与户外、预报窗口外 UNKNOWN、室内替代 |
| L5 协同 | 多成员 HARD 冲突、未确认成员、投票不能覆盖 HARD |

完整 Evidence 的 clean case 出现 BLOCKER/HIGH/UNKNOWN 计 false positive；UNAVAILABLE/STALE/CONFLICTING 对应事实输出 SATISFIED 计 critical regression。

### 4.4 Repair

Repair case 覆盖 `MOVE / SHIFT / REPLACE / INSERT_BREAK / INSERT_MEAL / REMOVE`，并固定以下不变量：

- 最多 A/B 两个方案；
- 先 preview，后 apply；
- 锁定预约、酒店、返程和成员 HARD 不得移动/删除；
- replace/insert 只能引用冻结候选和 Provider receipt；
- 应用后创建新 revision，不覆盖旧版本；
- 强制 full postcheck；
- 新增 HARD、把 UNKNOWN 变成 SATISFIED、缺 postcheck 的方案直接丢弃；
- 幂等重放不再生成 revision；
- 并发严格一个成功、一个 409。

## 5. Builder、真实来源与拖拽用例

### 5.1 候选质量

每个正常 Builder case 有 4～6 个冻结候选，并覆盖：

- `NEARBY / POPULAR / FUN / FOOD`；
- ON_ROUTE（≤15 分钟）、ACCEPTABLE_DETOUR（16～30）、DEFER_TO_OTHER_DAY（>30）、INFEASIBLE；
- wrong-city、wrong-category、canonical duplicate、闭馆、预约冲突、成员 HARD、Evidence UNKNOWN；
- Top-3 至少一个可用候选；
- 远距离地点保持可见并解释换日；
- 餐饮与体验多样性不被单一景点类别挤掉；
- nDCG@5、Recall@5、Top-3 usable、wrong-city、duplicate、HARD leak 分桶统计。

官方路线和开放用户内容只进入结构、标签、相邻和排序先验。当前事实仍由高德、天气和运营方来源重新取证。用户行为只有在完整记录曝光集合、排名、policy、context、snapshot 后才允许计算 acceptance/undo；脚本点击不能冒充真实采纳。

### 5.2 SuggestionSet 与原子接受

核心事务：

```text
SuggestionSet(base_revision, context_hash, policy_version, provider_snapshot)
→ 客户端只提交 suggestion_set_id + candidate_id
→ 服务端读取冻结 canonical POI/坐标/receipt
→ 单事务写 revision + projection + receipt + accepted event
→ 新地点成为下一轮 Anchor
```

拒绝：过期 set、stale revision、跨 workspace、未提供 candidate、HARD blocked、客户端夹带权威地点事实、幂等 key 复用到不同 candidate。

### 5.3 事件与恢复

完整因果链：

```text
suggestions_shown
→ candidate_previewed / candidate_dismissed
→ candidate_accepted(revision n→n+1)
→ suggestions_shown(new anchor)
→ stop_undone(revision n+1→n+2)
→ line_completed / suggestion_failed / revision_conflict
```

Backend/Yjs 重启和全新浏览器回读后，PostgreSQL 中的 revision、report、projection、members、receipts 和 events 必须仍一致；Yjs 只同步意图，不是事实源。

### 5.4 拖拽与移动按钮等价

两个相同 base revision 的独立 workspace 分别执行拖拽和按钮移动，忽略 command_id/client timestamp 后必须得到：

- 相同操作语义和 payload；
- 相同 days/stop 顺序，以及用逻辑 stop 占位符归一后的 revision semantic hash；每个 workspace 的原始 `content_hash` 仍必须在成功、409 回滚与 readback 内自洽，不能为了制造跨 workspace 的 hash 相等而复用全局 itinerary/stop 身份；
- 相同 changed days/edges/route delta/affected rules；
- 只刷新 changed edges；
- LLM 调用数 0；
- full Planner 调用数 0；
- 同样的 409 回滚行为；
- Undo 创建新 revision，不删除历史。

### 5.5 Final 2.0 Builder 扩展矩阵

本轮新增 18 条独立输入/label：`dev=12`、`regression=6`，北京、上海、杭州各新增 6 条；没有向 blind bootstrap 添加或复制任何 case。

| 城市 | dev 合同 case | regression 门禁种子 | 主要路线与边界 |
|---|---:|---:|---|
| 北京 | 4 | 2 | 故宫—景山—北海、前门—大栅栏—天坛、颐和园—圆明园；Anchor 查询、wrong-city/duplicate、冻结字段篡改、expired/stale/cross-workspace、幂等、accept rollback、四站新 Anchor、拖拽/按钮/并发/Undo/重启 |
| 上海 | 4 | 2 | 外滩—南京东路—豫园、新天地—思南路、武康路；插入边公式、四级 route delta、wrong-category、部分 Evidence UNKNOWN、set 生命周期、accept rollback、完整曝光/预览/拒绝/采纳事件链、Undo/重启回读 |
| 杭州 | 4 | 2 | 西湖—断桥—白堤—雷峰塔、灵隐—茶博、大运河—小河直街；多意图与多样性、成员 HARD/UNKNOWN、远距换日、插入边上下文、幂等冲突、冻结 set 全字段防篡改、四站 Anchor、拖拽/按钮/Undo/重启 |

扩展 schema 把以下信息从松散说明变为机器可校验合同：Anchor/插入边 request context、SuggestionSet 的 revision/context/policy/snapshot/expiry、canonical candidate/receipt/freshness/route legs、accept attempts、故障注入点、UI command pair，以及 query/set/accept/event/interaction/recovery 六类 oracle。

Validator 现在会拒绝：缺少上述对应 oracle 的 P5/G2/G5 case、三城种子失衡、已记录的 source/template/generator/mutation family 跨 development/blind、normalized input 重复、Builder canonical candidate sequence 重复、未知 case ID，以及把 seed-only 状态误写成已执行。数据集静态计数当前只具备 G2 四站 seed 3/9、G5 恢复 seed 6/9；独立九场景 Backend/Yjs 矩阵虽已实跑通过，但不会被伪写成额外数据集 seed，也不替代 G2。

## 6. Split 与污染防护

| Split | 当前数 | 作用 | 标签访问 |
|---|---:|---|---|
| pilot | 6 | schema/HTTP/scorer 调通 | development scorer |
| dev | 60 | Parser、Audit、Repair、排序与 P5 合同优化 | development scorer |
| regression | 12 | 已知失败和 G2/G5 Builder 门禁种子，只追加 | PR scorer |
| frozen_blind bootstrap | 18 | 晋级链路和隔离机制调通 | isolated scorer only |

污染检查按 `source_document / domain / source_family / template_family / generator_family / mutation_parent / normalized input hash / canonical POI sequence` 执行。`normalized_input_sha256` 对 `case.input` 进行 Unicode NFC、CRLF/CR→LF、object key 排序和紧凑 UTF-8 JSON 编码后逐字节重算；hash 重复即 G0 失败，不能只信声明字段。Builder canonical sequence 在任何 split 重复同样失败。

本版没有足够历史日志证明 Template/Generator family，也没有证据证明 64 条旧 `controlled_mutation` 的父 case。为避免制造不可证伪的 case-ID family，这些数据已改为 `high_fidelity_synthetic`，mutation 标为 `NOT_APPLICABLE`；template/generator 使用共享 unavailable sentinel 并标 `UNAVAILABLE`。只有实际绑定 source registry 归档集合的 6 条 case 使用可重算的 `RECORDED` source family。未来若记录真实 family 或 parent，validator 会对其跨 development/blind 关系 fail closed；当前 lineage 不可用本身保持 release warning。

Receipt 索引也由 bytes 反算：source receipt 必须逐项等于 registry 的 raw/extract SHA-256；静态 input subject 必须逐项绑定 `subject_receipt_registry.jsonl` 中的 case/path/subject/input hash，case ref 还要绑定 record SHA-256。旧统计的 25 case /111 个“缺 Provider receipt”逐路径重分为：Amap snapshot 精确绑定 0、development controlled fixture 90、frozen-blind `UNAVAILABLE` 21。没有按名称、坐标或当前时间补造调用。

完整枚举包含 277 个静态 subject：242 条 `CONTROLLED_FIXTURE_EXECUTION`、35 条 `UNAVAILABLE`、0 条真实 Provider receipt。比旧检查多发现了 6 个 blind seed、8 个 blind Import controlled fact，以及本轮 18 个 Import development controlled-fact subject；重复 POI 的不同 stop/path 使用不同 receipt ID。Fixture receipt 统一声明 `provider_call_attempted=false / current_fact_authority=false / live_provider_evidence=false / observed_at=null`，只证明受控输入 bytes。Import rejected candidate、Builder candidate/route leg/current fact 的真实 receipt 仍必须由运行期 `provider_receipts.jsonl` 提供；source prior/UGC/官方结构先验不得替代。35 条 blind unavailable 与运行时 Provider 覆盖缺口继续使 release fail closed。

仓库中的 `sealed/frozen_blind.labels.jsonl` 仅为 bootstrap。正式 blind 必须迁移到 SUT、Generator 和 Judge 无法读取的 CI secret artifact，产品输出完成后才挂载给 deterministic scorer。

## 7. G0–G6 门禁

| Gate | 运行内容 | 关键阈值 |
|---|---|---|
| G0 | schema、hash、source terms、scope、split、预算、blind 隔离 | 任一缺失停止；使用来源 raw/extract hash 不得为空 |
| G1 | unit/property/PostgreSQL/fixture browser；禁付费 API | failure=0，UNKNOWN→SATISFIED=0，静默错配=0，拖拽 LLM/Planner=0 |
| G2 | 30 条真实 HTTP import + 9 条四站 snapshot session | parse F1≥0.95，span/地点坐标receipt/postcheck=100%，P/R≥0.90/0.85，nDCG@5≥0.80 |
| G3 | 独立 5.6-sol A/B+B/A Judge | schema/fact ID=100%，各维≥4/5，关键幻觉=0 |
| G4 | 三城每周 live | ≥30 case，各城≥10，全链≥95%，candidate P95<3s，fixture fallback=0 |
| G5 | 双浏览器、并发、四站、拖拽/按钮、Undo、Backend/Yjs 重启 | 9 条产品恢复场景全部通过 |
| G6 | 同一 commit/config/data/snapshot 的全门禁重跑和 paired promotion | critical regression=0，95% CI 下界≥-0.02，任一 bucket 回退≤2pp |

Release blind 的统计目标：90 import、45 builder suggestion、24 fault/recovery。当前仍为 12/6/5；新增 18 条全部属于 dev/regression，不能计入 blind。G2 四站 session 当前仅 3/9 且真实产品 HTTP 在第二轮因候选→候选路线证据缺失而拒绝；G5 数据集 seed 仍为 6/9，另有 9/9 独立本地进程恢复矩阵 PASS。final gate 仍必须拒绝晋级。

## 8. 执行命令

结构校验：

```powershell
$env:PYTHONPATH="backend"
python -m scripts.validate_dual_entry_testset
python -m pytest -q backend/tests/test_dual_entry_testset_contract.py
```

要求 release ready 时，当前应失败并列出缺口：

```powershell
python -m scripts.validate_dual_entry_testset --require-release-ready
```

当前 runner 接口：

```powershell
cd backend
python -m evals.continuous validate --spec evals/run_specs/dual-entry-pr-offline.json
python -m evals.continuous run-import-http --spec evals/run_specs/dual-entry-pr-offline.json
```

当前 runner 已能执行 PR controlled-fixture Import HTTP slice，并产出 hash-bound preflight、HTTP transactions、product outputs、Provider receipts、deterministic scores、bad cases、cost 和 gate。PR 引用的北京/上海官方路线最小归档已绑定真实 raw/extract hash，preflight 8 项检查通过。Nightly snapshot、weekly live、release blind 仍只有 fail-closed RunSpec/foundation，不能宣称已执行。

## 9. 当前预期红灯与开发顺序

测试集故意把以下现状定义为预期红灯：

1. import candidate/apply 缺 canonical 坐标和完整 Provider receipt 的事务化物化；
2. 初始 Audit 不会自动完整采集 POI、相邻 route edge、weather 和官方事实；
3. Markdown 表格、自然段多地点、越界 day、全天等 Parser 边界；
4. wrong-city、路线空档不足、固定预约、返程、到达/离开日规则缺口；
5. 最后一天酒店和半日餐食误报；
6. Repair 主要只覆盖时间链和重复地点；
7. 真实 Seed 搜索、冻结 SuggestionSet、原子 accept、RecommendationEvent 尚未形成；
8. 候选查询仍偏“城市+类别”，没有完整 Anchor/插入边、HARD gate、MMR/配额和来源账本；
9. 新接受地点的 canonical projection/receipt/Anchor 连续性不足；
10. Nightly frozen snapshot 与独立 blind scorer 已实现；但四站 G2、weekly live、外部 blind bundle 和 Baseline/Candidate promotion 尚未完成。PR Import HTTP slice 已实现。

后续开发固定从第 1～4 项开始，因为它们决定“导入后检查是否准确、真实有效、有帮助”；再建设 SuggestionSet/accept/event，最后才扩大正式 blind、Live 和浏览器门禁。
