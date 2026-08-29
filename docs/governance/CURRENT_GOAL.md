# IN_PROGRESS GOAL：V0.3 Top-3 核心行程查

Goal ID: TC-VNEXT-G03-TOP3-AUDIT
Status: IN_PROGRESS
Goal type: PRODUCT_VERTICAL_SLICE

<!-- PRODUCT_DELIVERY_CURRENT_GOAL_STATE
{
  "schema_version": "product-delivery-current-goal-state-v1",
  "program_id": "TC-VNEXT-2026",
  "goal_id": "TC-VNEXT-G03-TOP3-AUDIT",
  "goal_status": "IN_PROGRESS",
  "gate_profile": "PRODUCT_DELIVERY_GATE",
  "required_gate": "Top-3 Audit Gate + PRODUCT_DELIVERY_PASS",
  "completion_status": "DELIVERY_VERIFIED_PENDING_INTEGRATION",
  "gate_result": "PRODUCT_DELIVERY_PASS",
  "goal_archived": false,
  "next_goal_id": "CORE_MVP_OWNER_REVIEW_PENDING",
  "next_activated": false,
  "h1_status": "NOT_RUN",
  "production_status": "NOT_RUN",
  "commercial_status": "NOT_RUN"
}
-->

## Metadata

- Goal ID：`TC-VNEXT-G03-TOP3-AUDIT`
- Program ID：`TC-VNEXT-2026`
- Product version：`V0.3`
- Mainline phase：`CORE_MVP`
- Gate profile：`PRODUCT_DELIVERY_GATE`
- Status：`IN_PROGRESS`
- Goal type：`PRODUCT_VERTICAL_SLICE`
- Branch：`codex/g03-top3-audit`
- Canonical predecessor：`origin/develop@1ef2e140cbafdef602a5a9a0fa824751b20b5bae`，G02产品PR #9合并与远端readback `PASS`
- Activation：G02已通过`PRODUCT_DELIVERY_PASS`、经PR #9进入`origin/develop`并完成归档
- Required gate：`Top-3 Audit Gate + PRODUCT_DELIVERY_PASS`
- Terminal state：`CORE_MVP_OWNER_REVIEW_PENDING`；G04：`NOT_ACTIVATED`

## Dependencies

- 唯一激活依赖是G02归档且Map & Stay Gate通过；该依赖已由G02交付回执、PR #9和`origin/develop@1ef2e140cbafdef602a5a9a0fa824751b20b5bae`远端readback满足。
- G03产品分支已在治理过渡PR #10合并后从`origin/develop@5a79c129f13914f4cd5a01789e641ada56d0b486`创建；首个preflight已回读exact activation subject、PlanRevisionRef、map/stay pointer和旧TripBrief/Itinerary日期约束。缺失lane标记`NOT_READY`而不伪造确认，并继续可独立完成的安全切片。

## User Outcome

用户只看到最值得处理的三个“必须调整 / 可以更好 / 需要确认”问题，并能预览、采纳最小修改。采纳后生成新行程版本，地图提示需要更新，完整复检后才显示已解决。

## Scope

- `031_day_index_trip_bridge.sql`、`MaterializationLineage`与正式materialize到首个ItineraryRevision；
- 地点存在/城市归属；
- 营业、闭馆、预约；
- walking/transit路线耗时和日容量；
- 酒店往返、午餐和晚餐空档；
- 有日期时的天气和风险；
- Top-3确定性排序；
- 用户友好Finding/Advice投影；
- Repair preview、EditCommand、新revision和postcheck。
- 公共`GET checks`、`POST changes/preview`和`POST changes/adopt`，严格脱敏并使用不透明token；
- 无真实日历日期的`DAY_INDEX_ONLY`和人数默认2的来源标记；
- 采纳使用ETag、Idempotency-Key和所有权校验，零路线Provider调用并令地图进入`NEEDS_UPDATE`。

## Pre-approved actions

- v3 materialize和Top-3用户结果；
- 复用现有Evidence/Audit/Advice/Repair；
- 必需的`031_day_index_trip_bridge.sql`，支持`ABSOLUTE|DAY_INDEX_ONLY`、nullable calendar与软人数来源；
- 现有高德/和风开发矩阵；
- 不新增风险搜索Provider。

## Parallel work packages

G03只保留一个主集成包`WP-G03-INTEGRATOR`。唯一总指挥在`codex/g03-top3-audit`和同一个干净主线工作树中串行完成物化与lineage → Evidence/Audit/Top-3 → preview/adopt/postcheck → 公共UI/E2E。不开启并行产品writer，不生成额外prompt/branch/worktree，不建设新的治理或运行时多Agent体系；短期只读诊断也不得修改产品或Goal状态。

## Decisions locked

- AuditEngine是Finding唯一权威。
- 内部保留全部MUST_ADJUST；公共页只展示前三项，剩余数量可见并按解决顺序补位。
- 无日期不生成具体天气/闭馆日期HARD。
- 用户结果最多3个，内部报告可保留更多。
- 具体地点候选必须来自冻结CandidateSet。
- LLM只表达已选事实和建议。
- 采纳后地图stale，不自动重算。
- 完整postcheck前不能显示已解决。
- 公共映射固定为：高严重度`VIOLATED`＝“必须调整”，中低严重度＝“可以更好”，`UNKNOWN`＝“需要确认”；内部保留全部未解决硬问题，公共层最多3条并返回剩余“必须调整”数量。

## Non-goals

- 截图；
- 时段/热门/夜景知识库；
- 用户记忆与分享；
- 新风险搜索Provider；
- 实时客流、医疗、订票、最低价。

## Acceptance

完全继承Top-3 Audit Gate：

- HARD漏检0；
- route precision/recall≥90%；
- CandidateSet/receipt绑定100%；
- 用户结果≤3；
- Repair后新增BLOCKER/HIGH/UNKNOWN为0；
- postcheck前错误“已解决”为0；
- 无日期的具体时效HARD为0；
- Provider局部失败保留成功事实。
- DAY_INDEX_ONLY可materialize且不伪造日期/确认；lineage、ETag、地图/住宿current pointer一致。

## Verification

- rule/oracle单测；
- Evidence freshness和partial failure；
- frozen CandidateSet；
- PostgreSQL materialize/audit/repair/postcheck；
- map stale联动；
- browser Top-3/preview/adopt/refresh；
- fault matrix和snapshot replay；
- PostgreSQL 031 fresh/existing、ABSOLUTE/DAY_INDEX_ONLY和旧数据兼容；
- H1/生产：`NOT_RUN`。
- 当前Top-3/最小修复定向测试、PostgreSQL、frontend build和浏览器E2E；候选复审与blind留到G07。

## Authority

- `AGENTS.md`、Charter、Spec、v3 API、Architecture；
- Program、Roadmap、Release Gates、Product Delivery Gate、Provider Admission、Risk Register；
- ADR-007、ADR-008、ADR-011、ADR-012、ADR-013、ADR-014及现有Audit/Repair ADR中未被取代的证据不变量。

## Baseline

- Implementation branch：`codex/g03-top3-audit`；exact activation baseline/upstream：`origin/develop@5a79c129f13914f4cd5a01789e641ada56d0b486`；治理过渡commit `ee85d912fe1a73495b7f7c2dc1618c8f6fd7cb28`经PR #10合并，GitHub `core-mainline` run `33267336522 PASS`；
- G02 product/delivery/integration：`c6e8b5ef248b9c0d0169bfe4088eac30ff5a26cd` / `19823105ed64403bdf8e2d6820ed839112ab5508` / `1ef2e140cbafdef602a5a9a0fa824751b20b5bae`；远端CI与readback `PASS`；
- 现场记录旧TripBrief日期/人数硬约束、Audit规则、CandidateSet与Provider版本；
- 历史Audit PASS不适用于新materialization lineage；H1/生产：`NOT_RUN`。

## Invariants

- AuditEngine是Finding唯一权威，LLM只表达；CandidateSet/receipt不可由模型补造；
- 所有HARD内部保留，公共Top-3不把剩余项显示为通过；
- `DAY_INDEX_ONLY`不生成日期天气/临时闭馆HARD，不伪造用户确认；
- materialize/repair使用CAS、幂等、lineage和新revision；postcheck前不得显示已解决；
- 采纳后地图为`NEEDS_UPDATE`，不自动路线调用；partial/UNKNOWN不算PASS。

## Budget

- Audit规则与Provider调用按RunSpec固定deadline/retry；具体地点候选最多来自冻结CandidateSet，不允许模型扩展；
- Top-3表达最多一次LLM调用且不得改变Finding/Repair；所有调用记账；
- 只允许现有无增量费用且已准入高德/和风开发矩阵；每切片checkpoint。

## HITL

新风险Provider、031以外公共schema、费用/账号/数据扩大、sealed oracle、H1/公网/生产/`main`需要批准；普通规则/迁移/测试故障自主修复。

## Checkpoint ledger

| 时间 | 用户结果 | Commit | Verification | Evidence level | Product progress | Governance ratio | Remaining | Risk/failure | Next autonomous action |
|---|---|---|---|---|---|---|---|---|---|
| 2026-08-30 | G02已并入`origin/develop`并完成归档，G03 Top-3核验与最小修复主线激活；尚未修改G03产品代码 | transition pending remote subject | G02 GitHub `core-mainline` run `33266880055 PASS`；PR #9合并；`origin/develop@1ef2e140cbafdef602a5a9a0fa824751b20b5bae` readback `PASS` | `PRODUCT_DELIVERY_PASS / REMOTE_INTEGRATION_PASS` | `Product progress=NONE / GOAL_TRANSITION` | `Governance ratio=100% / atomic transition only` | `031`、materialize、Top-3、preview/adopt/postcheck和浏览器主链 | live Provider、H1、公网、生产、商业保持`NOT_RUN`；G04不得自动激活 | 合并本治理过渡PR后，从新的`origin/develop`创建`codex/g03-top3-audit`并实现最小纵向切片 |
| 2026-08-30 | 用户可在登录后查看稳定Top-3，预览并采纳安全的最小改动；两种日期模式、lineage、冻结证据、完整postcheck和地图需更新联动均已交付 | product `68f7f9e` | G03 targeted/G02-v3回归 `16 PASS`；G03 PostgreSQL与fresh/existing migration `3 PASS`；frontend build、client build、OpenAPI check、G03 Playwright `1 PASS`；core-mainline `PASS`；product fingerprint `0c09093bfa1eca32942bf68d3b5d665d0470604139d48968e127ff2cad9c54a1` | `CONTROLLED_FIXTURE / REAL_POSTGRESQL / LOCAL_BROWSER / PRODUCT_DELIVERY_PASS` | `Product progress=API+RUNTIME+UI` | `Governance ratio=delivery receipt and checkpoint only` | 产品PR push、远端CI/readback与并入`develop`；随后独立治理PR归档G03并停在owner review | 旧Docker worker竞争曾造成租约接管延迟，隔离旧worker后浏览器复验通过；live Provider、H1、公网、生产、商业仍为`NOT_RUN` | 提交交付回执，push并读取远端subject/CI；合并G03后执行最小终态治理切换，不激活G04 |

## Auto-advance

- Required gate：`Top-3 Audit Gate + PRODUCT_DELIVERY_PASS`；terminal state：`CORE_MVP_OWNER_REVIEW_PENDING`；
- subject push/readback、耐久`PRODUCT_DELIVERY_PASS`、clean tree、无Stop后，归档G03并把唯一工作包置为已合并且不激活新写入者；保存可体验里程碑并切换为`CORE_MVP_OWNER_REVIEW_PENDING`。G04固定`NOT_ACTIVATED`，不得自动激活G04，需等待项目所有者体验验收；
- FUX-03、H1、公网、生产、商业和`main`不自动启动。
- H1、公网、生产、商业：`NOT_RUN`。

## Completion record

- Status：`IN_PROGRESS`；Subject commit：G03 product `68f7f9e`；Remote branch：`origin/codex/g03-top3-audit`，本次产品push/readback待执行；
- Verification / Evidence / Gate result / `structurally_valid`：`LOCAL_AUTOMATED_REGRESSION_COMPLETE / CONTROLLED_FIXTURE + REAL_POSTGRESQL + LOCAL_BROWSER / PRODUCT_DELIVERY_PASS / true`；结构有效不替代产品证据；
- H1 / production / commercial：`NOT_RUN / NOT_RUN / NOT_RUN`；live Provider与公网也为`NOT_RUN`；
- User-visible result：登录用户可查看最多三项“必须调整 / 可以更好 / 需要确认”，预览并采纳午餐停留，随后获得新版本与完整复核结果；Remaining risks：产品PR尚未并入`origin/develop`，owner体验验收尚未执行；Goal archived：`NO`；Next activated：`NO`；
- Promotion decision：`NOT_REQUESTED`。

## Stop conditions

- 需要新风险Provider或未准入数据；
- 未解决MUST_ADJUST无法进入内部队列或公共剩余数量；
- 现有Audit权威必须被LLM替代；
- 需要降低HARD或receipt门禁；
- 日期缺失无法通过明确桥接保持UNKNOWN且不伪造日期。
