# BreezeTravel 仓库开发约束

## 1. 唯一产品目标

BreezeTravel 只建设「行程查」：

> 用户粘贴攻略或上传截图，不填写前置表单，也不理解项目术语；系统自动生成高准确率的逐日行程卡片，提前准备路线地图，再用少量、可靠、可直接采纳的建议帮助用户把行程变得真正可执行。

北京、上海、杭州提供深度地点、路线和核验能力。其他国内城市可进行基础语义整理和卡片生成，但不得暗示已经通过同等级 Provider、知识或版本门禁。

当前权威主链：

```text
Text / Screenshot
→ SourceDocument
→ TripUnderstandingRevision
→ DayDraft / ActivityMention / SourceClaim
→ ExecutablePlaceMention
→ PlaceResolution
→ UserFacingTripResult
→ MapRenderSnapshot
→ ItineraryRevision
→ EvidenceSnapshot
→ AuditEngine
→ Top-3 Finding
→ RepairOption / EditCommand
→ 新 Revision
→ 手动地图更新 + 完整 postcheck
```

旧房间入口、Builder、Planner、ReAct/Critic、LoRA、旧 RAG 和 Yjs 是兼容或冻结资产，不得重新成为产品入口。开发子代理可用于数据生成、独立复核、反方审查和故障诊断，但不得变成产品运行时多 Agent。

## 2. 用户体验硬规则

普通用户只能看到逐日卡片、地点详情、路线、住宿和餐饮建议，以及“必须调整 / 可以更好 / 需要确认”。

普通用户界面、公共结果 API、DOM 和无权限页面不得出现：

- 原文映射、source span、offset 或置信度数字；
- UID、hash、revision、receipt、RunSpec；
- Brief、Evidence、Audit、Repair、Postcheck；
- 模型、Provider、内部阶段、堆栈或数据库错误。

这些信息必须留在内部权威记录中，用于回归、追责和证据回读；诊断入口独立授权并在生产默认关闭。

HTTP ETag只能是不可逆、不透明的CAS validator，不能编码可恢复revision/hash；随机`public_resource_id`只用于路由且不承担授权。匿名秘密必须在HttpOnly cookie中，实际路径ID和capability不得进入访问日志、分析事件或用户文案。

卡片点击只打开用户友好的地点详情与操作，不显示或高亮原文。错城、错类别、把描述句或 URL 当作地点属于严重错误；宁可保留“地点待确认”，也不得自动匹配错误地点。

首页不得要求用户先选城市、日期、人数或创建房间。缺失值使用可编辑的软假设：概率最高城市、无日历日期时 Day 1～Day 3、人数默认 2。软假设不得伪装成原文事实或 HARD 证据。

未找到地点、未选择酒店、Provider 暂不可用和数据不足不得使用红色。红色只用于有可靠证据的硬冲突。

## 3. 领域和架构不可变量

- PostgreSQL 是 revision、run、lease、幂等、receipt、lineage 和权威业务状态的唯一事实源。
- Redis 只保存缓存、限流、短期路线几何和可重建协调状态。
- LangGraph 只编排固定阶段、HITL、SSE 和恢复；副作用仍需稳定幂等键、事务和回执。
- `TripUnderstandingRevision` 保存内部语义与证据；`UserFacingTripResult` 是严格脱敏的用户投影。
- `ActivityMention` 必须区分 `PLANNED / OPTIONAL / REFERENCE / EXCLUDED / PASS_THROUGH`。只有有原子地点的 `PLANNED` 提及可以自动搜索 POI。
- LLM 只能提出语义草稿、查询改写和建议表达；不能生成已验证 POI、路线时间、EvidenceFact、Finding 或“已解决”状态。
- `ItineraryRevision` 不可变；任何有语义的编辑或建议采纳创建新 revision。
- `AuditEngine` 是 Finding 唯一权威。`UNKNOWN`、`UNAVAILABLE` 和局部失败不得计为 PASS。
- 确定性事实与建议性判断分开。热门、时段、典型时长、餐饮和酒店偏好必须以建议性语气展示依据。
- 原始截图不得进入数据库、日志或 Git；所有终态都删除，只保存 hash、OCR box、版本和清理回执。

采用 Next.js/React + FastAPI/Pydantic + PostgreSQL 的模块化单体。不得为技术关键词新增微服务、消息队列、Kafka、Temporal、Kubernetes、GraphRAG 或运行时多 Agent。

## 4. 地图与住宿不可变量

卡片首次生成并完成地点映射后，后台必须为同一`PlanRevisionRef`创建并实际执行一次walking/transit地图job，不阻塞卡片；G01交付该后端能力，G02才交付地图剧场和手动更新体验。

内部对象固定分层：

```text
MapRenderJob: QUEUED → BUILDING → READY / PARTIAL / UNAVAILABLE
MapRenderSnapshot: immutable terminal result
MapFreshness: CURRENT | STALE（按snapshot与current PlanRevisionRef比较）
```

- 普通用户API只返回`PREPARING / AVAILABLE / NEEDS_UPDATE / LIMITED / UNAVAILABLE`，不返回内部job/freshness枚举。
- 卡片编辑只产生新revision并把旧地图投影为`NEEDS_UPDATE`；不得自动调用路线Provider或实时重绘。
- 只有用户点击“重新渲染地图”才为current `PlanRevisionRef`重新计算。
- 迟到任务只能写回其绑定的旧`PlanRevisionRef`。
- 请求幂等键与地图逻辑唯一键必须同时防止重复Provider调用。
- 相邻地点同时比较步行和公交；差值不超过 10 分钟时优先步行，驾车不作默认。

未识别到酒店时生成非阻断的“住宿待选择”。系统综合各过夜日第一站和最后一站，先划定区域，再按 2/4/8 公里逐级扩大连锁酒店检索。最多对 12 家做路线评分并展示 3 家。用户选择后，同一家酒店成为所有过夜日的住宿锚点；不得虚构价格、房态、星级或服务质量。

## 5. 模型、Provider 与数据边界

业务只依赖 `StructuredInferenceProvider`，不得依赖Qwen私有wire shape。Qwen Max是质量上限和开发benchmark候选，Plus是主要生产候选，Flash是低延迟候选；只在dev/validation选择并冻结唯一候选后运行sealed blind一次。已固化DeepSeek只作冻结Baseline，不作静默fallback。

固定 model snapshot、schema、prompt、deadline 和失败策略。每次模型调用记录 token、延迟、修复调用、fallback 和估算费用，不记录密钥、完整原文或未脱敏响应。模型晋级必须通过同一冻结数据和确定性 scorer。

高德、天气、搜索、知识和社交内容必须先通过数据留存、来源、成本、过期和许可准入。未取得保存权的响应只能短期使用。暂不抓取小红书；RAG 只允许检索有来源和时效的建议性 `KnowledgeClaim`，不得决定地点身份、路线或硬事实。

用户记忆必须显式开启、结构化、可查看、可更改、可删除。原始攻略、截图和聊天默认不进入长期记忆，训练或评测使用需要单独同意。

## 6. 权威顺序与 Goal 执行

冲突时按以下顺序处理：

1. 本文件；
2. `docs/product/PROJECT_CHARTER.md`；
3. `docs/product/TRIP_CHECK_SPEC.md`；
4. `docs/product/TRIP_CHECK_API_CONTRACT.md`；
5. `docs/ARCHITECTURE.md` 与 Accepted ADR；
6. `docs/governance/PROGRAM.md`；
7. `docs/governance/AGENT_GATE_PROTOCOL.md`；
8. `docs/governance/CURRENT_GOAL.md`、`ROADMAP.md`、`RELEASE_GATES.md`；
9. 当前 commit/config/dataset 对应的 evidence。

### 唯一 Git 开发基线

- `origin/develop`是唯一集成基线；`main`保持受保护状态，未经人工批准不得合并。
- 当前Goal的实现分支必须从现场fetch后的`origin/develop`创建，并在`CURRENT_GOAL.md`记录exact baseline、upstream和远端readback。当前允许继续使用的实现分支是Goal中声明的分支。
- 历史P0～P6、旧评测、旧产品实验和已完成专项分支只保留为只读历史。除非当前Goal显式列为可复用资产并经过差异审查，否则不得继续在这些分支开发或把其`AGENTS.md`、`CURRENT_GOAL.md`当作当前状态。
- 分支内旧指导文件不得覆盖`origin/develop`当前版本。任何缺少当前`AGENTS.md + CURRENT_GOAL.md`的checkout只能做只读考古；写入前必须回到当前基线建立新分支。
- “分支统一”只允许把已完成且仍适用的资产并入`develop`；不得为追求表面一致而合入失败实验、未提交草稿、过期Goal或修改历史证据，也不得force-push或重写历史。

任何时刻 `CURRENT_GOAL.md` 只能有一个 `APPROVED` 或 `IN_PROGRESS` Goal。当前和可自动激活的planned Goal都必须写明用户Outcome、Dependencies、Scope、Non-goals、Authority、Baseline、Invariants、Acceptance/Gate、Verification、Budget、HITL、Stop conditions、Checkpoint、Auto-advance和Completion record；动态baseline可标记为“激活时填写”，其他字段不得省略。

每个可回滚切片后必须更新Goal checkpoint：用户结果、commit、实际验证、证据等级、剩余工作、新风险和下一自主动作。完成时先push并readback subject checkpoint；随后在一个治理过渡commit中，以完整当前合同生成最终completed归档，把 `CURRENT_GOAL.md` 原子替换为下一份完整 `APPROVED`合同，并同步替换`docs/governance/current_goal_binding.json`、把`authority_policy.json`精确推进到下一Goal generation、冻结该Goal专属scorer/threshold/schema/exporter，再push/readback并由独立custody登记新generation anchor。Program的Goal表、公钥、registry身份和既定自动Gate合同不得在自动过渡中改变。归档不得丢字段或保留PENDING；过渡commit不要求把自身未知hash写进自身。不得留下已完成Goal继续指挥开发，也不得跳过Program顺序。

新组件固定执行“实验 → 同数据比较 → 达到预设门槛 → 进入运行时”。失败是调查证据，不能通过弱化测试、修改 blind/oracle、隐藏错误或缩小用户目标获得 PASS。

G01～G07 必须执行 `docs/governance/AGENT_GATE_PROTOCOL.md`：隔离的 GPT-5.6-sol 任务只能形成 `MULTI_AGENT_SIMULATED_REVIEW` 或 `SEALED_AGENT_BLIND`，不得写成真人标注、真人验收或组织外独立证据。对应 Goal 的自动化、live Provider、三角色审查、ultra 裁决、sealed blind 和 fresh readback 全部通过，且没有未处理P0/P1或属于当前Goal的P2，才能标记 `AGENT_GATE_PASS`；候选绑定改变即使旧结论失效。G07 的最高自动状态是 `VNEXT_CANDIDATE_READY_AGENT_VERIFIED`，不得自动进入 H1。

Agent Gate 的 `authority_policy.json` 按Goal使用1～7代权限锚。G01首次Git新增只建立`BOOTSTRAP`预锚：它不能登记anchor、读取角色私钥、生成组件回执或PASS；完整live capture、外部authority signer与所有P0/P1闭合后，必须先生成由`SEALED_CUSTODY`签名的`authority-activation-readiness-v1`，机器绑定bootstrap commit/tree/policy/core、ACTIVE policy、AMap/Qwen执行回执、capture runner、registry合同及外部signer执行回执，才允许一次原子`BOOTSTRAP → ACTIVE`提交并由独立custody登记generation 1。后续只能在上一Goal已登记FINAL_GATE PASS的原子过渡commit中精确加一。每一代ACTIVE policy与`immutable_protocol_paths`从该Goal anchor起逐字节冻结；bootstrap core从首次新增起跨激活保持不变；Program的G01～G07顺序、前驱、自动Gate合同、公钥、registry、路径集合与绑定根跨代稳定。所有anchor的commit/tree/policy/protocol事实只能从Git与canonical远端推导，调用者不得填写。G02～G07必须回读仓库外append-only上一Goal PASS，候选不得自行跳级。私钥、custody registry实际路径、原始Agent输出、OCI镜像archive和blind truth必须留在所有Git worktree及Git目录之外；候选Python进程不得持有角色私钥或私钥路径，ACTIVE前必须交付不导入候选代码的仓库外隔离signer。八种角色签名不得互换，签名只证明对应隔离任务的字段证明，不能单独建立Provider事实、真人证据或PASS。自动产品检查只能在无外网、无宿主挂载、无宿主PID、合成profile且不含Gate/Provider秘密的OCI候选镜像中执行；候选依赖不得在root构建阶段执行。首次执行必须按完整`sha256:` image ID保存镜像，把只含一个目标image、无额外tag/链接/路径穿越且OCI root digest或legacy config digest与回执image ID一致、legacy graph精确等于primary manifest、attestation严格为unknown/unknown与in-toto、所有config/blob自校验的仓库外archive之路径、hash、大小和image ID写入回执；fresh readback无论本地tag是否存在都必须从单一安全句柄复制到匿名快照、解析校验并经stdin加载，只按完整image ID复跑，不得把可变tag或冷重建冒充原镜像。类型化effect表本身不证明live Provider调用：正式live证据还必须绑定custody登记的registry、一次性mint、冻结HTTPS capture runner的逐effect purpose签名和完整coverage；链路未完成时exporter、live component builder与verifier必须fail closed并保持`NOT_RUN`。Sealed scorer必须从一次冻结的输入、候选预测和仓库外truth重新计算完整冻结指标，禁止接受调用者手填aggregate metrics；首次验证尝试无论成功、失败或格式错误都消费一次性nonce。

activation-readiness 还必须绑定排除该自引用回执固定路径后的完整 ACTIVE tree，以及 ACTIVE 的 policy、Program core、config 与 data 分组哈希。固定回执路径之外的任一 Git blob 变化都会使旧回执失效，禁止把同一 readiness 跨实现、配置或数据树重放。
由于回执位于 data root 内，ACTIVE data 分组使用同一固定路径排除；禁止把回执自身或签名计入其自身摘要。

## 7. 授权与证据边界

Program/当前 Goal 明确预批准的开发分支、追加式 migration、v3 API、现有零增量费用 Provider 矩阵、离线测试、Agent Gate 任务、checkpoint commit/push 可自主执行。当前环境已有 Qwen/高德开发授权按 `AUTO_DISCOVERED_PROVIDER_BINDING` 和 `OWNER_ATTESTED_EXISTING_AUTHORIZATION` 记录；不得打印或提交密钥，也不得把 Provider 未暴露字段重新变成用户 HITL。

以下必须人工批准：

- 改变产品目标、跳过 Goal、降低 Gate；
- 未被 Program 预批准的公共 schema/API、migration 或生产依赖；
- 新账号、绑卡、付费 Provider、扩大外部数据或生产调用；
- 修改 sealed blind/oracle；
- H1 真人、招募、consent、公网、部署、release、合并 `main`；
- 删除旧数据、旧 API 或受保护分支。

证据等级固定分开披露：`AUTOMATED_TEST`、`LIVE_PROVIDER_EVIDENCE`、`MULTI_AGENT_SIMULATED_REVIEW`、`SEALED_AGENT_BLIND`、`HUMAN_USABILITY`、`PRODUCTION_EVIDENCE`。历史 Intake 或 Candidate PASS 不自动适用于新版 commit；Agent Gate 不得替代 H1、生产或商业证据。

普通代码、测试、构建、Agent 审查或 sealed blind 失败都留在当前 Goal 继续诊断。只有需要改变产品目标/Gate、读取或修改 blind truth、新增账号/费用/数据权限，或进入 H1、公网、生产、release、`main` 时才停止请求项目所有者决定。

默认按 Goal 选择最小充分验证。未运行的层级写 `NOT_RUN`，不得推断通过。
