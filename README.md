# BreezeTravel — AI 智能旅行协同规划系统

> 多人实时协同 × 智能体编排 × 可引用 RAG × 自动路径规划

BreezeTravel 是一款帮助小团体共同规划出游的产品：多人在同一房间实时投票、备注、调整地点，AI 助手基于游记知识库和实时 POI 数据给出推荐，并自动按天聚类排好路线、挂载酒店、注入贴心提示，让出行规划从"群里发链接互相吵"变成"一个画板上即时看见结果"。

---

## 核心特性

| 技术方向 | 实现内容 |
|----------|---------|
| **Advanced RAG** | 混合检索（BM25 + pgvector RRF 融合）+ Cross-Encoder Re-ranking + HyDE 查询扩展 + RAGAS 评估脚手架（Faithfulness/Relevancy/Recall 三项指标，入库后可一键执行） |
| **可解释引用** | 检索结果将来源、摘录、许可边界与分数通过 SSE 推送到前端；公开资料和演示语料明确区分 |
| **ReAct + Critic** | LangGraph 有状态图 + LLM native tool calling + Think→Act→Observe 循环 + **Critic 反思节点**（规则驱动质量检查，低质结果触发重检索） |
| **任务完成验证** | TripTaskSpec 约束契约 + SATISFIED/VIOLATED/UNKNOWN 三态 Verifier + 最多两轮定向修复 + 快照失效 UI |
| **Memory 系统** | Working Memory + 带来源/置信度/TTL/纠错/删除/退出机制的 Long-term Memory |
| **安全协同** | HTTP 房间成员授权 + Yjs 房间绑定 JWT + Prompt Injection 信号 + 日志脱敏 |
| **可靠运行时** | 请求 deadline/取消 + 工具预算/重试/熔断/并发隔离 + Redis 原子限流 + 双实例 Checkpoint |
| **MCP Server** | 三个核心工具（地点搜索/游记检索/天气查询）暴露为标准 **MCP Server**，可被 Claude Desktop / Cursor 直接调用 |
| **可观测性** | LangSmith 全链路追踪（节点耗时/Token 消耗/工具调用频次）+ /metrics 端点 + 本地显式验证脚本 |
| **LoRA 微调** | Qwen2.5-1.5B 意图分类：DeepSeek 数据蒸馏 1500 条 + SFTTrainer LoRA 训练脚本 + 准确率回归测试脚本（待 GPU 环境执行训练） |
| **实时协同** | Yjs CRDT 无锁同步，多标签页 500ms 内完成投票/备注/状态同步 |
| **路径优化** | K-Means 宏观聚类分天 + 高德真实驾车距离矩阵（Redis 缓存 TTL 24h）+ 最近邻 TSP 微观排序 |

---

## 系统架构

```
浏览器
  ├── Next.js 15 (App Router) + Tailwind CSS + Framer Motion
  ├── Zustand (本地 UI 状态) + Yjs + y-websocket (多人协同 CRDT)
  └── 高德地图 JS SDK 2.0 (地图渲染 + Driving 路线可视化)
       │
       ├── HTTP/SSE ──→ FastAPI 后端 (:8000)
       │                    ├── LangGraph ReAct Agent
       │                    │     Router (Tool Calling) → AmapSearch / RAGRetrieval → Synthesizer
       │                    ├── Advanced RAG Pipeline
       │                    │     HyDE扩展 → 混合检索(BM25+pgvector) → bge-reranker精排
       │                    ├── Memory Layer
       │                    │     Working Memory + Long-term Preferences (pgvector)
       │                    │     PostgreSQL Checkpointing (会话历史)
       │                    └── scikit-learn KMeans + TSP (排线算法)
       │
       ├── WebSocket ──→ y-websocket (:1234)  Yjs 实时协同
       │
       └── ──→ PostgreSQL 16 + pgvector (:5432)  向量数据库
                Redis 7 (:6379)  距离矩阵缓存 TTL 24h
```

### LangGraph 工作流

```
用户消息
  └── Router（ReAct：Think → Tool Select → Act → Observe）
        └── tool_executor（并发执行工具调用，最多循环 3 次）
              ├── search_places       → 高德 POI 搜索
              ├── search_travel_notes → RAG 混合检索 + 重排序
              └── get_weather         → 和风天气 API
                    ↓（无 tool_calls 或达到上限）
              Synthesizer（DeepSeek 合并数据，生成推荐）
                    ↓
              Critic（推荐质量反思）
                    ↓
              Planner（聚类→距离→排序→排期→Tips）
                    ↓
              Verifier（三态规则报告）
                    ├── VIOLATED → 定向修复（最多 2 轮）→ 重新验证
                    └── SATISFIED / UNKNOWN → 返回报告和快照哈希
```

### RAG Pipeline

```
Query ──→ HyDE（DeepSeek 生成假设文档） ──→ Embedding
                  ↓                              ↓
         BM25 (tsvector)              pgvector 余弦检索
                  └──────── RRF 融合 (top-20) ──────┘
                                    ↓
                    bge-reranker-v2-m3 精排（本地推理）
                                    ↓
                              top-5 → Synthesizer
```

---

## 评测与证据

所有公开数字必须能回读到版本化结果，不能把演示或合成语料的结果写成生产效果。

- 运行中可读取的脱敏摘要：`GET /api/evidence/latest`。
- 受控本地固定评测：Router 96、任务解析 72、Verifier 120、端到端 60，共 **348/348**；原始 case、split、hash 和置信区间位于 `backend/evidence/local_eval/`。
- 故障注入 **24/24**；双实例验证覆盖两个独立进程、跨实例 Checkpoint 和 Redis 3/6 原子限流，均有 JSON 报告可回读。
- 当前提交的是**历史基线，等待以公开资料重跑**：Router 固定离线集 50 条准确率 0.88，`both` 类 0.60；历史 RAG 27 条 Context Recall 0.6944；排线 50 次 P95 2221ms。
- 指标范围、原始文件、已知缺口和重跑门槛见 [评测说明](docs/EVALUATION.md) 与 [证据报告](docs/EVIDENCE.md)。架构、安全、可靠性和复现边界分别见 `docs/ARCHITECTURE.md`、`docs/SECURITY.md`、`docs/RELIABILITY.md`、`docs/REPRODUCE.md`。
- 深度优化方案与面试复习资料统一收在 `docs/`，避免在仓库根目录混放设计文档和运行产物。

这意味着：现有数字可作为改进基线，**不应在简历中宣称为公开真实语料或公网环境结果**。完成公开资料盲测重跑后再替换此段。

---

## 快速开始（3 分钟跑起来）

### 方式一：Docker 一键启动（推荐）

```bash
git clone https://github.com/Munto47/BreezeTravel.git breezetravel
cd breezetravel

cp .env.example .env
cp frontend/.env.local.example frontend/.env.local

# 启动所有后端服务（postgres + redis + y-websocket + backend）
docker-compose up -d --build

# 启动前端
cd frontend && npm install && npm run dev
```

打开 **http://localhost:3000** 即可使用。

> **零 API Key 体验**：保持 `AMAP_MOCK=true`（默认）+ `DEMO_MODE=true`，无需任何 Key 即可完整体验。

`breezetravel.cn` 当前只能确认前端页面可访问，认证 API 与 evidence 接口尚未通过公网验收；因此不作为“已上线 Demo”宣称。完整发布条件与 smoke 流程见 [Demo 运行手册](docs/DEMO_RUNBOOK.md)；性能或质量数字仅以最新 evidence run 为准。

---

### 方式二：配置真实 API（完整功能）

**后端 `.env`：**

```env
# 主 LLM：DeepSeek API
DEEPSEEK_API_KEY=sk-...

# 备用 LLM（OpenAI 兼容接口）
OPENAI_API_KEY=sk-...
OPENAI_API_URL=https://api.openai.com/v1

# 高德地图后端 REST API Key（Web 服务类型）
AMAP_API_KEY=your-rest-key
AMAP_MOCK=false

# 和风天气（可选）
QWEATHER_KEY=your-qweather-key

DEMO_MODE=false
```

**前端 `frontend/.env.local`：**

```env
NEXT_PUBLIC_AMAP_JS_KEY=your-js-key
NEXT_PUBLIC_AMAP_SECURITY_CODE=your-code
```

---

## 项目结构

```
breezetravel/
├── backend/
│   ├── app/
│   │   ├── agents/
│   │   │   ├── graph.py              # LangGraph 主图（ReAct + PostgresSaver）
│   │   │   ├── state.py              # AgentState（messages/intent/memory/places）
│   │   │   └── nodes/
│   │   │       ├── router.py         # ReAct Agent：LLM tool calling 意图路由
│   │   │       ├── amap_search.py    # 高德 POI 搜索（Mock + 真实双模式）
│   │   │       ├── rag_retrieval.py  # 混合检索 + Re-ranking + HyDE
│   │   │       ├── synthesizer.py    # DeepSeek 合成 Place 列表 + 自然语言回复
│   │   │       └── optimizer.py      # K-Means + TSP 排线（独立于主图）
│   │   ├── memory/                   # Memory 系统（Working/Long-term/Summary）
│   │   ├── tools/                    # LangChain @tool 工具集（POI/天气/搜索）
│   │   ├── api/                      # FastAPI 路由
│   │   ├── db/                       # asyncpg 连接池 + init.sql
│   │   └── schemas/                  # Pydantic 数据模型（Place/Itinerary/API）
│   ├── scripts/
│   │   ├── ingest_notes.py           # RAG 游记入库（合成数据 + Entity Linking + 分块）
│   │   └── finetune/                 # LoRA 微调脚本（数据生成 + 训练 + 评估）
│   ├── eval_data/                     # 固定 pilot/dev/blind 评测集与清单
│   ├── evals/                         # 统一评测、故障注入和实验框架
│   ├── evidence/                      # 可回读的本地验证与发布证据
│   └── tests/
│       ├── fixtures/                 # 4 城市 Mock POI 数据
│       ├── test_api.py               # API 集成测试
│       ├── test_rag.py               # RAGAS 评估（Faithfulness/Relevancy/Recall）
│       └── test_optimizer.py         # 排线算法单元测试
├── frontend/
│   ├── e2e/                           # Playwright 本地受控与公网 smoke
│   └── src/
│       ├── components/
│       │   ├── map/AMapContainer.tsx     # 地图（Marker 联动 + 多色路线）
│       │   ├── chat/                     # ChatPanel + ThinkingSteps（工具调用可视化）
│       │   └── places/                   # PlaceList + PlaceCard（投票/备注）
│       └── hooks/
│           ├── useYjsRoom.ts             # Yjs 协同核心
│           ├── useAIChat.ts              # SSE 流式解析
│           └── useOptimize.ts            # 排线请求
├── y-websocket/                       # 带房间 JWT 绑定的独立协同服务
├── docs/                              # 架构、评测、安全、复现与复习资料
├── verify-local.ps1                   # 本地全量验证入口
├── docker-compose.yml
└── .env.example
```

---

## 运行测试

```bash
cd backend
pip install -r requirements.txt

# 纯单元测试（无需 API Key / 数据库）
python -m pytest tests/test_optimizer.py tests/test_mock_data.py tests/test_router_ft_unit.py -v

# RAG 单元测试（离线）
python -m pytest tests/test_rag.py -v -k "not evaluate_rag_pipeline"

# RAGAS 集成评估（需要 API Key + 已入库游记数据）
python -m scripts.ingest_notes                                    # 先入库
python -m pytest tests/test_rag.py::evaluate_rag_pipeline -v -s  # 再评估
```

## MCP 接入（Claude Desktop / Cursor）

```bash
# 启动 MCP Server（默认端口 8001）
cd backend && python -m app.mcp_server

# 在 claude_desktop_config.json 中添加：
# {
#   "mcpServers": {
#     "breezetravel": { "url": "http://localhost:8001/mcp" }
#   }
# }
```

---

## 云端部署

### Railway（全栈一键）

1. Fork 本仓库
2. Railway 会自动识别 `docker-compose.yml` 并部署全部服务
3. 在 Railway 环境变量中填写 `DEEPSEEK_API_KEY`、`AMAP_API_KEY` 等

### Vercel（前端）+ Railway（后端）分离

前端 Vercel Root Directory 设为 `frontend`，配置环境变量：

```
NEXT_PUBLIC_API_URL=https://your-backend.railway.app
NEXT_PUBLIC_Y_WEBSOCKET_URL=wss://your-ws.railway.app
NEXT_PUBLIC_AMAP_JS_KEY=...
```

---

## 常见问题

**Q: 无需任何 API Key 能跑起来吗？**  
A: 可以。`AMAP_MOCK=true` + `DEMO_MODE=true` 可完整体验所有功能。

**Q: AI 对话无响应**  
A: 检查 `.env` 中 `DEEPSEEK_API_KEY` 是否有效；或将 `DEMO_MODE=true` 切换演示模式。

**Q: 地图空白不显示**  
A: 确认 `frontend/.env.local` 中 `NEXT_PUBLIC_AMAP_JS_KEY` 已填写，并在高德控制台将当前域名加入白名单。

**Q: RAG 游记数据为空**  
A: 运行入库脚本：`cd backend && python -m scripts.ingest_notes`

**Q: 多人协同如何测试**  
A: 同一浏览器开两个标签页，或不同浏览器打开相同 URL，输入相同房间号即可。

---

## License

MIT
