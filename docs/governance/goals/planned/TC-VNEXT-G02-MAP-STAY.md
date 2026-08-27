# PREDEFINED GOAL：V0.2 地图与整程住宿

## Metadata

- Goal ID：`TC-VNEXT-G02-MAP-STAY`
- Program ID：`TC-VNEXT-2026`
- Product version：`V0.2`
- Status：`DRAFT`
- Activation：G01 Text Card Gate通过并归档后
- Required gate：`Map & Stay Gate`
- Next Goal：`TC-VNEXT-G03-TOP3-AUDIT`

## Dependencies

- 唯一激活依赖是G01归档且Text Card Gate通过；随后G02置为`APPROVED`。
- 首个preflight填写branch/baseline、current OpenAPI、G01的029/worker/snapshot/许可和未关闭风险；缺失lane标记`NOT_READY`，不伪装通过并继续其他安全独立切片。

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
- 高德条款/数据留存readback；
- H1/生产：`NOT_RUN`。

## Authority

- `AGENTS.md`、Charter、Spec、v3 API、Architecture；
- Program、Roadmap、Release Gates、Provider Admission、Risk Register；
- ADR-007、ADR-008、ADR-010、ADR-012。

## Baseline

- branch/commit/upstream、G01 subject与transition：激活时填写；
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

高德许可/费用/账号变化、HotelBrandRegistry来源无法授权、未预批准migration/依赖、H1/公网/生产/`main`时请求批准；可修复地图或评分故障由代理继续诊断。

## Checkpoint ledger

| 时间 | 用户结果 | Commit | Verification | Evidence level | Remaining | Risk/failure | Next autonomous action |
|---|---|---|---|---|---|---|---|
| 激活时填写 |  |  |  |  |  |  |  |

## Auto-advance

- Required gate：`Map & Stay Gate`；Next template：`TC-VNEXT-G03-TOP3-AUDIT.md`；
- subject push/readback、Gate PASS、clean tree、无Stop后，最终归档并原子激活G03；
- FUX-02、H1、公网、生产、商业和`main`不自动启动。

## Completion record

- Status / Subject commits / Remote branch：激活后填写；
- Verification / Evidence / Gate result / `structurally_valid`：激活后填写；
- User-visible result / Remaining risks / Goal archived / Next activated：激活后填写；
- Promotion decision：`NOT_REQUESTED`。

## Stop conditions

- 高德许可不允许开发所需短期使用；
- 需要新账号/费用或扩大地图来源；
- 无法保证编辑零自动调用；
- 迟到任务可能覆盖current revision；
- 酒店品牌来源无法建立；
- 需要价格/房态/星级才能满足目标。
