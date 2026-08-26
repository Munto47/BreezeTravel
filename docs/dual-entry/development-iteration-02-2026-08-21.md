# 双入口开发迭代 02：可执行指标、权威推荐门禁与真实数据先验

日期：2026-08-21  
依据：`BreezeTravel_双入口可验证行程产品与架构重构最终方案_2026-08-20.md`

## 本轮结论

本轮把“有测试数据”推进为“测试数据可以被公开 HTTP 链路执行、按结构化 oracle 评分，并在事实不足时关闭门禁”。当前开发回归通过，但最终发布门禁仍保持关闭。

## 测试集与确定性评分

- 数据集固定为 78 cases，北京、上海、杭州各 26；IMPORT 37、BUILDER 41。仓库内只有 60 条 development labels，18 条 frozen-blind 仅保留 commitment/seal，真实 blind truth 必须由仓库外 bundle 提供。
- split 为 pilot 6、dev 42、regression 12、frozen blind 18。
- 每条输入都有按 NFC、换行归一和稳定 JSON 编码计算的 `normalized_input_sha256`。
- source/template/generator/mutation lineage 不再用 case ID 伪造；无历史证据时明确记录 `UNAVAILABLE` 或 `N/A`。
- 六类结构化 oracle 已接入独立 scorer：Parse F1、Entity Precision/Recall、Finding Precision/Recall、Repair/Postcheck、Builder nDCG@5、Builder Recall@5。
- 当前 development 适用分母分别为 5、4、16、7、11、13；其余 case 明确为 N/A，不用空标签补分母。
- 缺预测字段是 `UNSCORED`，并使 case/run 进入 `INVALID`；它与“合法的空预测集合”严格区分。
- 聚合结果保留分子、分母、coverage 以及 applicable/scored/invalid/N_A case IDs。配置阈值时，分母为零或 coverage 小于 1 会 fail closed。
- development runner 继续拒绝读取 frozen-blind/sealed labels；独立 blind scorer 已完成，且只接受仓库外 bundle/stdin，在验证 run/dataset/output/hash 绑定后输出聚合门禁回执，不返回逐 case 分数或 case ID。当前尚未提供真实外部 blind bundle，所以不能执行 release blind 评分。

## Import HTTP 执行链

Import runner 现在通过公开 HTTP 执行：登录、创建 room/workspace、导入、解析确认、apply、Audit、Repair preview、postcheck、repair apply、revision/readback。Finding 使用不依赖运行期 UUID 的稳定签名，Repair 使用结构化谓词评分。

wrong-city 候选只从 `rejected_candidates` 回读完整 Provider receipt，不能确认；空结果和不完整候选不会被推导成可验证证据。Audit 继续保留 UNKNOWN，不用直线距离、当前时间或 LLM 文本补造路线和营业事实。

## Builder 权威门禁

- SuggestionSet 的候选只能由 Provider canonical identity、完整地点 receipt 和逐腿 route receipt 构造。
- accept 时重新计算 POI 与每条路线 receipt 的 freshness，校验端点、交通方式和 delta 算术；过期、缺腿、矛盾或 UNKNOWN 均拒绝。
- 候选先通过同一 `AuditEngine + AuditRuleRegistry` 对临时 revision 的完整评估，再冻结 task/member/evidence/rule/input token。
- accept 在 workspace mutation lock 内重新校验这些 token；HARD violated 或 UNKNOWN 不能直接接受。
- Amap 明确返回的营业字段可以成为当前 `OPENING_HOURS` 事实；社区内容、官方路线先验、UGC 和 LLM 不得升格成实时营业、预约、无障碍、路线时长或地点身份事实。
- 可见候选最多保留 6 个真实结果；不足 4 个返回 `PARTIAL` 与 typed shortage，不补造候选。
- Provider 初排之后再次执行权威 Audit gate；可直接接受的候选稳定提升到 Top-3，不可行/UNKNOWN 候选仍可作为带原因的次级备选显示。最终 rank 变化后重新生成 gate receipt，避免 accept-time token 与展示 rank 不一致。
- accept 后 route receipts 写入 revision，普通 Audit 可以从目标 revision 还原新的 `ROUTE_TIME` facts。
- 营业时间解析已补齐 `24小时营业/开放`、全天营业/开放和跨午夜窗口；营销文案或畸形字符串仍保持 UNKNOWN。

## 官方与真实互联网用户数据

- 北京官方路线库、上海官方 citywalk 与三城 Wikivoyage 固定 revision 已有 hash-bound minimal archive / extract。
- 官方先验只在 Provider 已解析 canonical 候选后匹配，只影响 `official_route_prior`、路线邻接解释和来源引用。
- 社区资料只影响 content/diversity/route prior，并带内容 SHA-256。
- Provider 自带的“官方”字符串或分数不可信，不能自证来源。
- 杭州官方路线归档不可用会显式返回 unavailable，但不会阻断真实 Provider 候选，也不会冒充官方推荐。
- 任何 prior 完整性错误都会使全部 prior fail closed；真实 Provider 候选仍可保留。
- 即使命中官方/社区先验，route UNKNOWN 仍不能进入 acceptable Top 3。
- `suggestions_shown` 和 `candidate_accepted` 事件冻结候选的 `source_prior_refs`，但不把这些引用提升为 current fact。

## 真实 Provider 快照

本地授权实网捕获已实际运行一次，无重试、无 fixture fallback：

- 北京、上海、杭州各 4 个意图查询，共 12 次 Amap around 查询；
- 每城冻结 6 个候选，共 18 个 canonical candidate；
- 冻结 18 条 Anchor 到 candidate 的 walking route receipt；
- 文件 SHA-256：`9e93086e4c764ac7c5aa628d6e857a7b03f3a8939a9971a7631e27607a792c04`；
- payload / snapshot ID：`d64231204b8319cb488754afc331800dd0c51e41b2a2f3ba15194c3c2f2bc5bd`。

Builder RunSpec 的 G0 预检会逐字节校验文件、payload integrity、evidence class/subtype/status、claim boundary、snapshot ID，以及独立 graded ranking oracle 的文件/内容/source snapshot 三重绑定；当前 10 项检查全部 PASS。该证据只证明本地授权的真实地点与步行路线响应，不证明公网 E2E、推荐适宜性、预约/无障碍事实或人工认可。

三城首轮候选另有 development-only、hash-bound graded oracle。它只读取 canonical ID、路线分钟、品类和 receipt 完整性，不读取产品 rank/score/classification，也不冒充真人偏好。2026-08-21 的真实产品 HTTP 运行在 6 个可执行 Anchor 场景上得到 nDCG@5 `0.9921`、Recall@5 `1.0`；第 7 个杭州插入边场景因该首跳快照没有精确双 Anchor query/route 合同而 `UNSCORED`，所以整体 coverage `6/7` 并正确拒绝门槛晋级。

产品级 FrozenSnapshot 适配器已通过显式配置接入公开 SuggestionSet API。启动和每次请求都会重校文件、payload 和证据 envelope；city、intent、query、固定 Anchor 与路线端点必须精确匹配。首轮 Anchor 的权威绑定来自 snapshot query contract 加原始 Amap live route origin receipt，`CLIENT_SUPPLIED` 坐标只用于构造 query geometry，不能单独通过门禁。公开 ASGI 路径已实际完成 `/health → workspace → suggestions 201 → accept 200`；第二轮因缺少 candidate 到 candidate 的路线 receipt 返回 503，整体门禁正确 REJECT。

快照真实暴露了上海约 101 分钟、杭州约 131 分钟步行的远距离热门候选；它们必须由路线 delta 和 HARD/UNKNOWN 门禁降级或淘汰，不能因为“来自真实 Provider”就被称为合适。

为验证连续四站，本轮又执行了唯一一次有界链式采集：实际 88/90 个 Amap 请求，其中 36 次 POI、52 次 walking route；无重试、无 fixture fallback。三城都取得 3 轮结构数据，但公开 ASGI + 权威 Audit gate 重放发现 `OPENING_HOURS_UNPARSEABLE`、`OUTSIDE_OPENING_HOURS` 和后续 Anchor 分叉，三城均未通过四站 G2。artifact 因此准确封为 `PARTIAL / REJECT`，没有替换 RunSpec 中的旧可用首跳快照：

- 链式 artifact payload SHA-256：`60638e84317c6b58e9bc447c576d25da0ae0bbae2a54e1c6a6b907d30ca42e00`；
- 链式 artifact file SHA-256：`837223f9c181320fa8c29db4e0d2fa356388416f705fa8d74339329954ce7770`。

失败原因是旧捕获器只按 ranker/route 选择下一 Anchor，没有复用完整产品 Audit gate。后续捕获器已改为使用 `SuggestionAuditGate + suggestion-slot-v1` 和 baseline-vs-candidate finding delta；本轮没有进行第二次 live 调用。

## 本轮独立验证

- 全部并行改动合并后的后端完整回归：`1203 passed, 25 skipped, 0 failed`（JUnit：`backend/results/dual_entry_full_20260821.xml`）。
- scorer + Import/Builder HTTP Runner + foundation：`49 passed`。
- 官方/社区 prior 与 Suggestion 回归：`89 passed`。
- 数据集 validator：`structurally_valid=true`、`release_ready=false`、`errors=[]`。
- Builder frozen-snapshot preflight：`VALID / ACCEPT_PREFLIGHT`，10 项检查全部 PASS。
- Builder 恢复合同、增量审计、workspace/audit API 定向回归：`56 passed`；其中 1 条直接通过 FastAPI ASGI 公开路由执行恢复合同，未调用 domain service 或 SQL seed。
- 前端 SuggestionSet Playwright：`6 passed`，新增真实 DOM drag 与“移动到另一天”按钮的同命令/409 回滚等价合同。
- 前端 `next build`：编译、类型检查、静态页生成全部通过。
- 相关 Ruff：全部通过。
- Docker Desktop 恢复后真实 PostgreSQL 门禁：`12 passed`。该轮首先暴露 migration 020 重复创建 019 唯一约束的 fresh-schema 错误；修复为同时处理 PostgreSQL `duplicate_object` 与唯一索引的 `duplicate_table` 后，migration 018–021、Import rollback/readback、Suggestion/Undo、并发、历史 receipt、复合 FK 和 repository restart 全部实跑通过。
- Backend + Yjs 真实进程重启已扩为九场景矩阵：Playwright `1 passed (48.3s)`；北京/上海/杭州各 3 个独立 case，Backend 与 Yjs 的 boot UUID/PID/StartedAt 均换代，stop 窗口 8000/1234 均不可达，9 个全新 Yjs client 在浏览器前精确恢复 revision/audit/member/map/place/event 引用。机器证据 SHA-256：`2d3603218db5b3b1f4840dc8911d3fae7ccb7c1780e18b50e4b0446bcfffd658`。
- 正式 Builder HTTP 运行：`backend/evidence/runs/20260821T070543532772Z-nightly_snapshot-1bed51e8`，143 次公开 HTTP 调用、7/7 case 完成，accept body 中客户端地点事实为 0；所有可执行首轮 Top-3 均无 wrong-city/HARD/UNKNOWN 泄漏，drag/button、增量/完整 Audit 和并发单赢家合同通过。最终 `REJECT`，因为四站场景第二轮缺 candidate→candidate 冻结路线、插入边场景缺精确双 Anchor 快照，且该 RunSpec 未开启服务重启授权。

默认全量中的 25 个 skipped 主要是需要显式授权的服务/外部集成门禁，不能计为通过；其中本轮选定的 PostgreSQL 项已另行用 `RUN_SERVICE_INTEGRATION=1` 实跑。

## 仍然关闭的门禁

- migration 018–021 与本轮选定的 11 条 PostgreSQL rollback/FK/并发/restart 合同已经在仓库 Docker PostgreSQL 上实跑通过；这仍不等同于生产数据库、升级前真实历史数据或公网服务验证。
- Builder 快照已完成 RunSpec G0 绑定和产品级 FrozenSnapshot Provider 首跳执行。现有快照只有固定 Anchor 到候选的一跳路线；第二轮新 Anchor 没有候选间路线证据并正确 fail closed，四站 G2 必须扩充真实链式快照，不能用直线距离或旧锚点路线替代。
- Builder HTTP slice 已从 6 条扩为 7 条，并接入真实 Amap canonical ID 的独立 graded oracle 及 nDCG@5 `0.80` / Recall@5 `0.85` 门槛。6 个 Anchor 场景已形成真实可比较分母并高于数值阈值；插入边场景缺精确快照而使 coverage 只有 `6/7`，门禁仍 fail closed。
- 公共 Undo 已与 revision、pointer、command、`stop_undone` event 和 acceptance lineage 在同一锁/事务中接通；对应 PostgreSQL migration/FK/rollback 路径已纳入本轮实跑。
- drag/button 已通过两个公开创建的同构 workspace 执行相同逻辑 `MOVE_TO_DAY` operation+payload，并比较 changed days/edges/rules、route delta、逻辑 stop 占位符归一后的 revision semantic hash 与双方各自 409 后的原始 content-hash rollback readback。两个 workspace 使用不同的全局 itinerary/stop 身份，不再为制造跨 workspace 原始 hash 相等而复用 PostgreSQL 主键；浏览器 DOM 路径也验证两种交互产生同一命令。
- 双客户端并发使用 barrier 同时向公开 edit API 提交同一 base revision，门禁要求且仅允许一个 200、一个 `ITINERARY_REVISION_CONFLICT`，失败客户端必须显式 reload 到胜出的 revision。
- incremental findings 已通过公开 full Audit 对 affected rule/day 范围做语义一致性比较；只忽略运行期 finding/evidence UUID，不忽略规则、状态、严重度、原因、输入或受影响对象。
- backend + Yjs 真重启 harness 已接入：两服务均暴露进程级随机 boot witness，命名服务显式 `stop` 后必须观测到端口不可用，再 `start` 并要求 witness/PID/StartedAt 换代。2026-08-21 实际 Playwright 复跑 `1 passed (48.3s)`，9 个独立场景全部通过公开 HTTP、认证 Yjs WS 与真实浏览器精确回读；公开批量 cleanup 对 PostgreSQL room 和 Yjs doc 均证明 9/9 清除。Continuous Builder 绑定 allowlisted v3 门禁；未显式设置 `BREEZE_EVAL_ALLOW_SERVICE_RESTART=1` 时仍 fail closed。本矩阵只收口恢复合同，不替代导入/Repair、四站连续候选、拖拽/按钮/Undo 与并发 200/409 的独立业务门禁。
- 旧 25 case /111 个无 ID Builder subject 已完成真实性分类：0 个可与现有 Amap snapshot 精确绑定，90 个开发 subject 落为明确的 controlled-fixture execution receipt，21 个 frozen-blind subject 保持 `UNAVAILABLE`。全量静态枚举为 259 条：224 fixture / 35 unavailable / 0 real-provider；新增暴露旧检查漏掉的 blind seed 6 条与 blind Import controlled fact 8 条。Fixture receipt 不含伪造 Provider 请求/响应或观察时间，也不替代 Import rejected candidate、Builder route/current-fact 的运行时 `provider_receipts.jsonl`；Import 与最终发布 Provider snapshot 覆盖仍不完整。
- frozen blind 数量仍为 Import 12/90、Builder 6/45、fault/recovery 5/24。
- template/generator lineage 78/78 unavailable；source lineage 72/78 unavailable。
- human calibration 为 0/30；blind truth 已从仓库移除，但外部密封 bundle/CI secret storage 尚未 provision。
- weekly live 30-case 门禁尚未执行；现有 local-authorized snapshot 不能替代公网、滚动多实例或当前时点 live 证据。

因此当前可以声明的是：开发测试集、确定性评分、权威 Audit 门禁、真实来源边界、本地授权 Provider 快照、选定 PostgreSQL 合同和九场景本地进程恢复已经形成可复验链路；不能声明四站 G2、release blind、weekly live、公网服务或人工质量门禁已经通过。
