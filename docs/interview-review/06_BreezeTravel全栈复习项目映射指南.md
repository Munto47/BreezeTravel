# BreezeTravel 全栈复习项目映射指南

> 配套《AI 应用全栈秋招 - 大厂八股复习大纲》。用途不是把项目名硬套到每个术语上，而是把每个知识点落到可核验的代码、数据流、取舍和失败模式。面试中先说明“项目已有事实”，再说“如果生产化我会如何演进”。

## 1. 先记住这一条主线

```text
浏览器（Next.js）
  ├─ HTTP/SSE → FastAPI → LangGraph Agent → POI / RAG / 天气 → PostgreSQL + pgvector / Redis
  └─ WebSocket → y-websocket → Yjs CRDT（多人共享投票、备注、地点）

地点确定后：Planner 子图（K-Means → 距离矩阵 → TSP → 日程/天气/提示）
```

建议使用 60 秒项目开场：BreezeTravel 是一个多人协同旅行规划产品。前端通过 Yjs/CRDT 同步房间状态；后端以 FastAPI 提供 SSE 对话和排线接口；对话由 LangGraph 驱动 ReAct 工具调用，并把高德 POI、游记 RAG 和天气结果融合；RAG 使用 HyDE、PostgreSQL 全文检索、pgvector、RRF 和 reranker；确定地点后独立 Planner 子图用 K-Means 分天、驾车时间矩阵和 TSP 排序。系统还具备偏好记忆、指标、追踪、测试及演示降级模式。

## 2. 大纲模块总览

| 大纲模块 | 与项目的真实关系 | 首选证据/演示 | 面试时必须补足的边界 |
|---|---|---|---|
| 算法与数据结构 | 排线直接使用 K-Means、最近邻 TSP；检索与排序涉及 Top-K | `backend/app/agents/planner/`、`tests/test_optimizer.py` | 最近邻 TSP 是启发式，不保证全局最优；说明复杂度和适用规模 |
| Python、并发、FastAPI | 后端主技术栈；SSE 和并发工具执行是核心链路 | `api/chat.py`、`nodes/tool_executor.py`、`main.py` | 协程不等于并行；CPU 型 reranker 不能阻塞事件循环 |
| PostgreSQL / SQL | 结构化数据、会话 checkpoint、偏好与向量检索的底座 | `db/init.sql`、`rag/retriever.py` | PG 和 MySQL 的锁/MVCC 细节不可混讲；索引策略需用 EXPLAIN 验证 |
| Redis / 缓存 / 一致性 | 缓存高德距离矩阵，降低第三方 API 延迟与配额消耗 | Docker 配置、Planner DistanceAgent | 缓存不是事实源；TTL、失效、击穿和限流是生产化设计题 |
| 网络与 Web 安全 | SSE 负责单向 AI 流；WebSocket 负责双向协同 | `useAIChat.ts`、`useYjsRoom.ts` | SSE 与 WS 的重连、鉴权、背压和跨域需能解释 |
| OS / Linux / Docker | Docker Compose 编排后端、PG、Redis、y-websocket | `docker-compose.yml` | 容器、进程、端口、日志、健康检查与资源限制要能落到排障流程 |
| Java / Spring | 不是本项目技术栈 | 无 | 诚实说明：以 Python/FastAPI 实践为主；只迁移思想，不虚构 Spring 经历 |
| React / Next.js | 前端 UI、地图、流式渲染和协同状态 | `frontend/src/hooks/`、`frontend/package.json` | Zustand 是本地 UI，Yjs 才是共享事实源；避免双向同步环 |
| 分布式 / MQ / 可观测性 | 多服务协作、CRDT、trace、metrics、压测已有实践 | `metrics.py`、`tests/test_planner_graph.py`、压测脚本 | 当前没有 MQ 作为核心已落地链路；应讲“为何现在不引入、何时引入” |
| 工程效能 / 交付 | 单元、集成、离线评估、本地显式门禁、Docker 化 | `verify-local.ps1`、`backend/tests/` | GitHub Actions 已取消；不能宣称存在远程 CI 或完整发布/回滚平台 |
| LLM / RAG / Agent | 项目的核心差异化能力 | `agents/graph.py`、`rag/`、`mcp_server.py` | 评估指标、成本、幻觉、权限和降级必须成套回答 |
| 项目深挖 / 系统设计 | 上述链路可展开成 AI 对话、知识库、协同与行程规划设计题 | 本文第 5 节 | 区分现状、压测数据和未来设计；所有数字应可追溯 |

## 3. 按章节复习：概念怎样自然引到项目

### 02. Python、并发与 FastAPI（P0）

**项目剧情**：用户发送“帮我安排杭州三日游”，浏览器需要一边展示 Agent 正在检索的步骤，一边持续接收推荐地点和文字；工具调用又可能同时访问 POI、RAG、天气服务。

- **`async` / `await` 与事件循环**：`/api/chat` 使用 SSE 持续产生事件；工具执行节点并发处理可独立的 I/O。回答重点是：`await` 在等待网络/数据库时让出控制权，而不是创建 CPU 并行。
- **`gather`、超时与限流**：并发工具调用应设置并发上限、超时和异常隔离。追问“一个工具失败怎么办”时，回答为保留已有观察结果、记录失败原因、由 Synthesizer 明示信息不完整或触发 Critic 重试；不能让单个第三方调用拖死整个请求。
- **SSE**：后端推送 `thinking → place → text → done`；前端 `useAIChat.ts` 增量解析。它适合服务器到浏览器的单向流式回复，比 WS 简单；协同编辑仍选择 WS，因为客户端也持续写共享状态。
- **FastAPI 分层**：API 层做协议/校验与流式包装，Agent 层负责编排，schema 层定义 `Place` / `Itinerary` 等契约，DB 与第三方工具隔离在下层。用 Pydantic 避免把未校验 JSON 直接流入业务。
- **可做最小工件**：画出请求取消时序：浏览器断开 → 生成器检测断开/取消 → 取消未完成 Task → 归还连接/停止流。

### 03. 数据库：PostgreSQL、SQL 与 pgvector（P0/P1）

**项目剧情**：同一套 PostgreSQL 同时保存游记 chunk、向量、用户偏好和 LangGraph 会话状态；“推荐杭州酒店”既要语义召回，也要命中城市与住宿等结构条件。

- **混合检索不是只查向量**：`rag/retriever.py` 将 pgvector 语义检索和 `tsvector` 全文检索合并，再用 RRF 融合。语义检索能找同义表达，BM25 对具体酒店名、地铁线、价格等关键词更稳。
- **SQL / 索引追问**：`tsvector` 适配 GIN 全文索引；向量索引需在 HNSW/IVFFlat 的召回率、内存、建索引和写入成本间选择。先用 `EXPLAIN (ANALYZE, BUFFERS)` 验证，再优化，不能只凭“加索引”。
- **MVCC 与连接池**：PG 读通常通过行版本减少读写互斥，但长事务会阻碍清理并导致膨胀；异步服务必须复用连接池，不能每个请求新建连接。
- **事务边界**：投票/备注的强实时共享事实当前在 Yjs 文档，不应强行包装成 PG 事务；持久偏好、会话和关系数据则需要约束与幂等写入。回答时主动区分“协同状态一致性”和“数据库事务一致性”。
- **可做最小工件**：写一条带 `city` / `intent` 过滤的混合检索 SQL，并说明索引、参数化和 EXPLAIN 观察点。

### 04. Redis、缓存与分布式一致性（P0）

- **已落地场景**：高德驾车时间矩阵代价高、易受配额限制，距离 Agent 使用 Redis 缓存，TTL 为 24 小时。缓存键应包含起终点、出行方式及影响结果的参数。
- **Cache Aside**：先查缓存，未命中再请求高德并写缓存；第三方失败时不可写入伪造成功值。地点变更或路线参数变化后自然换键/主动失效。
- **高频追问**：缓存穿透（非法 POI 组合可短 TTL 缓存空值）、击穿（热矩阵使用请求合并/互斥锁）、雪崩（TTL 加抖动）、热 Key（本地短缓存/拆分/监控）。
- **一致性边界**：Redis 只加速距离估计，不是地点与投票的唯一事实源；路线允许短期陈旧，支付/库存类强一致场景则不能照搬这个方案。

### 05. 网络、HTTP 与 Web 安全（P0）

- **协议选型**：对话采用 HTTP + SSE（服务端连续推送）；多人编辑采用 WebSocket + y-websocket（双向、低延迟）。这正是“按交互方向选择协议”的实例。
- **SSE 工程细节**：`Content-Type: text/event-stream`、事件名/ID、心跳、断线重连、重复事件幂等。当前演示必须能讲清已有协议，生产完善项是 `Last-Event-ID` 恢复和严格的慢客户端背压策略。
- **安全清单**：LLM API Key 仅在后端环境变量；前端只保留可公开的地图 JS 配置。还应补充房间身份/权限、CORS 白名单、输入长度限制、速率限制、日志脱敏与第三方响应校验。
- **AI 特有安全**：游记/工具输出都属于不可信上下文，不能让它们覆盖 system 指令；工具参数须按 schema 校验，MCP 暴露的能力最小化授权。

### 06. 操作系统、Linux 与 Docker（P1）

- `docker-compose.yml` 将 FastAPI、PostgreSQL、Redis、y-websocket 放在可复现的本地服务拓扑中；端口分别对应 HTTP、DB、缓存和协同服务。
- 排障用“症状 → 进程/容器 → 网络 → 依赖 → 日志/指标”路径：例如 `/health` 慢，先确认后端生命周期、数据库池初始化和容器健康状态，而非直接优化业务 SQL。
- 追问容器化的价值时，答依赖一致、环境隔离、可复现；也答代价：镜像体积、配置/secret 管理、存储卷和资源限制。

### 07. Java、Spring Boot（P1）

本项目不能作为 Java/Spring 实战证据。正确关联方式是迁移工程思想：FastAPI 的依赖注入/异常处理/配置管理，对应 Spring 的 DI、Controller Advice、Configuration；asyncpg 连接池对应数据源池。明确说“概念可迁移，底层线程模型、事务代理和 JVM 细节需按 Java 独立掌握”。

### 08. React、Next.js 与前端工程（P1/P0）

- **状态边界**：Zustand 管本地 UI（面板、加载、选中态），Yjs 管多人共享的房间、地点与聊天追加记录。`useYjsRoom.ts` 是协同映射的核心。
- **流式 UI**：`useAIChat.ts` 将 SSE 帧转换为 thinking、地点卡和文本渐进更新；要避免逐帧导致不必要的大范围重渲染。
- **一致性直觉**：Yjs/CRDT 允许多个客户端离线或并发修改后合并，不依赖“抢锁”；但它不替代权限校验，也不自动解决业务语义冲突（例如关闭行程后是否允许继续投票）。
- **地图与安全体验**：地图 SDK Key 按前后端权限区分；动态脚本加载、错误降级、移动端布局与无障碍状态均是加分追问。

### 09. 分布式系统、消息队列与可观测性（P1）

- **现有分布式边界**：前端、Agent 后端、Yjs 服务、PG、Redis、外部地图/天气 API 是多服务协作，不等于项目已经实现了完整微服务治理。
- **可观测性证据**：LangSmith 用于 Agent 节点和工具调用 trace；`/metrics` 暴露成功率、Critic 触发率、平均 ReAct 轮数和工具分布；项目有 `/health` 与压测脚本。
- **为何暂不引入 MQ**：对话与排线是用户等待结果的同步/流式链路，先直接调用能降低复杂度。若要做异步语料入库、评估、通知或长任务，应引入队列，并设计幂等消费者、重试、死信和可观测任务状态。
- **故障演练题**：高德超时 → 缓存兜底/部分结果；RAG 无召回 → Critic 触发有限重试；LLM 不可用 → DEMO_MODE 或明确降级；Yjs 服务断开 → 客户端重连与本地更新后合并。

### 10. 工程效能、交付与服务端语言（P0）

- **测试金字塔证据**：优化器、Planner 子图、RAG、API、Mock 数据、Agent 评测都有测试文件；离线测试避免 API 成本和环境不稳定，集成评估单独运行。
- **质量门禁**：对 RAG 不只看“能回答”，还看 Faithfulness、Relevancy、Context Recall；对意图分类有准确率与延迟评估；对接口有 P50/P95/P99 压测口径。
- **交付边界**：已有 Docker 化与可重复的本地验证脚本，GitHub Actions 已取消；生产部署、远程质量门禁和自动回滚仍是后续能力，不能描述为已上线事实。

### 11. LLM、RAG、Agent 与 AI 应用工程（P0）

这是最重要的项目映射，建议按数据流作答：

1. **Router**：从消息和工作记忆确定意图/工具；可走 LoRA 分类 fast path，必要时由 LLM native tool calling 决策。
2. **ReAct 执行**：`tool_executor.py` 并发调用地点搜索、游记检索、天气，在有限轮数内累积观察结果。
3. **RAG**：短查询或精确地标跳过 HyDE；hotel/food/tips 可进行 Multi-Query；稠密 + 稀疏检索经 RRF 融合，Cross-Encoder 重排后将更少、更相关的 chunk 交给合成节点。
4. **Synthesizer + Critic**：合成 `Place` 与文字；Critic 用规则检查空结果/品类漂移等可验证问题，最多触发一次重检索，防止无限循环。
5. **Memory**：Working Memory 提取会话内偏好；Long-term Memory 用 pgvector 跨会话保存；checkpoint 保留会话历史。三者职责不同，避免把“记忆”泛化为单一聊天记录。
6. **MCP**：地点、游记、天气能力以标准 MCP Server 暴露；价值是工具接口标准化和可复用，不代表外部客户端天然可信。

必答取舍：HyDE 提升语义召回但有延迟/漂移风险；Multi-Query 增加覆盖但放大调用与检索成本；reranker 提升精度但通常是 CPU/GPU 重计算；Critic 增加质量保障但需设置次数上限；LoRA 路由降低部分 LLM 调用，却要监控分类错路由和模型加载延迟。

### 12. 项目深挖与系统设计（P0）

把下面四题作为项目延展题反复演练：

| 题目 | 用 BreezeTravel 回答的骨架 |
|---|---|
| 设计流式 AI 对话 | API 契约 → SSE 事件 → Agent 有界循环/超时 → 取消/重连 → Trace、指标、限流、降级 |
| 设计企业知识库 RAG | 文档解析/分块 → 权限元数据 → 混合检索/过滤/重排 → 引用与评估 → 增量索引、成本、抗提示注入 |
| 设计多人协同白板 | 房间与身份 → WebSocket → Yjs CRDT 合并 → 持久化/快照 → 权限、断连、水平扩展 |
| 设计旅行排线服务 | 输入约束/Place schema → 酒店分离与 K-Means 分天 → 距离缓存 → TSP → 时间槽、天气、失败兜底 |

## 4. 文件证据索引

| 主题 | 代码证据 |
|---|---|
| FastAPI、健康检查、生命周期 | `backend/app/main.py` |
| SSE 对话 API | `backend/app/api/chat.py` |
| 排线 API 与 Planner | `backend/app/api/optimize.py`、`backend/app/agents/planner/graph.py` |
| ReAct 图与状态 | `backend/app/agents/graph.py`、`backend/app/agents/state.py` |
| 路由、工具、合成、反思 | `backend/app/agents/nodes/router.py`、`tool_executor.py`、`synthesizer.py`、`critic.py` |
| RAG 的 HyDE / Multi-Query / 混合检索 | `backend/app/rag/hyde.py`、`multi_query.py`、`retriever.py` |
| 数据表与 pgvector | `backend/app/db/init.sql` |
| MCP | `backend/app/mcp_server.py` |
| 前端 SSE、Yjs 协同 | `frontend/src/hooks/useAIChat.ts`、`useYjsRoom.ts` |
| 指标、测试、编排 | `backend/app/metrics.py`、`backend/tests/`、`docker-compose.yml` |

## 5. 每个知识点的四段式回答模板

1. **问题**：在 BreezeTravel 中，什么用户行为或故障触发该技术需求？
2. **机制**：请求、状态或数据如何在组件间流动？用一个文件/接口作为证据。
3. **取舍**：为什么选它，不选一个看似合理的替代方案？成本是什么？
4. **可靠性**：它失败时的症状、监控指标、降级与测试是什么？

例：为什么对话使用 SSE、协同使用 WebSocket？——AI 回复是后端到前端的顺序单向流，SSE 协议简单、天然适配 HTTP；投票和备注是每个客户端都要实时写的双向状态，Yjs 通过 WebSocket 同步 CRDT 更新。代价是 SSE 要处理重连与慢客户端，WS 要处理连接、鉴权和扩容；两者分别用事件幂等与房间权限/断线合并保障可靠性。

## 6. 复习执行建议

- **P0 优先顺序**：Python/SSE → PostgreSQL/Redis → HTTP/安全 → 工程测试与交付 → LLM/RAG/Agent → 项目系统设计。
- **每个主题的产出**：一张链路图、一个最小代码/SQL/配置片段、一个失败模式、一个可核验文件路径、一个 3 分钟口述答案。
- **D7 项目映射抽检**：随机从大纲抽一个概念，限时画出它在本项目的入口、状态、下游、指标和降级；若讲不出“当前已实现”和“未来设计”的界线，回到对应代码核验。

## 7. 诚实表述清单

- 已有：FastAPI、SSE、Yjs/CRDT、LangGraph、混合 RAG、Memory、MCP、Planner 多节点、Redis 距离缓存、指标/追踪、测试和 Docker Compose。
- 设计可讲、但不应冒充已上线：完整身份认证/房间 RBAC、SSE 断点续传、全套限流、MQ 异步任务、Kubernetes 生产发布、跨区域容灾与生产 SLO。
- 不属于项目事实：Java/Spring/JVM 实战。可做知识迁移，不能当作该项目经历。
