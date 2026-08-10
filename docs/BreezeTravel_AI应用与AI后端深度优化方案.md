# BreezeTravel：AI 应用与 AI 后端深度优化开发方案

> 文档版本：v2.0
> 基线日期：2026-08-09
> 目标岗位：AI 应用开发、Agent 工程、Agentic RAG、AI 后端、AI 全栈
> 项目定位：可公开体验、可验证任务完成、可追溯证据、可观测、可降级的多人协同旅行规划系统
> 预计开发周期：6～9 周；周期只用于排定优先级，不作为交付事实

## 1. 文档目的与最终判断

本方案用于把 BreezeTravel 从“功能较完整的 Agent/RAG 项目”升级为“能够在大厂 AI 应用开发面试中承受连续深挖的个人作品”。

最终成果不以框架数量、Agent 数量或单次演示成功为标准，而必须同时回答：

1. 用户的旅行规划任务是否真正完成；
2. 系统如何区分硬约束、软偏好、冲突条件和证据不足；
3. 推荐和行程中的事实由什么来源支持，数据是否过期；
4. Agent、固定工作流和规则逻辑分别负责什么，为什么这样分工；
5. RAG、Router、Planner 和 Critic 的收益是否经过独立评测和消融；
6. 工具超时、部分失败、限流、客户端断开和多实例运行时会发生什么；
7. 多人协同修改后，旧的验证结果是否会被正确作废；
8. 指标能否绑定代码、数据、模型、Prompt、配置和运行环境并被复现；
9. 项目是否产生了真实用户任务数据，而不只是开发者自测；
10. 哪些结论已经实现并验证，哪些仍只是生产化设计。

完成本方案后，项目可以成为 AI 应用/AI 后端校招中的主项目；它不等同于真实大厂生产系统，也不能替代算法、Python、网络、操作系统、数据库、Transformer 和模型推理基础。

### 1.1 岗位能力映射

| 岗位考察 | 本项目的主要证据 | 对应阶段 |
|---|---|---|
| 从模糊业务到 AI 方案 | TripTaskSpec、澄清、约束冲突和任务完成定义 | R1、R6 |
| Agent/Workflow 设计 | 有限 ReAct、工具策略、固定 Planner、定向修复边界 | R1、R4 |
| RAG 质量 | 公开来源、混合检索、引用、blind、bad case 和消融 | R3、R4 |
| Memory | 长短期边界、可信度、纠错、删除、TTL 和注入策略 | R2 |
| AI 后端 | SSE、deadline、取消、限流、熔断、并发隔离和多实例 | R5 |
| 安全治理 | 房间授权、WebSocket 鉴权、Prompt Injection、PII 脱敏 | R2、R5 |
| 评测与迭代 | 固定数据集、三态 Verifier、Judge 校准、原始 case | R3、R4 |
| 产品交付 | 公网 Beta、协同任务、真实用户对照和失败复盘 | R3、R6 |

项目实现只能为这些能力提供可追问的实例。算法题、计算机基础和模型原理需要单独准备，不能用项目复杂度代替。

## 2. 事实边界和证据等级

### 2.1 能力状态

每项能力只能使用以下一个主状态：

| 状态 | 含义 | 可否写入简历 |
|---|---|---|
| `planned` | 仅有设计，尚无可运行实现 | 否 |
| `implemented` | 代码存在，但未完成对应验证 | 只能描述为开发中 |
| `unit_verified` | 通过确定性单元测试 | 可写实现，不写端到端效果 |
| `integration_verified` | 在真实依赖或受控集成环境通过 | 可写本地/集成验证 |
| `publicly_verified` | 公网部署和公网 E2E 有可回读结果 | 可写公网验证 |
| `user_validated` | 真实用户按固定任务完成测试 | 可写用户验证及其样本边界 |

`production_claim_not_made` 不是能力状态，而是所有个人项目发布中的永久声明：除非确有长期真实生产流量，否则不使用“生产级”“海量并发”“大规模商业落地”等表述。

### 2.2 每项证据的最小结构

```json
{
  "capability_id": "constraint-verifier-v1",
  "status": "integration_verified",
  "commit_sha": "...",
  "code_paths": ["backend/app/constraints/verifier.py"],
  "test_command": "...",
  "dataset_hash": "...",
  "corpus_hash": "...",
  "model": "...",
  "prompt_version": "...",
  "environment": "docker-local-real-db",
  "raw_artifacts": ["backend/evidence/releases/..."],
  "known_failures": [],
  "verified_at": "ISO-8601"
}
```

没有固定输入、原始输出和版本信息的数字，不进入 README、简历或演示材料。

## 3. 当前系统基线

### 3.1 已存在的主链路

当前代码已经形成以下产品链路：

```text
Next.js 房间界面
  -> HTTP/SSE
  -> FastAPI
  -> LangGraph Router / Tool Executor / Synthesizer / Critic
  -> 高德 POI / RAG / Weather
  -> Planner 固定工作流
  -> Itinerary
  -> Yjs 多人地点投票、备注和阶段同步
```

现有能力包括：

- Router 有限 ReAct 和最大轮数限制；
- 多工具并发、工具级超时和部分失败结果保留；
- RAG 的 pgvector、PostgreSQL 全文检索、RRF、HyDE、Multi-Query 和 Reranker；
- 引用标题、URL、摘录、许可证、revision、语料类型传播；
- LangGraph PostgreSQL Checkpoint；
- SSE 节点事件、地点预览、增量更新和取消传播；
- Planner 的聚类、距离矩阵、排序、天气、模板排程、规则检查和 Tips；
- Working Memory、Long-term Memory；
- Yjs CRDT 协同；
- Redis 限流基础；
- Docker Compose、CI、部署 workflow、MCP Server 和证据发布脚本。

### 3.2 当前证据边界

当前仓库存在 12 个审核公开来源、1,092 个公开语料 chunk、60 条 RAG blind 和 24 条 Router blind 的本地结果。其确定性结果可以证明来源召回和当前小型路由集通过，但不能证明复杂回答、复杂多轮任务或公网稳定性。

`backend/evidence/latest.json` 仍标记为 `historical_baseline_pending_rerun`。历史 RAGAS 指标使用过合成语料，不得与当前公开语料结果混写。

当前发布基线仍有以下已知问题：

- 工作区尚未形成干净 release；
- Demo 测试单独运行与组合运行结果不一致；
- CI 未覆盖完整 Demo 主链路；
- Python lint 当前不阻断；
- 定时公网 Playwright 已配置，但配置存在不等于公网运行已经成功；
- 数据库迁移和 Checkpoint setup 仍与应用启动存在耦合；
- 现有 `/metrics` 主要使用进程内字典，多实例不共享；
- 现有 Planner `critic_v2` 生成违规列表后直接进入 Tips，没有形成真正的定向修复回路；
- Chat、Optimize、部分 Room API 和 Yjs WebSocket 尚未形成统一的房间成员授权边界；
- Long-term Memory 会自动提取并追加偏好，但缺少完整的同意、纠错、删除、TTL 和污染防护。

## 4. 目标架构

```text
用户自然语言 / 房间协同状态
  -> TripTaskParser
       -> TripTaskSpec
       -> missing_fields / conflicts / assumptions
  -> Router / Tool Planner
       -> Tool Policy Gate
       -> POI / Public RAG / Weather 并行调用
       -> ToolReceipt + Provenance
  -> Candidate Synthesizer
  -> Constraint-aware Planner
       -> Candidate Itinerary
  -> ItineraryVerifier
       -> SATISFIED / VIOLATED / UNKNOWN
       -> VerificationReport
  -> Targeted Repair Controller
       -> 只修复明确违反项，有限轮次
  -> Yjs 多人协同修改
       -> planning_input_hash 变化
       -> 旧验证报告立即失效并重新验证
  -> Trace / Metrics / Evaluation / Feedback / Evidence Release
```

### 4.1 决策边界

| 决策类型 | 负责模块 | 原则 |
|---|---|---|
| 模糊意图、查询改写、候选推荐、自然语言摘要 | LLM | 允许概率性，但必须记录模型、Prompt 和输入证据 |
| 工具选择边界、权限、参数、调用预算 | Policy + 程序逻辑 | 未知工具、越权参数和超预算调用默认拒绝 |
| 必选/排除、重复、时间链、单位、revision | Verifier | 确定性检查，不交给 LLM Judge |
| 缺少价格、营业时间、儿童政策等 | Verifier `UNKNOWN` | 不得伪装成满足或违反 |
| 局部修复建议 | LLM 或规则修复器 | 只能修改允许字段，修复后必须重新验证 |
| 多人协同冲突 | CRDT + 业务策略 | CRDT 解决数据合并，业务规则解决权限和语义冲突 |

## 5. 核心数据契约

### 5.1 TripTaskSpec

新增 `backend/app/schemas/task_spec.py`：

```json
{
  "schema_version": "1.0",
  "task_id": "uuid",
  "room_id": "room_xxx",
  "task_revision": 3,
  "city": "杭州",
  "date_range": {"start": "2026-10-02", "days": 3},
  "travelers": {"adults": 2, "children": 1, "seniors": 0},
  "budget": {
    "amount": 3000,
    "currency": "CNY",
    "scope": "total",
    "include_transport": true,
    "include_hotel": true
  },
  "must_include": [
    {"kind": "place", "value": "西湖", "source": "user_explicit"}
  ],
  "exclude": [
    {"kind": "activity", "value": "高强度爬山", "source": "user_explicit"}
  ],
  "hard_constraints": [
    {
      "id": "c_max_travel",
      "type": "max_daily_travel_minutes",
      "operator": "lte",
      "value": 120,
      "unit": "minute",
      "source": "user_explicit"
    }
  ],
  "soft_preferences": [
    {"id": "p_indoor_rain", "type": "prefer_indoor_on_rain", "weight": 0.8}
  ],
  "assumptions": [],
  "missing_fields": [],
  "conflicts": []
}
```

约束：

- `user_explicit`、`room_consensus`、`memory`、`inferred` 必须区分；
- 推断值不得覆盖显式用户输入；
- Memory 只能作为软偏好，除非用户本轮明确确认；
- 金额、时间、距离和人数必须有单位及作用域；
- 信息不足时输出 `missing_fields`，不猜测关键硬约束；
- 矛盾条件写入 `conflicts`，在规划前解决；
- `task_revision` 每次硬约束变化递增。

### 5.2 VerificationReport

新增 `backend/app/schemas/verification.py`：

```json
{
  "report_id": "uuid",
  "task_id": "uuid",
  "task_revision": 3,
  "itinerary_id": "uuid",
  "itinerary_version": 2,
  "planning_input_hash": "sha256:...",
  "overall_status": "UNKNOWN",
  "checks": [
    {
      "constraint_id": "c_budget",
      "status": "UNKNOWN",
      "reason_code": "PRICE_DATA_MISSING",
      "message": "2 个地点缺少可验证票价，无法确认总预算",
      "evidence_refs": ["poi:xxx", "source:yyy"],
      "observed_at": "2026-08-09T...Z"
    }
  ],
  "verified_at": "ISO-8601"
}
```

状态定义：

- `SATISFIED`：已有充分且未过期的数据，约束满足；
- `VIOLATED`：已有充分且未过期的数据，约束明确违反；
- `UNKNOWN`：缺少关键数据、数据过期、来源冲突或无法可靠判定。

`UNKNOWN` 不进入自动修复回路。它应生成补充信息请求、保守提示或允许用户接受不确定性。

### 5.3 ToolCallEnvelope 与 ToolReceipt

新增 `backend/app/tools/runtime.py`，统一所有工具调用：

```json
{
  "call_id": "uuid",
  "trace_id": "uuid",
  "room_id": "room_xxx",
  "actor_user_id": "user_xxx",
  "tool": "search_places",
  "arguments": {},
  "authorization_scope": ["room:read", "poi:read"],
  "deadline_ms": 5000,
  "idempotency_key": "..."
}
```

工具执行器必须做到：

- Tool 名称和参数使用 allowlist + Pydantic Schema；
- 检查当前用户、房间成员关系和工具权限；
- 请求级总 deadline 向每个工具传播；
- 只对幂等读操作重试；
- 429、timeout、provider_5xx、invalid_payload、empty_result 分类记录；
- 设置单请求工具调用数、单工具并发和全局并发上限；
- 保留成功的部分结果；
- ToolReceipt 记录耗时、结果数量、降级状态和脱敏错误；
- RAG 文档内容只作为数据，不得改变系统指令或工具权限。

### 5.4 协同版本契约

规划请求必须包含：

```text
task_revision
itinerary_version
planning_input_hash
```

`planning_input_hash` 由以下内容规范化排序后计算：

- TripTaskSpec；
- 参与规划的 Place ID 与必要字段；
- votedBy、pin、排除状态；
- 当前行程版本。

前端检测到当前协同状态的 hash 与 VerificationReport 不一致时，必须立刻显示“验证结果已过期”，禁止继续显示绿色通过状态。

## 6. 分阶段开发方案

阶段按依赖关系推进，但不是瀑布式封闭开发：R3 的最小公网 Beta 应尽早暴露真实问题，R4～R5 再依据 bad case 深化。推荐节奏如下：

| 阶段 | 推荐时长 | 前置条件 | 核心交付 |
|---|---:|---|---|
| R0 | 3～5 天 | 当前仓库 | 干净 release、组合测试、迁移和 CI 门禁 |
| R1 | 7～10 天 | R0 | TaskSpec、三态 Verifier、有限定向修复 |
| R2 | 5～7 天 | R0，可与 R1 后半段并行 | HTTP/WS 权限、Memory 治理、协同失效机制 |
| R3 | 7～10 天 | R1、R2 | 高质量端到端评测、公开语料 release、最小公网 Beta |
| R4 | 5～7 天 | R3 固定评测集 | 消融、动态策略、组件删减结论 |
| R5 | 7～10 天 | R3 | deadline、可观测、故障注入和两实例验证 |
| R6 | 5～7 天 | R3～R5 的目标子集 | 用户验证、最终证据和面试材料 |

每阶段开始前固定输入和基线，结束时只通过对应门禁升级状态；未通过时保留原始失败，不用后续阶段的局部成功覆盖前序失败。

### R0：建立可复现发布基线

#### 目标

先消除“单独测试通过、组合测试失败”和“开发机能跑、干净环境不可复现”，为后续所有指标建立可信基础。

#### 开发内容

##### R0.1 配置与依赖注入

- 将模块导入时读取的全局 Settings 改为可覆盖的 `get_settings()` 或服务依赖；
- Demo、Test、Local Real、Public 四类配置显式分离；
- 测试不得通过 import 顺序改变其他测试的配置；
- `.env.example` 只放占位符和安全默认值；
- 固定 Python 3.11、Node 20、PostgreSQL/pgvector 和 Redis 镜像版本；
- 明确 CPU Docker 默认关闭 FT Router，避免模型缺失导致全链路降级。

重点文件：

- `backend/app/config.py`
- `backend/app/main.py`
- `backend/tests/conftest.py`（新增）
- `docker-compose.yml`
- `.env.example`

##### R0.2 测试隔离和 CI 门禁

- 把 `test_chat_demo.py` 纳入 CI；
- 增加随机顺序执行，发现全局状态污染；
- 后端单元测试与 PostgreSQL/Redis 集成测试分 Job；
- `ruff`、`pytest`、`tsc --noEmit`、前端 build 全部阻断；
- lint 不再使用 `continue-on-error`；
- Playwright 分为本地受控 E2E 和公网 E2E；
- 公网 E2E 必须输出 Actions Run URL、目标版本、截图和失败原因。

##### R0.3 迁移和启动流程

- 将数据库迁移从 FastAPI lifespan 拆出为独立命令；
- 应用启动只检查 schema version，不隐式修改生产数据库；
- 新容器启动顺序为：依赖健康 → migration job → backend → frontend；
- Checkpointer 表初始化纳入迁移或受控 bootstrap；
- 增加全新数据库与已有数据库的迁移测试。

##### R0.4 Release Manifest

新增：

- `backend/scripts/build_release_manifest.py`
- `backend/evidence/releases/<commit>/release.json`
- `docs/REPRODUCE.md`

Manifest 至少记录 Commit、依赖锁哈希、迁移版本、配置摘要、测试命令和原始输出路径。

#### 测试

- 全套测试在同一 pytest 进程通过；
- 随机顺序连续运行至少 3 个 seed；
- 全新数据库迁移成功；
- 从干净 clone 使用一条主命令完成构建和验证；
- CI 不读取开发机 `.env`；
- `git diff --check` 和前后端非交互检查通过。

#### 完成定义

- 工作区形成可定位 Commit；
- CI 全绿且不存在非阻断质量检查；
- 干净 clone 回读成功；
- `latest.json` 仍可保持历史待重跑，但其状态和新 release 不混淆。

### R1：TripTaskSpec、三态验证和定向修复

#### 目标

先定义“旅行任务完成”的机器可判定标准，再扩展端到端评测。

#### 开发内容

##### R1.1 Task Parser

新增：

- `backend/app/schemas/task_spec.py`
- `backend/app/agents/nodes/task_parser.py`
- `backend/app/services/task_spec_service.py`

Parser 使用结构化输出，把自然语言转为 TripTaskSpec。解析后执行程序校验：

- 城市、日期、天数、人数、预算格式；
- must_include 与 exclude 冲突；
- 不可能时间窗；
- 预算作用域缺失；
- 关键字段缺失；
- 显式约束与 Memory 冲突。

信息不足时返回 `needs_clarification`，只追问影响规划的最少字段。

##### R1.2 Verifier 规则库

新增：

```text
backend/app/constraints/
  verifier.py
  registry.py
  rules/
    inclusion.py
    exclusion.py
    duplicate.py
    daily_capacity.py
    time_chain.py
    travel_time.py
    opening_hours.py
    budget.py
    weather.py
    hotel_area.py
```

每条规则统一返回 ConstraintCheck，不直接打印字符串。

首批必须完成的规则：

1. 必选地点；
2. 排除地点/活动；
3. 重复地点；
4. 每日地点数量；
5. 时间链和通勤缓冲；
6. 最大日交通时间；
7. 用餐时间窗；
8. 营业时间三态验证；
9. 天气与室内外安排；
10. 预算三态验证。

现有 `critic_v2.py` 的规则逐步迁移到新 registry。无 `place_meta` 时不得静默判定通过，应返回 `UNKNOWN`。

##### R1.3 Planner 输入和输出升级

修改：

- `backend/app/schemas/api.py`
- `backend/app/agents/planner/state.py`
- `backend/app/agents/planner/graph.py`
- `backend/app/api/optimize.py`

`OptimizeRequest` 新增 `task_spec` 和 `planning_input_hash`。Planner 必须优先满足硬约束，再优化软偏好和距离，而不是生成后只检查。

优化目标按层级计算：

```text
一级：硬约束可满足性
二级：明确违反数量最小
三级：未知约束数量最小
四级：交通时间/折返
五级：投票、偏好、多样性
```

##### R1.4 定向修复控制器

新增 `backend/app/agents/planner/repair_controller.py`：

- 只接收 `VIOLATED`；
- 按 day_index 和 constraint_id 生成 RepairPlan；
- 每次修复只允许修改目标天或目标字段；
- must_include、pin、用户锁定天不可被静默删除；
- 修复后重新运行完整 Verifier；
- 最大修复轮数 2；
- 同一 violation 连续出现时停止并返回未解决原因；
- `UNKNOWN` 不自动重试。

Planner 图调整为：

```text
scheduler
  -> verifier
     -> no_violation: tips
     -> repairable_violation: repair -> verifier
     -> exhausted: tips_with_warning
```

##### R1.5 前端约束面板

新增：

- `frontend/src/types/taskSpec.ts`
- `frontend/src/types/verification.ts`
- `frontend/src/components/itinerary/ConstraintPanel.tsx`

界面分别显示满足、违反、未知和验证已过期；未知项不得显示成红色错误，也不得显示成绿色通过。

#### 测试

- 每条规则含正常、违反、未知和边界值测试；
- 同一 TripTaskSpec 对相同 Itinerary 结果确定；
- 缺价格、缺营业时间、来源过期时返回 UNKNOWN；
- 定向修复不改变无关天；
- 修复达到上限时正确终止；
- 前端 hash 变化后旧报告失效。

#### 完成定义

- 端到端接口能够返回 TripTaskSpec、Itinerary 和 VerificationReport；
- 硬规则不依赖 LLM Judge；
- 当前 Planner Critic 从“只报告”升级为“有限定向修复 + 重新验证”；
- 未知信息不再被静默当作通过。

### R2：身份、房间权限、Memory 和协同一致性

#### 目标

建立真实多人产品必须具备的安全和状态边界。

#### 开发内容

##### R2.1 统一身份来源

- Chat、Optimize、Room、Places、Itinerary API 统一使用 JWT 当前用户；
- 请求体中的 `user_id` 不再作为可信身份；
- `thread_id` 必须由 room_id 查库获得或校验归属；
- 创建、加入、读取、修改、导出分别定义权限；
- 新增 `backend/app/services/room_access.py` 集中检查成员关系；
- 所有 Room 资源查询防止 IDOR 越权。

##### R2.2 Yjs WebSocket 鉴权

当前 `new WebsocketProvider(url, roomId, doc)` 只依赖 roomId。升级目标：

- WebSocket 握手携带短期 room token；
- y-websocket 服务验证 JWT、room_id 和成员关系；
- 非成员不能订阅文档或 awareness；
- token 过期、离开房间和权限撤销有明确行为；
- 日志不记录原始 token；
- 公网反向代理限制消息大小、连接数和空闲时长。

##### R2.3 Yjs 与数据库职责

明确：

- Yjs 是房间在线协同状态；
- PostgreSQL 是授权、审计和持久快照；
- 同步接口必须鉴权、幂等并校验 room membership；
- snapshot 写入带 `planning_input_hash`；
- 服务重启后从持久化恢复；
- 删除地点、投票和备注的合并规则写成测试。

##### R2.4 Memory 治理

重构 `backend/app/memory/longterm.py`：

- 只保存稳定旅行偏好，不保存一次性任务事实；
- 偏好记录包含 category、confidence、source_message_ids、created_at、expires_at；
- 相同偏好去重，冲突偏好保留新版本并失效旧版本；
- 用户可查看、修改、删除和关闭长期记忆；
- Memory 注入有长度、类别和可信度上限；
- Memory 只作为软偏好；
- 对话中的网页/RAG 内容不得写入用户偏好；
- 提取失败、模型异常和 embedding 失败不写入空向量伪记录；
- 增加 TTL 清理和审计日志。

新增 API：

```text
GET    /api/user/memories
PATCH  /api/user/memories/{id}
DELETE /api/user/memories/{id}
POST   /api/user/memory-settings
```

##### R2.5 Prompt Injection 和输出边界

- RAG chunk 使用明确 data delimiter；
- 系统 Prompt 声明文档指令不具备权限；
- 文档中出现“调用工具、泄露 Prompt、忽略规则”等指令时记录 injection signal；
- 工具权限由程序校验，不由模型文本授权；
- 输出引用只允许白名单 URL scheme；
- 日志、SSE 和错误响应统一脱敏。

#### 测试

- 非成员读取/修改其他房间返回 403；
- 伪造 user_id/thread_id 无效；
- 未授权 WebSocket 连接失败；
- 两用户并发投票、备注和删除的最终状态符合定义；
- 重启后房间状态恢复；
- Memory 关闭后不写入；
- Memory 删除后不能再次加载；
- 恶意 RAG 指令不能触发越权工具。

#### 完成定义

- HTTP、SSE、WebSocket、持久化和 Memory 使用同一身份与房间边界；
- 公开 Demo 不暴露其他用户房间、偏好或 Trace；
- 多人协同变化会可靠作废旧 VerificationReport。

### R3：端到端评测、公开数据和最小公网 Beta

#### 目标

用高质量固定任务验证完整产品，而不是继续扩大容易满分的小型关键词集。

#### 开发内容

##### R3.1 统一评测数据结构

新增：

```text
backend/evals/
  schema.py
  runner.py
  metrics.py
  adjudication.py
backend/eval_data/
  router/
  rag_claim/
  task_parse/
  end_to_end/
  faults/
```

每个端到端 case 至少包含：

```json
{
  "id": "e2e-hz-001",
  "split": "blind",
  "city": "杭州",
  "turns": [],
  "expected_tool_set": ["search_places", "search_travel_notes"],
  "expected_task_spec": {},
  "required_constraints": [],
  "source_snapshot": "...",
  "fault_profile": null,
  "review_status": "double_checked"
}
```

##### R3.2 数据集治理

- train/dev/blind 按任务和来源隔离；
- Blind 标签在最终评测前不可用于调参；
- 使用 blind 调参后必须生成新 blind 版本；
- Ground Truth 不直接复制目标语料措辞；
- 每条任务记录来源、审核人、修改历史和 hash；
- 公开语料继续保留 URL、revision、许可证、署名、获取时间和 SHA-256；
- LLM 生成的测试样本必须经过人工复核；
- 敏感用户数据不得进入公开评测集。

##### R3.3 评测规模和指标

第一版目标：

| 评测层 | 第一版规模 | 主指标 |
|---|---:|---|
| Router | 80～120 | tool-set exact match、Macro-F1、both F1 |
| RAG Claim | 80～120 | Recall@5、nDCG@10、citation support |
| Task Parser | 60～80 | 字段准确率、constraint F1、clarification accuracy |
| Verifier | ≥100 规则 case | false-pass rate、三态准确率 |
| 端到端任务 | 50～80 | task completion、constraint satisfaction、unknown honesty |
| 故障场景 | 20～30 | 预期降级行为通过率 |

第一版 release candidate 的建议门槛如下。门槛需要在 10～20 条 pilot case 上检查可实现性后冻结，冻结后不得根据 blind 结果临时下调：

| 能力 | 建议门槛 |
|---|---|
| Router | Macro-F1 ≥ 0.90、both F1 ≥ 0.85、tool-set exact match ≥ 0.85 |
| RAG | Recall@5 ≥ 0.85、citation support ≥ 0.90 |
| Task Parser | 硬约束 F1 ≥ 0.90、应澄清问题召回率 ≥ 0.90 |
| Verifier | 确定性规则 false-pass = 0、三态准确率 ≥ 0.95 |
| 端到端 | 硬约束任务完成率 ≥ 0.80、关键无依据断言率 ≤ 0.05 |
| 故障集 | 预期用户可见行为通过率 ≥ 0.90，且无越权或静默伪成功 |

评测口径：

- Verifier 的确定性规则以程序测试为主；
- LLM Judge 只用于表达质量、相关性等主观维度；
- Judge 必须在抽样集上与人工标注校准；
- 报告样本量、分层结果和 bootstrap 置信区间；
- 不只输出总平均，必须输出城市、意图、约束类型和失败类型分桶；
- 每个失败 case 进入可复现 bad-case registry。

##### R3.4 最小公网 Beta

在 R0～R2 通过后即部署最小公网版本，不等待所有可观测和多实例工作完成。

公网 Beta 必须具备：

- 测试账号或低成本登录；
- API 配额和用户限流；
- 房间授权；
- 公开数据和许可证说明；
- Evidence 页面；
- 版本号和 Commit；
- 一条公开端到端 Smoke；
- 失败时对用户说明降级，不显示伪成功。

#### 测试

- 完整 eval runner 可重复执行；
- 数据集和语料 hash 固定；
- Blind 集不被训练/调参脚本读取；
- 公网真实域名通过登录、建房、加入、聊天、优化、约束展示和保存流程；
- 公网 E2E 失败会保留报告，不更新 release evidence。

#### 完成定义

- `backend/evidence/latest.json` 可由发布门禁从历史状态升级为当前 release；
- 至少 50 条端到端任务形成原始结果和 bad-case 列表；
- 公网 Beta 可访问，但仍明确 `production_claim_not_made`。

### R4：消融实验、动态路由与质量优化

#### 目标

证明每个主要 AI 组件为什么存在，并删除没有稳定收益的复杂度。

#### 开发内容

##### R4.1 统一实验运行器

新增 `backend/evals/experiments.py`，通过配置运行：

- Dense；
- BM25；
- Dense + BM25 + RRF；
- Reranker on/off；
- HyDE on/off；
- Multi-Query on/off；
- 固定 Workflow vs ReAct；
- ReAct vs ReAct + Critic；
- 规则 Router vs API LLM Router vs LoRA Router；
- Planner without Verifier vs Planner + Verifier + Repair。

每次实验冻结：

- Commit；
- 数据集；
- 语料；
- 模型与版本；
- Prompt；
- 随机种子；
- 并发和缓存状态；
- 价格表版本。

##### R4.2 指标

每组记录：

- Recall@K、MRR、nDCG；
- Citation support；
- Tool-set exact match；
- Task completion；
- Constraint satisfaction；
- False-pass 和 Unknown rate；
- p50/p95 延迟；
- LLM input/output tokens；
- 估算成本；
- 工具调用次数；
- 降级率和错误类型。

##### R4.3 动态启用策略

实验结论落实为简单可解释策略，例如：

- 短、精确地标查询跳过 HyDE；
- food/hotel/tips 且历史 bad case 显示收益时启用 Multi-Query；
- 只有候选池足够大时启用 Reranker；
- 明确混合实时 POI + 攻略证据时强制 both；
- 简单确定性请求走固定 Workflow；
- 只有开放式多步问题进入 ReAct；
- LoRA Router 只有质量、延迟、成本至少一项显著占优且其他项不退化时进入默认链路。

#### 测试

- 相同配置可复现相同确定性指标；
- 实验缓存不会跨配置污染；
- 动态策略有对应路由测试；
- 删除组件后运行回归；
- 每项结论能定位到原始 case，而不只是一张总分表。

#### 完成定义

- 输出质量、延迟和成本 Pareto 报告；
- 能回答每个主要组件在哪些请求开启、在哪些请求关闭；
- 默认链路不保留无实证收益的组件。

### R5：可靠性、可观测性、多实例和故障注入

#### 目标

证明 Agent 是可运行、可定位和可降级的后端服务，而不只是模型调用链。

#### 开发内容

##### R5.1 请求级 deadline 和取消

- Chat 请求建立总 deadline；
- deadline 分配到 Router、Tools、Synthesizer 和 Critic；
- 工具不得各自用满总超时时间；
- SSE 断开向 LangGraph、LLM 和工具任务传播取消；
- 后台 Memory 写入使用独立受控任务，不继承已断开的主请求；
- 取消后记录 `cancelled_by_client`，不记为服务错误。

##### R5.2 重试、熔断和并发隔离

- 只对幂等读取自动重试；
- 使用指数退避、随机抖动和总 deadline；
- 不对验证错误、权限错误和无效参数重试；
- DeepSeek、高德、天气、Embedding 分别设置 semaphore；
- Provider 熔断状态不得让所有请求长时间等待；
- Redis 故障时限流降级行为明确标注为 single-instance fallback；
- PostgreSQL 不可用时禁止伪造 Checkpoint/Memory 成功。

##### R5.3 OpenTelemetry、Prometheus 和日志

新增：

- `backend/app/observability/tracing.py`
- `backend/app/observability/metrics.py`
- `backend/app/observability/logging.py`

核心指标：

```text
agent_request_total
agent_task_completed_total
agent_degraded_total
agent_duration_seconds
agent_time_to_first_meaningful_place_seconds
agent_tool_duration_seconds
agent_tool_failure_total
agent_react_iterations
agent_llm_input_tokens
agent_llm_output_tokens
agent_estimated_cost_usd
rag_retrieval_duration_seconds
rag_empty_result_total
constraint_check_total
constraint_unknown_total
constraint_false_pass_detected_total
sse_disconnect_total
memory_write_total
memory_write_rejected_total
yjs_connection_total
```

约束：

- Metrics label 不使用 user_id、room_id、query 等高基数字段；
- Trace 可记录脱敏 room hash 和 case ID；
- 结构化日志包含 trace_id、event、duration、error_category；
- Prompt 和用户文本默认不进入公共日志；
- Token 成本绑定模型价格版本；
- Dashboard 必须能从失败请求跳转到相关 Trace 或日志查询。

##### R5.4 性能测试方法

分开报告：

1. 纯应用/Mock 基准；
2. 本地真实数据库和 Redis；
3. 真实外部 API；
4. 公网端到端。

每份结果记录：

- 并发数、到达率、持续时间、样本量；
- keep-alive、缓存冷热、模型和供应商；
- 首事件、首个真实地点、完整响应三个延迟；
- 成功、降级、失败比例；
- 外部 API 与应用内部耗时；
- Token 和单次成本；
- Commit、配置和环境。

`thinking` 事件不计作有用结果。建议目标只有在首次基线后再冻结；未实测前不写入简历。

首轮基准完成后，可将以下目标作为 release guardrail，而不是当前事实：

| 指标 | 建议目标 | 适用条件 |
|---|---:|---|
| Time to First Meaningful Place p95 | < 5s | 真实外部 API、暖实例 |
| 完整任务 p95 | < 15s | 20 并发、固定任务混合 |
| 非 LLM 应用错误率 | < 1% | 不含供应商拒绝和用户取消 |
| SSE 断开后任务释放 p95 | < 2s | 可观测到 LLM/工具取消结果 |
| 单工具故障可解释降级率 | ≥ 95% | 仅统计预定义可降级场景 |

任何未达目标的结果保留为基线和优化输入，不删除或改名为“异常样本”。

##### R5.5 故障注入矩阵

至少覆盖：

| 故障 | 预期行为 |
|---|---|
| DeepSeek timeout/429/5xx | 有其他结果则降级，无证据则明确失败 |
| 高德 timeout/空结果 | 保留 RAG，标记实时 POI 不可用 |
| RAG timeout/空检索 | 保留实时 POI，不生成无来源攻略事实 |
| Weather 失败 | 行程可生成，但天气约束为 UNKNOWN |
| Redis 不可用 | 限流降级边界可见，不声称多实例一致 |
| PostgreSQL 不可用 | Checkpoint/Memory/保存失败明确返回 |
| 无效模型 JSON | 结构化解析失败并受控回退 |
| SSE 断开 | 及时取消主任务并停止费用继续增长 |
| Yjs 重启 | 持久状态恢复或明确恢复失败 |
| Prompt Injection | 不越权调用、不泄露系统指令 |
| Memory 污染 | 低置信偏好不进入硬约束 |

每个场景保存输入、注入方式、预期、实际、Trace 和结论。

##### R5.6 多实例

- 两个 backend 实例通过负载均衡接入；
- Redis 统一限流；
- PostgreSQL Checkpoint 可跨实例恢复；
- migration 使用独立 Job；
- 指标汇聚到 Prometheus；
- SSE 单连接保持在一个实例，不要求跨实例迁移；
- Yjs 首先验证单实例持久化；只有确需多实例时，引入 Redis adapter、共享更新日志或一致性路由，不能用两个独立 y-websocket 实例冒充协同一致。

#### 完成定义

- 能通过 Trace 解释一次真实失败；
- 客户端断开后任务释放时间有实测数据；
- 单工具失败不会丢失其他成功结果；
- 两实例下限流、Checkpoint 和指标行为有可回读结果；
- 故障注入报告包含未解决问题，不要求制造全绿。

### R6：真实用户验证、最终发布和面试证据包

#### 目标

把技术系统收敛为可访问、可复现、可讲清楚的完整作品。

#### 开发内容

##### R6.1 业务基线和固定任务

准备 3～5 类标准任务，例如：

- 亲子三日游，包含雨天和每日交通时间上限；
- 两人低预算旅行，必须包含两个地点并排除排队店；
- 多人投票冲突，需要保留多数选择和一项个人硬约束；
- 工具部分失败情况下继续生成可用方案；
- 协同修改后重新验证约束。

对照方式：

- 手工使用群聊/地图收藏完成；
- 使用 BreezeTravel 完成同类任务；
- 任务顺序交叉，降低学习效应。

##### R6.2 用户指标

10～20 名用户按固定任务记录：

- Time to First Valid Plan；
- 任务完成率；
- 硬约束遗漏数；
- 生成后修改次数；
- 最终保留推荐地点比例；
- 协同冲突解决次数；
- 保存/导出成功率；
- 简短满意度和主要失败原因。

样本量小，报告原始分布和样本边界，不用小样本包装总体结论。

##### R6.3 公网发布门禁

发布前必须满足：

- 当前 Commit 的 CI 全绿；
- 数据库迁移成功；
- 公开语料 license/attribution 完整；
- 端到端 blind、故障集和公网 Smoke 通过各自门槛；
- Evidence 页面与 release manifest 一致；
- Secret、用户数据、Prompt 和私有 Trace 已脱敏；
- Demo 配额、告警和关闭开关可用；
- 失败不会原子更新 `evidence/latest.json`。

##### R6.4 面试材料

最终交付：

```text
README.md                         三分钟项目总览
docs/ARCHITECTURE.md              请求/状态/数据流与边界
docs/EVALUATION.md                数据集、指标、消融和 bad case
docs/RELIABILITY.md               deadline、降级、故障注入和 SLO
docs/SECURITY.md                  权限、Memory、Injection 和隐私
docs/INCIDENT_REVIEW.md           一份真实故障复盘
docs/REPRODUCE.md                 干净环境复现
backend/evidence/releases/...     原始证据和 manifest
三分钟演示视频                    公网真实流程，不剪掉失败提示
```

面试叙事统一为：

```text
业务问题
-> 原始失败或基线
-> 请求、状态和数据流
-> 技术选择与未选择方案
-> bad case 和定位证据
-> 修改
-> 对照结果
-> 当前边界与下一步
```

#### 完成定义

- 面试官可访问公网产品；
- 可查看当前 release 的公开证据；
- 可从干净环境复现核心离线评测；
- 能看到真实用户任务、真实失败和故障处理；
- 简历数字均能定位到原始结果。

## 7. 阶段优先级和停止条件

### P0：必须完成

- R0 可复现发布基线；
- R1 TripTaskSpec、三态 Verifier、定向修复；
- R2 房间权限、WebSocket 鉴权、Memory 最小治理；
- R3 第一版端到端评测和公网 Beta；
- R6 基础用户任务和证据发布。

P0 完成后，项目已经具备强校招主项目价值。

### P1：显著增强 AI 后端竞争力

- R4 消融和动态路由；
- R5 deadline、取消、重试、熔断、可观测性；
- 两个 backend 实例验证；
- 系统化故障注入。

### P2：只在证据驱动下实施

- Yjs 多实例；
- Kubernetes；
- 消息队列；
- Parent-Document Retriever；
- GraphRAG；
- 更大规模 LoRA/SFT；
- 新增更多 Agent 或 Agent 框架。

停止条件：如果某项技术不能对应明确 bad case、可测指标或真实部署需要，则不为“技术栈丰富”而加入。

## 8. 建议的阶段验收总表

| 阶段 | 核心产物 | 阻断门禁 |
|---|---|---|
| R0 | 干净 release、CI、迁移、manifest | 全套组合测试和干净 clone 不通过则停止 |
| R1 | TaskSpec、Verifier、Repair、约束 UI | UNKNOWN 被当作通过或修复无上限则停止 |
| R2 | HTTP/WS 权限、Memory 治理、协同失效机制 | 存在跨房间越权或旧验证仍显示通过则停止 |
| R3 | 高质量评测、公开语料、Beta E2E | Blind 泄漏或公网 Smoke 失败则不发布指标 |
| R4 | 消融报告、动态策略 | 无原始 case 或配置不可复现则不采纳结论 |
| R5 | Trace、指标、故障与多实例报告 | 只有配置没有真实故障/负载结果则不升级状态 |
| R6 | 用户测试、最终证据、演示和复现文档 | 简历数字无法回读则不写入简历 |

## 9. 最终效果

完成后，BreezeTravel 应具备以下可见行为：

1. 用户输入多约束需求，系统生成可编辑 TripTaskSpec；
2. 关键字段不足或约束冲突时先澄清；
3. Router 只调用完成任务所需的工具；
4. 每个工具调用都有权限、deadline、call_id、结果和失败分类；
5. 推荐中的公开知识显示来源，实时数据带获取时间；
6. Planner 优先满足硬约束，再优化距离、投票和偏好；
7. Verifier 明确区分满足、违反和未知；
8. Critic/Repair 只修改明确失败部分，并在有限轮次后终止；
9. 多人协同修改会使旧验证报告立即失效；
10. Memory 可查看、修改、删除和关闭；
11. 工具部分失败时保留成功结果并向用户说明降级；
12. 每次请求能够关联 Trace、延迟、Token、成本和失败类型；
13. 评测结果绑定 Commit、数据、语料、模型、Prompt 和配置；
14. 公网产品、用户任务和真实故障都有可回读证据。

最终简历价值不再是“使用了很多 Agent/RAG 技术”，而是：

> 独立完成可评测、可追溯、具备三态约束验证、权限治理、故障降级和多人协同一致性的 Agentic RAG 旅行规划产品，并通过盲测、消融、Trace、故障注入、公网 E2E 与真实用户任务证明效果和边界。

## 10. 简历声明边界

可以写入简历的内容必须满足：

- 代码可定位；
- 测试或运行结果可回读；
- 指标对应固定数据集和版本；
- 历史合成语料与当前公开语料分开；
- Mock、本地真实依赖、公网和用户验证分开；
- 单实例和多实例分开；
- 构建成功与实际运行成功分开；
- 配置了 CI/E2E 与 CI/E2E 真正成功分开；
- 规划目标不写成已实现；
- 两实例、20 并发等有限实验不描述为海量生产能力；
- `UNKNOWN` 不描述成约束满足；
- 没有真实长期生产流量时保留 `production_claim_not_made`。
