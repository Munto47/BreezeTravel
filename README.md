# BreezeTravel — AI 智能旅行协同规划系统

> 多人实时协同 × LangGraph ReAct Agent × Advanced RAG × Memory 系统 × LoRA 微调

一个面向 AI 应用开发岗位技术演示的旅行规划工具，完整覆盖当下主流 AI 工程技术栈：从 RAG 数据构建、混合检索、重排序，到 ReAct Agent 编排、跨会话 Memory 系统，再到 LoRA 微调全链路。

---

## AI 技术亮点

| 技术方向 | 实现内容 |
|----------|---------|
| **Advanced RAG** | 混合检索（BM25 + pgvector RRF 融合）+ Cross-Encoder Re-ranking + HyDE 查询扩展 + RAGAS 评估脚手架（Faithfulness/Relevancy/Recall 三项指标，入库后可一键执行） |
| **ReAct + Critic** | LangGraph 有状态图 + LLM native tool calling + Think→Act→Observe 循环 + **Critic 反思节点**（规则驱动质量检查，低质结果触发重检索） |
| **Memory 系统** | Working Memory（会话内规则提取，无 LLM 开销）+ Long-term Memory（跨会话 pgvector 持久化）|
| **MCP Server** | 三个核心工具（地点搜索/游记检索/天气查询）暴露为标准 **MCP Server**，可被 Claude Desktop / Cursor 直接调用 |
| **可观测性** | LangSmith 全链路追踪（节点耗时/Token 消耗/工具调用频次）+ /metrics 端点 + GitHub Actions CI |
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
              Critic（规则驱动质量反思）
                    ├── 质量不足 → 回到 Router 重试（最多 1 次）
                    └── 质量通过 → END
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

## 评估结果

### RAG Pipeline 评估（RAGAS，2026-05-21）

| 指标 | 分数 | 说明 |
|------|------|------|
| **Faithfulness** | **0.8942** | 回答忠实于检索上下文（不幻觉） |
| **Answer Relevancy** | **0.6624** | 回答与用户问题的相关度 |
| **Context Recall** | **0.4000** | 检索结果覆盖参考答案关键信息 |
| **综合平均** | **0.6522** | — |

> 配置：HyDE 查询扩展 + BM25+pgvector RRF 混合检索 + bge-reranker-v2-m3 精排  
> 评估 LLM：deepseek-chat，Embedding：BAAI/bge-m3（SiliconFlow）  
> 语料：80 篇合成游记，209 个 chunk，4 城市（成都/北京/上海/厦门），测试集 5 个旅行问题

### LoRA 微调评估（Qwen2.5-1.5B，训练中）

| 指标 | 数值 |
|------|------|
| 训练数据 | 1200 条（DeepSeek 数据蒸馏，4 类意图均衡分布） |
| 基础模型 | Qwen2.5-1.5B-Instruct |
| LoRA 配置 | r=16, alpha=32, fp16，RTX 4060 8GB |
| 训练时 token accuracy | epoch 1.6 → 94.8%（收敛中） |
| 准确率 / 延迟 | ⏳ 待训练完成后更新 |

---

## 快速开始（3 分钟跑起来）

### 方式一：Docker 一键启动（推荐）

```bash
git clone https://github.com/Munto47/AITravel.git
cd AITravel

cp .env.example .env
cp frontend/.env.local.example frontend/.env.local

# 启动所有后端服务（postgres + redis + y-websocket + backend）
docker-compose up -d --build

# 启动前端
cd frontend && npm install && npm run dev
```

打开 **http://localhost:3000** 即可使用。

> **零 API Key 体验**：保持 `AMAP_MOCK=true`（默认）+ `DEMO_MODE=true`，无需任何 Key 即可完整体验。

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
AITravel/
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
│   └── tests/
│       ├── fixtures/                 # 4 城市 Mock POI 数据
│       ├── test_api.py               # API 集成测试
│       ├── test_rag.py               # RAGAS 评估（Faithfulness/Relevancy/Recall）
│       └── test_optimizer.py         # 排线算法单元测试
├── frontend/
│   └── src/
│       ├── components/
│       │   ├── map/AMapContainer.tsx     # 地图（Marker 联动 + 多色路线）
│       │   ├── chat/                     # ChatPanel + ThinkingSteps（工具调用可视化）
│       │   └── places/                   # PlaceList + PlaceCard（投票/备注）
│       └── hooks/
│           ├── useYjsRoom.ts             # Yjs 协同核心
│           ├── useAIChat.ts              # SSE 流式解析
│           └── useOptimize.ts            # 排线请求
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
