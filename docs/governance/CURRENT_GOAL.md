# APPROVED GOAL：V0.2 地图与整程住宿

Goal ID: TC-VNEXT-G02-MAP-STAY
Status: APPROVED
Goal type: PRODUCT_VERTICAL_SLICE

<!-- PRODUCT_DELIVERY_CURRENT_GOAL_STATE
{
  "schema_version": "product-delivery-current-goal-state-v1",
  "program_id": "TC-VNEXT-2026",
  "goal_id": "TC-VNEXT-G02-MAP-STAY",
  "goal_status": "APPROVED",
  "gate_profile": "PRODUCT_DELIVERY_GATE",
  "required_gate": "Map & Stay Gate + PRODUCT_DELIVERY_PASS",
  "completion_status": "PENDING",
  "gate_result": "PRODUCT_DELIVERY_NOT_RUN",
  "goal_archived": false,
  "next_goal_id": "TC-VNEXT-G03-TOP3-AUDIT",
  "next_activated": false,
  "h1_status": "NOT_RUN",
  "production_status": "NOT_RUN",
  "commercial_status": "NOT_RUN"
}
-->

## Metadata

- Goal ID：`TC-VNEXT-G02-MAP-STAY`
- Program ID：`TC-VNEXT-2026`
- Product version：`V0.2`
- Mainline phase：`CORE_MVP`
- Gate profile：`PRODUCT_DELIVERY_GATE`
- Status：`APPROVED`
- Goal type：`PRODUCT_VERTICAL_SLICE`
- Branch：`codex/g02-map-stay`
- Canonical baseline/upstream：`origin/develop@59da74b51455dc224efbf95c0caea3240b70c80e`，PR #5与远端readback `PASS`
- Activation：G01已通过`PRODUCT_DELIVERY_PASS`、经PR #5进入`origin/develop`并归档
- Required gate：`Map & Stay Gate + PRODUCT_DELIVERY_PASS`
- Next Goal：`TC-VNEXT-G03-TOP3-AUDIT`

## Dependencies

- 唯一激活依赖G01归档且Text Card Gate通过；该依赖已由PR #5、交付回执与远端readback满足，G02现为`APPROVED`。
- 首个preflight填写branch/baseline、current OpenAPI、G01的029/worker/snapshot、`OWNER_ATTESTED_EXISTING_AUTHORIZATION`和未关闭风险；缺失lane标记`NOT_READY`，不伪装通过并继续其他安全独立切片。

## User Outcome

用户打开地图模式时通常已经有真实路线；同一天地点同色，步行/公交可切换。编辑卡片后旧地图明确显示“行程已修改，地图尚未更新”，只有点击“重新渲染地图”才计算当前行程。

原文没有酒店时，用户得到综合全程首末站往返成本的最多3家连锁酒店候选；选择后整程住同一家。

## Scope

- 复用G01的`MapRenderJob/MapRenderSnapshot`、lease、逻辑幂等、迟到保护和walking/transit事实；
- 地图剧场、日颜色、marker/line联动；
- `NEEDS_UPDATE`用户投影和手动rerender；
- `StayAreaPlanner / HotelBrandRegistry / StayRecommendationSnapshot`；
- 2/4/8km与同城扩展；
- 最多12家路线矩阵和Top-3；
- 选择共享StayAnchor并创建新revision。

## Pre-approved actions

- `030_stay_recommendation_snapshots.sql`；
- v3 map-renders、stay-suggestions、stay-selection；
- 当前已有无增量费用高德POI、walking和transit开发调用；
- Redis短期geometry缓存；
- 不引入消息队列。

## Parallel work packages

G01归档并原子激活G02后，主对话先冻结公共/内部接口、`030`编号和exact product baseline，再按`WORK_PACKAGE_PROMPT_TEMPLATE.md`生成并登记三份完整提示词。每包由一个用户可见的独立功能对话承担；子Agent只可短期只读复核或诊断。

| Package | Branch | Worktree | 初始状态 | Owned paths（激活时精确化） | Acceptance |
|---|---|---|---|---|---|
| `WP-G02-MAP-THEATER-UI` | `codex/wp-g02-map-theater-ui` | `D:/munto/code/claudeProject/agentTravel-wp-g02-map-theater-ui` | `IN_PROGRESS` | 地图剧场前端及定向浏览器测试 | 同日同色、walking/transit切换、NEEDS_UPDATE |
| `WP-G02-STAY-DOMAIN` | `codex/wp-g02-stay-domain` | `D:/munto/code/claudeProject/agentTravel-wp-g02-stay-domain` | `IN_PROGRESS` | 住宿区域/品牌/评分纯领域模块及测试 | 2/4/8km、最多12→3、整程同店可重放 |
| `WP-G02-MAP-STAY-BACKEND` | `codex/wp-g02-map-stay-backend` | `D:/munto/code/claudeProject/agentTravel-wp-g02-map-stay-backend` | `WAITING_FOR_WRITER_SLOT` | map/stay service、API和持久化集成 | 手动更新只算current revision，编辑调用0 |

集成者始终占一个writer名额，因此先启动地图UI和住宿领域两个功能对话。A或B经主对话验收、登记`ready_commit`并冻结为官方`READY_TO_MERGE`后，才把后端包切为`IN_PROGRESS`并启动第三个功能对话。功能对话只能请求冻结，不得自行改registry、创建migration编号、改共享OpenAPI或合并。

三个包全部验收冻结后停止贡献分支写入。主对话严格按`WP-G02-STAY-DOMAIN → WP-G02-MAP-STAY-BACKEND → WP-G02-MAP-THEATER-UI → E2E`串行合并；`ready_commit/merged_commit`祖先顺序不符时Gate失败。集成故障优先交回原功能对话修复，主对话只处理最小冲突和登记路径内胶水；每包最多两轮修复复审。

## Decisions locked

- 初次卡片READY自动创建一次地图任务。
- 后续卡片编辑自动路线Provider调用为0。
- 用户手动rerender。
- 差值≤10分钟优先walking。
- 驾车不是默认。
- 迟到任务只属于旧`PlanRevisionRef`，内部freshness不进入普通API。
- 酒店按Day1…DayN-1过夜日、酒店→首站和末站→酒店方向计算；整程同店，不自动选择。
- `StayScoringPolicyVersion`冻结坐标、12家停止阈值、失败惩罚和tie-break。
- 不显示价格、房态、星级和服务质量。

## Non-goals

- Top-3 Audit、天气、预约和完整Repair；
- 酒店档次、预算和库存；
- 实时跟随卡片重绘；
- 新地图Provider、地图长期geometry存储；
- 公网和生产商业展示。

## Acceptance

完全继承Map & Stay Gate：

- 编辑后自动路线调用0；
- stale/revision绑定100%正确；
- 幂等、迟到、并发、重启和config漂移通过；
- walking/transit独立失败语义；
- 地图失败不影响卡片；
- 住宿扩圈、brand、城市、类别和评分可重放；
- 错酒店严重项0；
- 同店物化和stale完整回读；
- 只有无合格酒店的负例oracle允许空候选并中性返回；
- 30组已知存在至少3家合格连锁酒店的正例中，snapshot Top-3非空率100%、首位合格100%，受控live dev非空率≥90%；
- 公共地图状态只使用用户枚举；stale卡片详情不把旧路线表达为当前；
- 住宿用户卡片解释区域、通勤摘要、最差单程、换乘和证据缺口。
- 已有snapshot时地图首屏几何P95≤1.5秒；未就绪时地图壳和用户状态≤500ms。

## Verification

- map worker/service状态机；
- route mode policy；
- Postgres 029/030集成；
- Redis丢失和geometry过期；
- AMap fixture/snapshot/live dev matrix；
- browser pre-ready/ready/stale/rerender/partial；
- stay scoring/property tests；
- public result禁止内部字段；
- 高德最小留存与owner attestation readback；
- 当前地图/住宿定向测试、PostgreSQL、frontend build与浏览器E2E；候选复审留到G07；
- H1/生产：`NOT_RUN`。

## Authority

- `AGENTS.md`、Charter、Spec、v3 API、Architecture；
- Program、Roadmap、Release Gates、Product Delivery Gate、Provider Admission、Risk Register；
- ADR-007、ADR-008、ADR-010、ADR-012、ADR-013、ADR-014。

## Baseline

- Implementation branch：`codex/g02-map-stay`；activation baseline/upstream：`origin/develop@59da74b51455dc224efbf95c0caea3240b70c80e`；
- G01 delivery subject：`8aebb6af9639ed5814e157a6f0de0b136e64a70e`；G01 integration subject：`59da74b51455dc224efbf95c0caea3240b70c80e`；
- Goal transition subject在过渡PR合并后由远端readback记录；首个G02产品切片开始前再次fetch并确认无基线漂移；
- 只接受G01同绑定地图后端证据，不以旧room driving地图或坐标虚线为当前能力；
- H1、公网、生产、商业：`NOT_RUN`。

## Invariants

- snapshot不可变，job可变，freshness由current `PlanRevisionRef`计算；
- card edit自动路线调用为0，不同请求key命中同逻辑任务也复用；
- 旧路线可以带提示查看，不能作为当前事实；
- 酒店候选必须同城、酒店类别、注册连锁；无候选中性；
- 不展示价格、房态、星级、内部评分或质量承诺；geometry遵守许可和TTL。

## Budget

- 路线每条边walking/transit各至多一次；手动重复点击复用逻辑任务；最多12家酒店进入方向性路线矩阵；
- 2/4/8km/同城按12家阈值停止；Provider retry/deadline按冻结config并记账；
- 不新增账号、付费、地图Provider、消息队列或长期geometry存储；每切片checkpoint。

## HITL

新高德账号/费用/扩大数据权限、HotelBrandRegistry新增受限来源、未预批准migration/依赖、H1/公网/生产/`main`时请求批准；现有授权和可修复地图、评分故障由Codex继续诊断，候选Agent Gate问题冻结到G07。

## Checkpoint ledger

| 时间 | 用户结果 | Commit | Verification | Evidence level | Product progress | Governance ratio | Remaining | Risk/failure | Next autonomous action |
|---|---|---|---|---|---|---|---|---|---|
| 2026-08-29 | G01已并入`origin/develop`，G02地图与整程住宿主线已激活；尚未修改G02产品代码 | transition pending remote subject | G01 `core-mainline`、PR #5合并、`origin/develop@59da74b`远端readback `PASS` | `PRODUCT_DELIVERY_PASS / REMOTE_INTEGRATION_PASS` | `Product progress=NONE / GOAL_TRANSITION` | `Governance ratio=100% / atomic transition only` | 地图剧场、手动更新、整程住宿产品实现与G02定向验证 | G07候选项目`NOT_RUN`且不得阻断 | 从当前基线开始最小G02产品切片 |

## Auto-advance

- Required gate：`Map & Stay Gate`；Next template：`TC-VNEXT-G03-TOP3-AUDIT.md`；
- subject push/readback、耐久`PRODUCT_DELIVERY_PASS`、clean tree、无Stop后，最终归档，并原子更新Goal binding与work-package registry激活G03；不登记外部ledger、不创建authority generation；
- FUX-02、H1、公网、生产、商业和`main`不自动启动。

## Completion record

- Status：`APPROVED`；Subject commits：G01 delivery `8aebb6a`、integration `59da74b`，G02 transition待远端readback；Remote branch：`origin/codex/g02-map-stay`；
- Verification / Evidence / Gate result / `structurally_valid`：激活后填写；
- H1 / production / commercial：激活时固定为`NOT_RUN / NOT_RUN / NOT_RUN`；
- User-visible result / Remaining risks / Goal archived / Next activated：激活后填写；
- Promotion decision：`NOT_REQUESTED`。

## Stop conditions

- 需要新账号/费用或扩大地图来源；
- 无法保证编辑零自动调用；
- 迟到任务可能覆盖current revision；
- 酒店品牌来源无法建立；
- 需要价格/房态/星级才能满足目标。
