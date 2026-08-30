# COMPLETED GOAL：V0.3 Top-3 核心行程查

Goal ID: TC-VNEXT-G03-TOP3-AUDIT
Status: COMPLETED
Goal type: PRODUCT_VERTICAL_SLICE

<!-- PRODUCT_DELIVERY_CURRENT_GOAL_STATE
{
  "schema_version": "product-delivery-current-goal-state-v1",
  "program_id": "TC-VNEXT-2026",
  "goal_id": "TC-VNEXT-G03-TOP3-AUDIT",
  "goal_status": "COMPLETED",
  "gate_profile": "PRODUCT_DELIVERY_GATE",
  "required_gate": "Top-3 Audit Gate + PRODUCT_DELIVERY_PASS",
  "completion_status": "DELIVERY_INTEGRATED",
  "gate_result": "PRODUCT_DELIVERY_PASS",
  "goal_archived": true,
  "next_goal_id": "TC-VNEXT-G04-SCREENSHOT",
  "next_activated": false,
  "g04_status": "NOT_ACTIVATED",
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
- Status：`COMPLETED`
- Goal type：`PRODUCT_VERTICAL_SLICE`
- Branch：`codex/g03-top3-audit`
- Activation baseline：`origin/develop@5a79c129f13914f4cd5a01789e641ada56d0b486`
- Canonical integration subject：`origin/develop@3999ee583faf4c14ff5c16e2d573bf5cc12689cb`，产品PR #11合并与远端readback `PASS`
- Required gate：`Top-3 Audit Gate + PRODUCT_DELIVERY_PASS`
- Terminal state：`CORE_MVP_OWNER_REVIEW_PENDING`；G04：`NOT_ACTIVATED`

## Dependencies

- G02已通过`PRODUCT_DELIVERY_PASS`、经PR #9进入`origin/develop`并完整归档。
- G03治理过渡PR #10经GitHub `core-mainline` run `33267336522 PASS`后并入`origin/develop@5a79c129f13914f4cd5a01789e641ada56d0b486`。
- 产品分支从该精确基线创建；旧TripBrief日期/人数约束、Audit规则、CandidateSet及地图/住宿pointer均已在首个preflight回读。

## User Outcome

用户只看到最值得处理的三个“必须调整 / 可以更好 / 需要确认”问题，并能预览、采纳最小修改。采纳后生成新行程版本，地图提示需要更新，只有完整复检后才显示新结果。

## Scope

- `031_day_index_trip_bridge.sql`、`MaterializationLineage`与正式materialize到首个不可变`ItineraryRevision`；
- 无真实日历日期的`DAY_INDEX_ONLY`与人数默认2的来源标记；
- 地点、路线、日容量、住宿通勤、餐饮及Provider失败的冻结证据快照；
- AuditEngine全量Finding、公开Top-3确定性排序与剩余“必须调整”数量；
- 公共`GET checks`、`POST changes/preview`、`POST changes/adopt`；
- ETag、Idempotency-Key、所有权校验、不透明token、新revision和完整postcheck；
- 采纳零路线Provider调用，旧地图自动投影为`NEEDS_UPDATE`；
- 公开JSON和DOM严格脱敏。

## Pre-approved actions

- v3 materialize和Top-3用户结果；
- 复用现有Evidence/Audit/Advice/Repair边界；
- 追加式`031_day_index_trip_bridge.sql`；
- 现有无增量费用高德/和风开发矩阵；
- 不新增风险搜索Provider、账号、费用或生产调用。

## Parallel work packages

G03只使用一个主集成包`WP-G03-INTEGRATOR`。唯一总指挥在`codex/g03-top3-audit`和同一个干净主线工作树中串行完成物化与lineage、Evidence/Audit/Top-3、preview/adopt/postcheck、公共UI与E2E；未创建并行产品writer或运行时多Agent。

该包ready commit为`3009372c07fd5b82a76655b2df5c831f4698ec93`，经产品PR #11并入`3999ee583faf4c14ff5c16e2d573bf5cc12689cb`，状态为`MERGED`。

## Decisions locked

- AuditEngine是Finding唯一权威；LLM不能生成EvidenceFact、Finding或“已解决”状态。
- 内部保留全部未解决硬问题；公共页最多展示三项并返回剩余“必须调整”数量。
- 无日期不生成依赖真实日期的天气、闭馆或时段硬结论。
- 公共映射固定为：高严重度`VIOLATED`＝“必须调整”，中低严重度＝“可以更好”，`UNKNOWN`＝“需要确认”。
- 排序依次使用严重度、证据确定性与新鲜度、可执行性、影响范围和稳定规则键。
- 采纳后地图stale，不自动重算；完整postcheck前不得显示问题已解决。
- 具体地点候选只来自冻结CandidateSet，局部失败保持`UNKNOWN/UNAVAILABLE`。

## Non-goals

- 截图输入；
- 时段、热门和夜景知识库；
- 用户记忆与分享；
- 新风险搜索Provider；
- 实时客流、医疗、订票、最低价；
- FUX-03/H1真人、公网、生产、商业、发布、部署或合并`main`。

## Acceptance

- `ABSOLUTE_DATES`与`DAY_INDEX_ONLY`均可物化，后者不伪造日期或确认；
- 人数明确区分用户提供与默认2人；
- understanding→itinerary lineage、current plan ref、CAS与幂等重放正确；
- CandidateSet与实际回执绑定，Provider局部失败不假PASS；
- AuditEngine保存全部硬Finding，公共Top-3稳定排序并在解决后补位；
- preview→adopt创建新revision，复制住宿锚点，采纳路线Provider调用为0；
- 旧地图进入`NEEDS_UPDATE`，只有完整postcheck产生新公开结果；
- 公共JSON与DOM不泄漏Evidence、Audit、Repair、Postcheck、UID、hash、receipt或revision。

## Verification

- G03 targeted与既有G02/v3回归：`16 PASS`；
- G03 PostgreSQL、fresh/existing migration：`3 PASS`；
- frontend production build：`PASS`；
- trip-check client generate/typecheck/build与OpenAPI check：`PASS`；
- G03 Playwright“卡片→地图→住宿→检查→预览→采纳→复核”：`1 PASS`；
- 本地core-mainline及指纹绑定交付回执：`PASS`；
- GitHub PR #11 `core-mainline` run `33269598302 PASS`；
- live Provider、FUX-03/H1、公网、生产、商业：`NOT_RUN`。

## Authority

- `AGENTS.md`、Charter、Spec、v3 API Contract与Architecture；
- Program、Roadmap、Release Gates、Product Delivery Gate、Provider Admission和Risk Register；
- ADR-007、ADR-008、ADR-011、ADR-012、ADR-013、ADR-014及未被取代的Audit/Repair不变量。

## Baseline

- Implementation branch：`codex/g03-top3-audit`；activation baseline/upstream：`origin/develop@5a79c129f13914f4cd5a01789e641ada56d0b486`；
- G03 product commit：`68f7f9e01f0b5c5ecfa8b6286bf57394838b0c37`；
- G03 delivery commit：`3009372c07fd5b82a76655b2df5c831f4698ec93`；
- Product fingerprint：`0c09093bfa1eca32942bf68d3b5d665d0470604139d48968e127ff2cad9c54a1`；
- Product PR #11 integration：`origin/develop@3999ee583faf4c14ff5c16e2d573bf5cc12689cb`；remote subject与祖先readback `PASS`；
- G02 product/delivery/integration：`c6e8b5ef248b9c0d0169bfe4088eac30ff5a26cd` / `19823105ed64403bdf8e2d6820ed839112ab5508` / `1ef2e140cbafdef602a5a9a0fa824751b20b5bae`。

## Invariants

- AuditEngine是Finding唯一权威，CandidateSet/receipt不能由模型补造；
- 所有硬问题内部保留，公共Top-3不能把剩余项显示为通过；
- `DAY_INDEX_ONLY`不生成日期天气/临时闭馆HARD，不伪造用户确认；
- materialize与adopt使用CAS、幂等、lineage和新revision；
- 采纳后地图为`NEEDS_UPDATE`且不自动路线调用；partial/UNKNOWN不算PASS；
- PostgreSQL仍是revision、run、幂等、receipt和lineage的唯一事实源。

## Budget

- 只运行G03最小充分测试、真实PostgreSQL、前端构建与浏览器主链；
- Top-3表达不得改变Finding或Repair，具体地点只来自冻结CandidateSet；
- 只使用已有无增量费用Provider开发矩阵；未运行live Provider；
- 未新增微服务、队列、运行时多Agent或非必要基础设施。

## HITL

新风险Provider、额外公共schema、费用/账号/数据扩大、sealed oracle、FUX-03/H1、公网、生产、发布、部署、商业或`main`需要项目所有者另行批准；本Goal未请求这些权限。

## Checkpoint ledger

| 时间 | 用户结果 | Commit | Verification | Evidence level | Product progress | Governance ratio | Remaining | Risk/failure | Next autonomous action |
|---|---|---|---|---|---|---|---|---|---|
| 2026-08-30 | G02已集成并归档，G03主线从精确新基线激活 | transition `ee85d912fe1a73495b7f7c2dc1618c8f6fd7cb28`；integration `5a79c129f13914f4cd5a01789e641ada56d0b486` | PR #10；GitHub run `33267336522 PASS`；远端readback `PASS` | `REMOTE_AUTOMATED / GOAL_TRANSITION` | `Product progress=NONE / GOAL_TRANSITION` | `Governance ratio=100% / atomic transition only` | G03产品实现和验收 | live Provider、真人、公网、生产、商业不在本Goal | 从新`origin/develop`创建唯一G03产品分支并交付纵向主链 |
| 2026-08-30 | 登录用户可查看稳定Top-3，预览并采纳安全最小改动；两种日期模式、lineage、冻结证据、完整postcheck和地图需更新联动已交付 | product `68f7f9e01f0b5c5ecfa8b6286bf57394838b0c37`；delivery `3009372c07fd5b82a76655b2df5c831f4698ec93` | targeted `16 PASS`；PostgreSQL `3 PASS`；frontend/client/OpenAPI `PASS`；Playwright `1 PASS`；product fingerprint已绑定 | `CONTROLLED_FIXTURE / REAL_POSTGRESQL / LOCAL_BROWSER / PRODUCT_DELIVERY_PASS` | `Product progress=API+RUNTIME+UI` | `Governance ratio=delivery receipt and checkpoint only` | 远端CI、产品PR合并与终态治理收口 | 旧Docker worker竞争导致过租约延迟；隔离旧worker后复验通过 | push/readback，提交产品PR并等待远端主线验收 |
| 2026-08-30 | G03产品经PR #11并入`origin/develop`，G01～G03核心主线可体验 | delivery `3009372c07fd5b82a76655b2df5c831f4698ec93`；integration `3999ee583faf4c14ff5c16e2d573bf5cc12689cb` | GitHub run `33269598302 PASS`；PR #11 `MERGED`；远端subject与祖先readback `PASS` | `REMOTE_AUTOMATED / PRODUCT_DELIVERY_PASS / REMOTE_INTEGRATION_PASS` | `Product progress=API+RUNTIME+UI / COMPLETE` | `Governance ratio=terminal transition only after integration` | 项目所有者体验验收 | FUX-03/H1、公网、生产、商业仍`NOT_RUN`；release、部署、main均`NOT_REQUESTED` | 归档G03，关闭唯一writer并停在`CORE_MVP_OWNER_REVIEW_PENDING` |

## Auto-advance

- G03 subject已push/readback、耐久`PRODUCT_DELIVERY_PASS`已物化、PR #11已合并；
- 本归档与owner-review状态在独立治理过渡中原子完成；
- 自动推进在`CORE_MVP_OWNER_REVIEW_PENDING`终止；G04固定`NOT_ACTIVATED`，不得自动激活G04；
- FUX-03/H1、公网、生产、商业、发布、部署和`main`不自动启动。

## Completion record

- Status：`COMPLETED`；Goal archived：`YES`；Next activated：`NO`；
- Subject commits：product `68f7f9e01f0b5c5ecfa8b6286bf57394838b0c37`、delivery `3009372c07fd5b82a76655b2df5c831f4698ec93`；
- Remote branch：`origin/codex/g03-top3-audit`；canonical integration：`origin/develop@3999ee583faf4c14ff5c16e2d573bf5cc12689cb`；
- Verification / Evidence / Gate：`REMOTE_AUTOMATED_REGRESSION_COMPLETE / CONTROLLED_FIXTURE + REAL_POSTGRESQL + LOCAL_BROWSER / PRODUCT_DELIVERY_PASS`；GitHub run `33269598302 PASS`；
- User-visible result：用户可完成“粘贴攻略→卡片→地图→住宿→检查→预览→采纳→完整复核”主链；
- Remaining risks：live Provider、FUX-03/H1、公网、生产和商业未运行，不得外推为对应证据；
- Terminal state：`CORE_MVP_OWNER_REVIEW_PENDING`；G04：`NOT_ACTIVATED`；
- Release / deployment / main merge：`NOT_REQUESTED / NOT_REQUESTED / NOT_REQUESTED`；
- Promotion decision：`NOT_REQUESTED`；`structurally_valid=true`只表示合同结构有效。

## Stop conditions

- 项目所有者尚未完成体验验收并批准G04；
- 任何尝试把自动化结果表述为真人、生产或商业证据；
- 任何尝试自动激活G04、创建新writer、部署、发布或合并`main`；
- 任何需要降低G03 Gate、修改sealed oracle或扩大外部数据/费用的提案。

---

# COMPLETED ADDENDUM：G03R 语义、地点与结果页 P1 返修

Goal ID：`TC-VNEXT-G03-TOP3-AUDIT`；repair slice：`G03R-SEMANTIC-PLACE-UI-P1`；Status：`COMPLETED / ARCHIVED`；完成日期：`2026-08-30`。

## User outcome

用户粘贴攻略后，逐日卡片只保留真正计划到访的原子地点并保持日序与日内顺序；推荐、经过、排除、描述、URL和预约噪声不进入行程。北京、上海、杭州使用版本化900地点词典形成检索候选，但只有通过城市、类别、行政区和唯一性校验的Provider结果才能自动匹配，证据不足时保守显示“地点待确认”。结果页稳定保留Top-3，支持桌面拖拽、键盘和移动端等价编辑，并以服务端回读处理失败和409。

## Dependencies and scope

- 精确基线：`origin/develop@8a33a4b22a405135f310376d8766d9170d80097d`；所有者授权：`OWNER_APPROVED_G03_P1_REPAIR_2026-08-30`与`OWNER_APPROVED_G03_PLACE_REPAIR_2026-08-30`；
- 语义包：`dd26967ea3d04453a7aac2e52017088d4b7c829b`，集成为`a16e3a93a4a56ff2a81fce4cde1332885c46afd6`；
- 地点包：`d554b0d73c2b8d2ce93bf1adb93ab6412904536d`，集成为`6149f51ef8d13025846b50c329f174b31288c3ef`；
- UI累计候选：原UI `994ac8557f1d507787b9ca26e724d7df684d3faa`、稳定性`030d2129736ac354a4febe6631e8141098e70a75`、增强恢复`7fb559d071f03da940c398f1dafc0372f1bb9a48`、最终证据`791fe1a7135256ac205790dccff980291c237195`，最终集成映射`93e8a466f0409ae9e898a906c9d101012b656c88`；
- delivery checkpoint：`2d74a88ffd97988f50fbaa271b5d66f7411c2155`；PR #13 integration：`origin/develop@1f5a93c62aeeefc83486778d446780710977529d`；产品指纹：`aa76dbafb3fe48d28f4e54efcbac1530aa2bf0c706c86a6e8facbf6d7b5079ee`。

## Non-goals and authority

本返修未新增公共API、migration、依赖、媒体代理、运行时多Agent或自动地图重绘；未修改sealed blind/oracle；未激活G04。权限来自项目`AGENTS.md`、产品合同、Program、当前Goal以及上述所有者授权。部署、发布、`main`合并、新账号、费用、FUX-03/H1、公网、生产和商业均不在授权内。

## Invariants and acceptance

- 只有有原子地点的`PLANNED`提及可自动搜索POI；错城、错类别、行政区冲突、同层歧义与Provider不足必须待确认；
- 卡片编辑只产生新revision并令旧地图`NEEDS_UPDATE`，不得自动请求`map-renders`；
- 地图/住宿增强读取真实可取消、单飞、单请求3秒、总预算10秒、最多8轮；失败端诚实降级且成功端保留；
- 同日、跨日、空日移动每个有效操作只发送一个命令；无变化不发送；失败与409以权威结果复原；
- 公共JSON与DOM不出现Provider URL、内部revision、hash、receipt、模型、置信度或原文映射；
- PostgreSQL继续作为revision、run、幂等、receipt和lineage唯一事实源。

## Gate and verification

- 固定72条Qwen开发比较每版本只运行一次；最终`72/72`可比较、0禁入、0额外PLANNED、432/432计划召回、720/720五角色、432/432日序顺序；这是`LIVE_QWEN_NONBLIND_DEVELOPMENT_DIAGNOSTIC`，不是sealed blind；
- 语义、地点和既有v3组合回归：`131 PASS`；Top-3非数据库：`3 PASS`；G03 PostgreSQL隔离链：`1 PASS`；固定数据：`90 VALID / blind Gate NOT_RUN`；
- frontend production build、OpenAPI drift、trip-check client build：`PASS`；结果页E2E：`29/29`；默认3 workers、retries=0的repeat3：`87/87`；
- 清空真实Qwen/高德密钥的`local_fixture`完整旅程：`1/1 PASS`；GitHub PR #13 `core-mainline` run `33307676730 PASS`；PR状态`MERGED`；
- 交付结果：`PRODUCT_DELIVERY_PASS / REMOTE_INTEGRATION_PASS`；live AMap矩阵因零增量额度未证明保持`NOT_RUN / 0 calls`。

首次主集成repeat3曾得到`86/87`：trace显示Next dev bundle语法错误、DOM停在SSR loading且0次fixture API请求。清理并重建缓存、预热bundle后完整87项通过；该环境诊断保留，未用retry或单worker掩盖。

## Budget, HITL and stop conditions

模型、schema、deadline、token、temperature和retry保持冻结；没有新增Provider费用。sealed blind、FUX-03、H1、公网、生产、商业、发布、部署与`main`均为`NOT_RUN/NOT_REQUESTED`。只有项目所有者完成体验验收并明确批准进入G04，才可从届时最新`origin/develop`激活新Goal；否则停止所有产品writer和自动推进。

## Checkpoint and completion record

| 用户结果 | Subject / integration | 自动验证 | 证据边界 | 下一状态 |
|---|---|---|---|---|
| 准确语义、保守地点与稳定结果页返修已进入`develop` | `2d74a88ffd97988f50fbaa271b5d66f7411c2155` / `1f5a93c62aeeefc83486778d446780710977529d` | local全矩阵PASS；GitHub run `33307676730 PASS`；PR #13 MERGED | live AMap、sealed blind、H1、公网、生产、商业未运行 | `CORE_MVP_OWNER_REVIEW_PENDING`；G04 `NOT_ACTIVATED` |

Auto-advance：`DISABLED`；Goal archived：`YES`；writer activation：`NONE`；release/deployment/main merge：`NOT_REQUESTED`。返修后的用户可见结果已完成，剩余工作只有项目所有者体验验收；这不能由自动化测试替代。
