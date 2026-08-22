# 双入口目标能力状态表

更新时间：2026-08-20。状态只使用 `planned / implemented / unit_verified / integration_verified / publicly_verified / user_validated`。本表针对新产品闭环；既有相似能力不自动等于目标能力完成。

| 阶段 | 目标能力 | 当前状态 | 当前依据或缺口 |
|---|---|---|---|
| P0 | 唯一开发基线与转型 ADR | implemented | Final 1.0 与 ADR-001；尚不代表代码完成 |
| P0 | 10 份真实 AI 行程与人工排雷记录 | planned | 采集 manifest 为空；不得用合成/Agent 标签代填 |
| P1 | TripWorkspace 聚合根 | integration_verified | 新 workspace/revision repository 与 API 已实现；随机临时 PostgreSQL 的创建、推进和重启式回读已通过；导入页把 workspace/import 引用写入 URL，刷新后从服务端恢复草稿、revision、report、Evidence 和 Repair 列表 |
| P1 | append-only ItineraryRevision | integration_verified | 命令、导入和 Repair 都创建新 revision；PostgreSQL 集成验证旧 revision 保留且 current 引用原子推进 |
| P1 | canonical content hash / report input hash | unit_verified | 服务端 canonical hash 覆盖语义行程、任务、消歧、Evidence 与规则版本；键序性质测试已通过 |
| P1 | If-Match、幂等与 revision conflict | integration_verified | 编辑、Undo、Confirm、Import apply 和 Repair apply 已覆盖冲突与重放；Import/Audit create、Audit refresh、Repair propose、Tips generation 也使用持久化 creation command，同 key 异 payload稳定拒绝。真实 PostgreSQL 已覆盖 command 租约接管、回滚、并发一胜一及响应回放 |
| P1 | 移动端断线恢复 API 合同 | integration_verified | `/resume` 在单个 PostgreSQL repeatable-read 只读事务中恢复 current revision/import/report/evidence/proposed 或 applied Repair/Tips，返回聚合 ETag/304 和独立写 ETag；跨 room 与不存在同形 404，lineage 损坏稳定 409。Import mutation 强制 `If-Match`，APPLIED/FAILED 后禁止修改草稿；现有导入页已切换该合同，未做移动端界面 |
| P1 | 成员约束版本底座 | integration_verified | 已实现 Final 6.5 模型、workspace 级 append-only constraint revision、历史集合重建、并发一胜一冲突以及 Audit input hash 绑定；MEMORY/INFERRED 不得成为 HARD。P7 已在该底座上补齐成员 API 和受限分享合同。 |
| P2 | 独立 Audit Engine 与唯一 RuleRegistry | integration_verified | Critic 全规则固定 parity 用例已覆盖营业、用餐、餐饮数量、相邻品类、时间链、天气和酒店；独有节奏规则已迁入 registry，`critic_v2` 已从 Planner 主图移除并只保留为历史 parity oracle；持久化 Planner 响应不再暴露 legacy report 为权威 finding |
| P2 | EvidenceSnapshot/EvidenceFact | integration_verified | 四态 freshness、字段事实、Provider 部分失败和不可变快照已通过单元测试，并在 PostgreSQL 完整链路回读 |
| P2 | 不经 Planner 的审计 API | integration_verified | 已实现独立审计、报告、Evidence、refresh/events；SQL 仓储链路不经 Planner 完成审计与回读 |
| P3 | 原文保存和结构化解析草稿 | integration_verified | 纯文本、source span、source sentence、固定承诺、限制和 Prompt Injection 降级已测试，并通过 PostgreSQL import 生命周期；解析失败草稿可修改后创建新 import，旧记录不覆盖；真实 blind 仍为 0 |
| P3 | POI 候选、置信度与人工消歧 | integration_verified | 阈值、歧义、NOT_FOUND/Provider failure、受控重检索、批量原子确认与 apply revision 1 已覆盖；单个重搜不改变其他 resolution version；真实匹配 precision 尚待 M1 |
| P4 | 风险分组与 Evidence 回读 | integration_verified | 新导入页已展示必须修改/建议调整/待确认、判定输入、Evidence 时间与 freshness；本地浏览器已在 PostgreSQL 链路完成导入、重检索、审计、Repair A/B、apply、刷新和服务端回读，不等于公网 E2E |
| P4 | Repair A/B、不可变预览和完整 postcheck | integration_verified | 时间冲突 A/B 与重复地点删除 preview、锁定保护、未涉及日期等价、新 HIGH/UNKNOWN 非回归、postcheck、apply/reject/幂等已测试；路线目标只在两侧 FRESH 且完整的 edge 事实存在时计算真实分钟差，否则为 `null`，并参与稳定词典序排序；apply/reject 共享行锁并经 PostgreSQL 并发竞速验证只能产生一个终态 |
| P4 | Tips 与最终 revision/report 一致 | integration_verified | 新增 011 和不可变 `FinalTipsArtifact`，只接受 current revision/current full report 且无未修复 BLOCKER/HIGH；UNKNOWN 会进入提示上下文而不会被写成已确认。Planner 与 Repair UI 都在 Audit/postcheck 后生成，artifact 可按 report 跨刷新回读；服务策略、幂等和拒绝路径已单测，PostgreSQL FK/JSONB/新仓储实例回读已通过；真实 LLM 文案质量未作 Judge 声明 |
| M1-dev | 代理校准协议与确定性指标聚合器 | integration_verified | 三个独立 GPT-5.6-sol `synthetic_proxy` 角色产物已通过 hash、角色、模型和字段门禁；产物不能写入真人字段。 |
| M1-dev | 三城合成 AI 行程与模拟排雷 | integration_verified | 独立 synthetic lane 共 150 条，真实 Parser/Resolver/Evidence/Audit/Repair 链路离线执行；代理门禁实测通过（precision 1.00、recall 1.00、关键一致率 0.92、Evidence readback 1.00、3 分钟内 1.00）。仅为本地开发证据。 |
| P8 最终真人验收 | 真实行程与真人校准 | planned | 当前聚合器实测 `BLOCKED_HUMAN_DATA`：0/30 份真人标注行程、0/15～20 名真实组织者；Agent/Judge 不得代填。这不再阻断 P5～P7 开发，但阻断公网发布表述 |
| P5 | 三城路线骨架与候选插入成本 | integration_verified | `014_route_templates_and_suggestions.sql`、三城 15 条 `MODEL_GENERATED + DRAFT` 骨架、版本/来源/失效、幂等 apply 均已覆盖；受控 PostgreSQL 验证种子、应用和新仓储回读。首页→模板选择→工作台的浏览器回归通过。模板应用会冻结显式合成坐标锚点，候选插入/酒店首末站评分只在投影完整时执行；否则明确 unavailable。没有任何模板被伪写为 `REVIEWED`。 |
| P6 | 时间轴拖拽、移动端等价操作和增量审计 | integration_verified | `/workspace/[workspaceId]` 支持拖拽与移动端等价命令、地图/时间线双向选择、乐观回滚和审计后确认；确认在事务内校验当前完整报告。编辑后可只刷新 current revision 新增/替换的路线边：仅接受模板/revision 的显式坐标投影，追加新的 EvidenceSnapshot/AuditReport，Provider 禁用、坐标缺失或调用失败均写为 `UNAVAILABLE`，历史 revision/snapshot 不改写。增量重用相同任务/证据/成员上下文且 `llm_calls=0`，对照完整审计；50 次编辑无丢失，受控缓存路径 P95=1.2ms。真实 Provider 路线 P95 仍未取得。 |
| P7 | 成员约束、分享和冲突处理 | integration_verified | 成员仅可写自己的 confirmed HARD 约束；约束已进入权威审计并写出受影响成员。受 scope/recipient/失效/撤销保护的 hashed bearer 链接、ACK/约束回应、成员面板、接收方脱敏只读页、Yjs 引用同步和用户触发的冲突回读已实现；受控 PostgreSQL 验证 token 只存 hash、回复/撤销与重启读回。2026-08-21 真实双用户 Backend/Yjs 进程换代 E2E 已扩为北京/上海/杭州各 3 个、共 9 个隔离场景并实际通过；该恢复矩阵不替代四站连续候选、weekly live 或真人门禁。 |
| P8 | 临行复检、本地证据差异 | integration_verified | 幂等复检会追加 EvidenceSnapshot/AuditReport，工作台显示事实/finding/Provider failure/receipt 差异与 EARLY/24–48h/LATE 窗口依据；受控 PostgreSQL 验证追加、重放及新仓储回读。默认回放存量事实并明确不调用 Provider；配置齐全时可选高德适配器记录 hash、观测时间和局部失败。公网双入口 E2E、真人验收和发布证据包仍 planned。 |

## 冻结范围

- 城市：北京、上海、杭州；
- 人数：2～5 人；
- 天数：2～5 天；
- 输入：纯文本、路线骨架、手动选择；
- M1 前不扩城、不增加 Agent、不引入 MQ/Kubernetes/GraphRAG、不重新微调 LoRA。
