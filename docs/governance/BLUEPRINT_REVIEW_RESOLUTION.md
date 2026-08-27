# Blueprint 1.0 独立审查与整改记录

> Goal：`TC-BP-G00-BLUEPRINT`
>
> 日期：2026-08-27
>
> 状态：`HIGH_PRIORITY_CLOSED`

本记录只证明独立审查的P0/P1已在蓝图文档中关闭，不证明产品能力已实现。产品、架构、反方治理和商业四个视角均由只读子代理独立检查；整改由主代理完成，并经过多轮聚焦复审。

## P0 处理

| 视角 | 问题 | 处理 | 位置 | 状态 |
|---|---|---|---|---|
| 产品/架构/反方 | G01承诺地图预计算，但029/worker/route权限在G02 | 把029、可恢复地图job、walking/transit、逻辑去重和迟到保护移入G01；G02只交付地图剧场、手动更新和住宿 | Program、Spec、API、Architecture、ADR-008、G01/G02、Gates | CLOSED |
| 架构 | understanding与itinerary revision身份、materialize lineage不清 | 冻结`PlanRevisionRef`、不透明ETag、current pointer和原子`MaterializationLineage`；前后materialize各只有一种写入权威 | Architecture、Spec、API、ADR-007、G02/G03 | CLOSED |
| 架构 | 无日期无法进入现有TripBrief/Itinerary | 031改为必需`day_index_trip_bridge`，支持`ABSOLUTE/DAY_INDEX_ONLY`和nullable calendar，禁止虚构确认 | Program、Spec、API、Architecture、G03、Gates | CLOSED |
| 反方 | README/CLAUDE/docs索引/旧ADR/旧证据仍自称当前权威 | 更新入口与权威顺序；ADR-001～006逐份标明取代与保留范围；历史合同、证据、Runbook和RAG入口加`NOT_VNEXT_AUTHORITY`，不改历史数值 | README、CLAUDE、docs/README、ADR-001～006、历史入口 | CLOSED |
| 反方 | 旧Current Goal归档被改写 | 恢复为`origin/develop`原内容；新版免责声明放在独立索引 | completed/TC-INTAKE-CONFIRM-E2E-HOTFIX.md | CLOSED，规范化内容相等 |
| 反方 | G01～G07不是可自动激活的完整合同 | 补Dependencies、Authority、Baseline、Invariants、Budget、HITL、Checkpoint、Auto-advance和Completion；授权标题统一 | goals/planned/G01～G07、Goal模板、AGENTS | CLOSED |
| 反方 | 先归档再填Completion会留下PENDING并形成commit自引用 | 改为subject checkpoint A push/readback，再以原子transition B生成最终归档并激活下一Goal | AGENTS、Program、Goal模板、ADR-012 | CLOSED |
| 反方 | 多模型在sealed blind上选默认 | 只用dev/validation选择唯一候选；冻结binding/threshold后blind一次；独立custodian保管标签 | ADR-009、Provider Admission、Program、G01、Gates | CLOSED |
| 反方 | 旧manifest测试不能证明新版Blueprint | 明确本次只作历史兼容检查；新版manifest生成器适配归入G07；增加独立只读Blueprint检查 | Program、G07、Current Goal、本记录 | CLOSED |

## P1 处理

| 视角 | 问题 | 处理 | 状态 |
|---|---|---|---|
| 产品/架构 | 公共API泄漏地图内部状态 | 拆分MapRenderJob、不可变Snapshot、freshness；公共只用`PREPARING/AVAILABLE/NEEDS_UPDATE/LIMITED/UNAVAILABLE` | 已改 |
| 架构 | 不同请求key可重复路线调用、202/SSE任务可能丢失 | 增加地图逻辑唯一键；028包含持久understanding job/lease/event/recovery | 已改 |
| 产品/架构/反方 | 匿名所有权、原文TTL、删除未定义 | FULL登录、DEMO HttpOnly session/24h、一次性claim、登录source最长30天、行程/账号删除和回执均在G01 | 已改 |
| 产品/架构 | 住宿算法不可确定性重放且用户理由不足 | 冻结过夜日、方向边、坐标、12家停止阈值、失败惩罚/tie-break和用户理由字段 | 已改 |
| 产品/架构 | Top-3与全部硬冲突不隐藏矛盾 | 内部全量；公共前三项+剩余数量；同因可聚合，解决后补位，不得显示通过 | 已改 |
| 架构/反方 | Qwen snapshot、账号、区域、价格和隐私不具体 | exact binding和Goal激活后的首个preflight readback；Max只作开发benchmark候选，Validation后才冻结候选 | 已改 |
| 反方 | 高德保存权被过早断言 | POI/route持久化改为`PROPOSED/BLOCKED_PERSISTENCE`，许可readback是G01依赖 | 已改 |
| 反方 | 输入放大、并发和调用预算缺失 | G01冻结50k code point、14天、80活动、并发2、模型1+1及POI/路线预算，超限`LIMITED` | 已改 |
| 产品 | 形成性用户学习太晚、北极星无行为指标 | 预定义FUX-01/02/03人工批准检查点和最小无原文行为指标；不替代H1/Gate | 已改 |
| 商业 | V1.1没有付费证据合同 | 区分组织者/创作者、付费时刻、真实付款/退款/复购/有效分享和停止条件 | 已改 |
| 反方 | Screenshot/Knowledge/性能门槛未冻结 | 冻结reading order、paired parity、三图P95环境和Knowledge消融阈值 | 已改 |

## 复审追加发现与关闭

- 地图Job/Snapshot/freshness在最高权威AGENTS同步，公共状态不泄漏；G01地图正例冻结30份行程/120条边，snapshot可用覆盖100%、受控live≥95%。
- 住宿冻结30组已知正例，snapshot Top-3非空100%、首位合格100%、受控live非空≥90%；只有负例oracle允许空候选。
- FULL/DEMO所有权、一次性claim、不透明ETag、非秘密`public_resource_id`、HttpOnly capability和访问日志脱敏闭合。
- G01提供source/整程/账号旅行数据三类删除API、普通用户入口、二次确认/重新验证、异步状态和browser fresh readback。
- 下一Goal在上一Gate后始终先置`APPROVED`；账号、许可和consent只在激活后的首个preflight标记lane状态，不会留下0个active Goal。
- auto-selected precision按全部自动选择实例计算；validation与blind分母各≥50，数据各至少65个gold mentions但仍直接检查分母。
- 最终独立复审：产品/商业`P0=0, P1=0`；架构/API`P0=0, P1=0`；反方治理`P0=0, P1=0`。三方均只读且允许进入subject checkpoint。

## 明确保留的边界

- G00不修改产品代码、测试代码、migration或依赖；manifest测试只能标记历史兼容。
- G01激活不等于Qwen或高德已准入；激活后的首个preflight必须现场readback依赖，缺失lane标记`NOT_READY`。
- FUX检查、H1、Provider新账号/费用、公网、生产、商业、release、`main`仍需人工批准。
- 任何复审新P0/P1未关闭时，Blueprint Gate保持`REJECT`。

## Subject 前验证结论

- 三路独立复审：`PASS`，P0/P1为0；
- planned Goal完整字段和单active Goal：`PASS`；
- 历史Current归档规范化内容一致：`PASS`；
- 相对链接、旧目标漂移、公开状态枚举和migration归属：`PASS`；
- 文档only diff与现有历史兼容测试：`PASS`；
- subject commit/push/readback与治理transition：执行中，结果写入Current Goal归档和Blueprint Validation。
