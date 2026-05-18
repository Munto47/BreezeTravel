# BreezeTravel — AI 智能旅行协同规划系统

> 多人实时协同 × LangGraph ReAct Agent × Advanced RAG × Memory 系统 × LoRA 微调

一个面向 AI 应用开发岗位技术演示的旅行规划工具，完整覆盖当下主流 AI 工程技术栈：从 RAG 数据构建、混合检索、重排序，到 ReAct Agent 编排、跨会话 Memory 系统，再到 LoRA 微调全链路。

---

## AI 技术亮点

| 技术方向 | 实现内容 |
|----------|---------|
| **Advanced RAG** | 混合检索（BM25 + pgvector RRF 融合）+ Cross-Encoder Re-ranking + HyDE 查询扩展 + RAGAS 自动化评估 |
| **ReAct Agent** | LangGraph 有状态 DAG + LLM native tool calling + Think→Act→Observe 循环 + 反思/Critic 节点 |
| **Memory 系统** | Working Memory（会话内结构化偏好）+ Long-term Memory（跨会话 pgvector 持久化）+ 会话摘要压缩 |
| **LoRA 微调** | Qwen2.5-1.5B 意图分类微调：DeepSeek 合成训练数据 → LoRA 训练 → 评估对比 → 替换部署 |
| **实时协同** | Yjs CRDT 无锁同步，多标签页 500ms 内完成投票/备注/状态同步 |
| **路径优化** | K-Means 宏观聚类分天 + 高德真实驾车距离矩阵 + 最近邻 TSP 微观排序 |

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
        ├── tool=amap_search  → AmapSearch → Synthesizer
        ├── tool=rag_search   → RAGRetrieval → Synthesizer  ← 混合检索+重排序
        └── tool=both         → AmapSearch → RAGRetrieval → Synthesizer
                                                  └── [可选 Critic 反思，低质量触发重检索]
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

python -m pytest tests/ -v
python -m pytest tests/test_optimizer.py -v   # 排线算法
python -m pytest tests/test_rag.py -v         # RAG RAGAS 评估
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
