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
- 原始截图只进入短期临时存储，不得进入数据库、日志或 Git，成功、失败、取消和超时终态都删除。OCR文本、阅读顺序和bbox来源映射作为加密`SourceDocument`继承30天上限和主动删除；删除后只保留不可逆hash、结构化结果、版本和清理回执。

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

暂不抓取小红书；RAG 只允许检索有来源和时效的建议性 `KnowledgeClaim`，不得决定地点身份、路线或硬事实。

用户记忆必须显式开启、结构化、可查看、可更改、可删除。原始攻略、截图和聊天默认不进入长期记忆，训练或评测使用需要单独同意。

## 6. 产品主线与执行比例

治理、测试、审查和证据只用于保护产品结果，不能自行升级为产品目标。优先级固定为：

1. 可运行、可查看的端到端用户主链；
2. 真实模型/Provider效果、确定性质量指标和失败降级；
3. 当前版本所需的最小充分证据；
4. 候选版、生产或组织级加固。

执行时遵守以下硬规则：

- G01～G03固定为`CORE_MVP`，G04～G06固定为`PRODUCT_ENHANCEMENT`，六个Goal统一使用`PRODUCT_DELIVERY_GATE`；它只回答当前用户旅程是否可用以及安全底线是否保持。G07才使用`HARDENED_CANDIDATE_GATE`完成统计、性能、复审、blind、exact binding、可靠性和供应链收口。
- 始终阻断的安全底线只有：错城/错类别/描述句或URL被当成地点；越权、原文或内部信息泄漏；数据删除失败；编辑后自动调用路线Provider；把`UNKNOWN/UNAVAILABLE`冒充成功。
- G01～G03每个活动切片只能是`PRODUCT / BLOCKING_DEFECT / GOAL_TRANSITION`。`BLOCKING_DEFECT`必须有可复现且直接破坏当前旅程或安全底线的P0/P1；P2/P3登记后续归属，不得阻止已经通过的当前交付门。
- G01～G03机器拒绝Agent Gate、blind、authority、custody、签名、broker、候选回执平台、通用评测基础设施以及不影响当前用户结果的性能/可靠性加固。已有实现统一标记为`FROZEN_G07_ASSET`，保留但不得继续修改或作为当前依赖。
- 一个问题最多两轮“修复→复审”。两种实现仍失败时优先采用诚实的保守降级，例如“地点待确认”或`LIMITED`，不得继续建设新治理系统。
- 除Goal过渡外，不允许独立纯文档checkpoint。普通PR必须改变产品运行时代码/API/UI，或关闭一个已登记P0/P1；`product_progress=NONE`不能获得`PASS`。
- 当前Goal交付门通过后必须允许归档和推进，G07项目为`NOT_RUN`不得阻断。脚本、测试说明或文档变化不作废产品证据；只有相关运行时代码或Provider配置改变才改变产品指纹并重跑对应验证。
- G01～G03合同激活后冻结；增加范围、提高门槛或修改校验器必须由项目所有者通过CODEOWNERS审批。`develop`只经PR和唯一必选检查`core-mainline`更新，不设置日常bypass。
- G03通过后不得自动进入G04；状态固定切换为`CORE_MVP_OWNER_REVIEW_PENDING`，交付可运行主链、演示脚本、已知边界和验证结果，等待项目所有者体验验收。
- 测试通过、receipt或签名都不是用户体验或真人证据；未运行的模型、Provider、blind、真人和生产证据继续如实写`NOT_RUN`。

每个切片在`current_work_packages.json.active_slice`登记用户结果、当前Goal验收引用、工作类型、最小改动、允许路径、禁止机制、`repair_review_cycle`、产品进展和停止条件，并在提交前运行`python -m scripts.validate_core_mainline`。机器合同的唯一权威为`docs/governance/product_delivery_gates.json`。

并行开发必须读取`docs/governance/current_work_packages.json`并通过机器校验：

- 主对话框是唯一集成者，负责版本化提示词、工作包登记、writer调度、验收、状态变更和串行合并；它不得在三个已登记贡献包之外再承担一个未登记功能包。长期功能开发只能由用户可见的独立功能对话承担，每个功能对话只拥有一个独立分支、一个独立worktree和一个已登记工作包。
- 子Agent只可做短期标注、独立复核、反方审查和故障诊断；不得拥有功能分支、提交产品代码、修改Goal/registry状态或形成官方`READY_TO_MERGE`。功能修复必须由对应功能对话完成，不能用子Agent绕过writer上限。
- 任一时刻只有一个非终态集成者；集成者始终占一个writer名额，当前Goal最多两个贡献对话为`IN_PROGRESS/BLOCKED_EXTERNAL`。已生成完整提示词但尚未启动的当前Goal包使用非写入状态`WAITING_FOR_WRITER_SLOT`；开发可并行，产品合并必须串行。
- 每个贡献包在`IN_PROGRESS`前必须绑定版本化提示词路径/hash、Goal激活commit、local/remote branch、独立worktree绝对路径、用户可见功能对话引用、完整`owned_paths/forbidden_paths`、验收和定向测试。提示词合同以`docs/governance/WORK_PACKAGE_PROMPT_TEMPLATE.md`为准。
- 所有功能worktree从同一exact product baseline建立并读取同一版本`AGENTS.md`、`current_goal_binding.json`和registry activation；任一hash、binding、branch、worktree或prompt不一致时只能只读。
- 只可提前准备下一个Goal，最多两个`PREPARED_NOT_INTEGRATED`贡献工作包；下一Goal不得提前登记集成者，当前Goal不得依赖下一Goal；不得跨两级开发、提前合并、提前创建migration或改变公共合同。
- `owned_paths`、branch和worktree不得重复或重叠。普通贡献任务不得修改治理文件、Goal/work-package binding、编号migration、共享OpenAPI/生成物和依赖锁文件；不得自行合并，只能commit/push自己的工作包分支并回报远端readback。
- 功能对话报告`READY_TO_MERGE`不等于官方状态。主对话必须验收路径、commit、工作树、定向测试和远端readback后登记`ready_commit`；此后分支tip变化、worktree变脏或继续提交都会使冻结失效。
- 贡献包运行期间，主对话只能提交registry/checkpoint等控制面变更；开始产品代码集成前，所有相关贡献包必须冻结。集成者随后按已登记的领域模型→持久化/API→前端→E2E顺序合并，再运行当前Goal的`PRODUCT_DELIVERY_GATE`。

具体执行模板和本次偏航复盘见`docs/governance/PRODUCT_MAINLINE_EXECUTION_GUIDE.md`。

## 7. 权威顺序与 Goal 执行

冲突时按以下顺序处理：

1. 本文件；
2. `docs/product/PROJECT_CHARTER.md`；
3. `docs/product/TRIP_CHECK_SPEC.md`；
4. `docs/product/TRIP_CHECK_API_CONTRACT.md`；
5. `docs/ARCHITECTURE.md` 与 Accepted ADR；
6. `docs/governance/PROGRAM.md`；
7. `docs/governance/product_delivery_gates.json`与`PRODUCT_MAINLINE_EXECUTION_GUIDE.md`；
8. `docs/governance/CURRENT_GOAL.md`、`ROADMAP.md`、`RELEASE_GATES.md`；
9. 当前 commit/config/dataset 对应的 evidence。

### 唯一 Git 开发基线

- `origin/develop`是唯一集成基线；`main`保持受保护状态，未经人工批准不得合并。
- 当前Goal的实现分支必须从现场fetch后的`origin/develop`创建，并在`CURRENT_GOAL.md`记录exact baseline、upstream和远端readback。当前允许继续使用的实现分支是Goal中声明的分支。
- 并行worktree必须使用`current_work_packages.json`登记的同一baseline、branch、依赖、路径所有权和状态。工作包状态只允许`PREPARED_NOT_INTEGRATED / WAITING_FOR_WRITER_SLOT / IN_PROGRESS / READY_TO_MERGE / MERGED / DEFERRED / BLOCKED_EXTERNAL`；历史v1/v2 registry只读兼容，当前写入和交付门必须使用v3。
- 历史P0～P6、旧评测、旧产品实验和已完成专项分支只保留为只读历史。除非当前Goal显式列为可复用资产并经过差异审查，否则不得继续在这些分支开发或把其`AGENTS.md`、`CURRENT_GOAL.md`当作当前状态。
- 分支内旧指导文件不得覆盖`origin/develop`当前版本。任何缺少当前`AGENTS.md + CURRENT_GOAL.md`的checkout只能做只读考古；写入前必须回到当前基线建立新分支。
- “分支统一”只允许把已完成且仍适用的资产并入`develop`；不得为追求表面一致而合入失败实验、未提交草稿、过期Goal或修改历史证据，也不得force-push或重写历史。

任何时刻 `CURRENT_GOAL.md` 只能有一个 `APPROVED` 或 `IN_PROGRESS` Goal。当前和可自动激活的planned Goal都必须写明用户Outcome、Dependencies、Scope、Non-goals、Authority、Baseline、Invariants、Acceptance/Gate、Verification、Budget、HITL、Stop conditions、Checkpoint、Auto-advance和Completion record；动态baseline可标记为“激活时填写”，其他字段不得省略。

每个可回滚切片后必须更新Goal checkpoint：用户结果、commit、实际验证、证据等级、剩余工作、新风险和下一自主动作。完成时先push并readback subject checkpoint；随后在一个治理过渡commit中，以完整当前合同生成最终completed归档，把 `CURRENT_GOAL.md` 原子替换为下一份完整 `APPROVED`合同，并同步替换`docs/governance/current_goal_binding.json`，再push/readback。G01、G02通过后依序推进；G03只切换到`CORE_MVP_OWNER_REVIEW_PENDING`并停止。只有G07显式启用`HARDENED_CANDIDATE_GATE`时才推进authority generation、冻结对应协议并登记外部anchor。归档不得丢字段或保留PENDING；过渡commit不要求把自身未知hash写进自身。不得留下已完成Goal继续指挥开发，也不得跳过Program顺序。

新组件固定执行“实验 → 同数据比较 → 达到预设门槛 → 进入运行时”。失败是调查证据，不能通过弱化测试、修改 blind/oracle、隐藏错误或缩小用户目标获得 PASS。

G01～G06只执行当前Goal的`PRODUCT_DELIVERY_GATE`；G07才执行`docs/governance/AGENT_GATE_PROTOCOL.md`并要求`HARDENED_CANDIDATE_GATE`。隔离Agent任务只能形成`MULTI_AGENT_SIMULATED_REVIEW`或`SEALED_AGENT_BLIND`，不得写成真人标注、真人验收或组织外独立证据。G07最高自动状态是`VNEXT_CANDIDATE_READY_AGENT_VERIFIED`，不得自动进入H1。

现有`authority_policy.json`、BOOTSTRAP、外部signer/broker/verifier和隔离OCI设计属于`DEFERRED_CANDIDATE_HARDENING`。历史提交和测试保留，不删除、不伪称已运行；G01～G06不得继续实现这条链，也不得因其`NOT_RUN`阻断产品Goal。G07如确有候选篡改、供应链或组织隔离威胁需要，再按ADR-014显式激活、审查和验证。

## 8. 授权与证据边界

Program/当前 Goal 明确预批准的开发分支、追加式 migration、v3 API、现有零增量费用 Provider 矩阵、定向离线测试、checkpoint commit/push 可自主执行；Agent Gate任务只在G07预批准。当前环境已有 Qwen/高德开发授权按 `AUTO_DISCOVERED_PROVIDER_BINDING` 和 `OWNER_ATTESTED_EXISTING_AUTHORIZATION` 记录；不得打印或提交密钥，也不得把 Provider 未暴露字段重新变成用户 HITL。

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
