# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

**BreezeTravel** — AI 智能旅行协同规划系统，面向 AI 应用开发岗位的技术演示项目。

核心技术展示（已完成 + 升级中）：
- **LangGraph 多 Agent 编排**：ReAct 模式 Router → Tool Use → RAG/AmapSearch → Synthesizer
- **Advanced RAG**：混合检索（BM25 + pgvector） + Cross-Encoder Re-ranking + HyDE 查询扩展
- **Memory 系统**：短期工作记忆 + 长期用户偏好（pgvector 持久化） + 会话摘要压缩
- **微调**：Qwen2.5-1.5B LoRA 意图分类微调（DeepSeek 合成训练数据）
- **Yjs CRDT 实时协同**：多人 500ms 内同步
- **K-Means + TSP 混合排线**：K-Means 宏观聚类 + 最近邻 TSP 微观排线

主 LLM：**DeepSeek API**（deepseek-chat / deepseek-reasoner）；OpenAI 接口保留作备用。

## 常用命令

### 一键启动（推荐）
```bash
docker-compose up -d          # 启动所有服务（postgres + redis + y-websocket + backend）
docker-compose down           # 停止
docker-compose logs -f backend  # 查看后端日志
```

### 后端独立开发（热重载）
```bash
cd backend
pip install -r requirements.txt
python -m uvicorn app.main:app --reload --port 8000
```

### 前端独立开发
```bash
cd frontend
npm install
npm run dev      # http://localhost:3000
npm run build
npm run lint
```

### 数据库操作
```bash
docker-compose exec postgres psql -U postgres -d travel_agent
```

### RAG 数据入库
```bash
cd backend
python -m scripts.ingest_notes    # 生成合成游记并入库 pgvector
```

### 运行测试
```bash
cd backend
python -m pytest tests/ -v
python -m pytest tests/test_optimizer.py -v   # 排线算法
python -m pytest tests/test_rag.py -v         # RAG 检索评估（Sprint 1 新增）
```

## 环境变量配置

复制 `.env.example` 为 `.env`，关键字段：
- `DEEPSEEK_API_KEY` — 主 LLM（deepseek-chat / deepseek-reasoner）
- `OPENAI_API_KEY` / `OPENAI_API_URL` — 备用 LLM 接口（兼容 OpenAI 格式）
- `AMAP_API_KEY` — 后端高德 REST API Key（Web 服务类型）
- `AMAP_JS_KEY` — 前端高德 JS SDK Key（Web 端 JS API 类型，两个 key 不同）
- `AMAP_MOCK=true` — 默认开启，使用本地 fixture 数据保护 API 配额
- `DEMO_MODE=true` — 跳过所有 LLM 调用，返回预设数据（面试演示用）

## 架构说明

### 服务端口
- `3000` — Next.js 前端
- `8000` — FastAPI 后端（`/docs` 查看 Swagger）
- `1234` — y-websocket 实时协同服务
- `5432` — PostgreSQL + pgvector
- `6379` — Redis（API 缓存 + 距离矩阵）

### LangGraph 工作流（后端核心）

`backend/app/agents/` 下的主图流向：

```
用户消息
  └── Router（ReAct 模式：Think → Tool Select → Act → Observe）
        ├── tool=amap_search  → AmapSearch → Synthesizer
        ├── tool=rag_search   → RAGRetrieval → Synthesizer
        ├── tool=both         → AmapSearch → RAGRetrieval → Synthesizer
        └── tool=web_search   → WebSearch → Synthesizer
                                       └── [Critic 反思节点，可触发重检索]
```

**各节点职责：**
- **Router** (`nodes/router.py`): ReAct Agent，LLM tool calling 选择工具，意图分类 + 查询改写
- **AmapSearch** (`nodes/amap_search.py`): 高德 POI 搜索；`AMAP_MOCK=true` 时从 `tests/fixtures/amap_mock_places.json` 读取
- **RAGRetrieval** (`nodes/rag_retrieval.py`): 混合检索（BM25 + pgvector RRF 融合）+ bge-reranker 精排 + HyDE 扩展
- **Synthesizer** (`nodes/synthesizer.py`): DeepSeek 合并数据，生成 Place 列表和回复文本
- **Optimizer** (`nodes/optimizer.py`): **独立节点**，通过 `POST /api/optimize` 触发；K-Means + TSP 生成 Itinerary

`/api/chat` 通过 SSE 流式推送 `thinking → place → text → done` 事件（`graph.astream_events()` 真实流式）。

### Memory 系统（Sprint 2 新增）

三层 Memory 架构：
1. **Working Memory**：`AgentState.working_context`，当前会话内结构化偏好追踪
2. **Long-term Memory**：`user_preferences` 表（pgvector 存储），跨会话用户偏好持久化
3. **Session History**：LangGraph PostgreSQL Checkpointing，对话历史 + 摘要压缩

### RAG 系统（Sprint 1 升级）

```
查询 → HyDE 扩展（DeepSeek 生成假设文档）
     → 双路检索：pgvector 余弦相似度 + PostgreSQL tsvector BM25
     → RRF 融合（top-20）
     → bge-reranker-v2-m3 重排序（本地推理）
     → top-5 传入 Synthesizer
```

评估：RAGAS 框架（Faithfulness + Answer Relevancy + Context Recall）

### 全局数据模型（"货币"）

`backend/app/schemas/place.py` 的 `Place` 对象贯穿所有节点和前端状态树，`place_id` 使用高德 POI ID 作为全局唯一标识。

### 前端状态管理

Yjs YDoc 三个共享结构（`frontend/src/hooks/useYjsRoom.ts`）：
- `doc.getMap('room')` — 房间元数据（roomId, threadId, tripCity, tripDays）
- `doc.getMap('places')` — 共享地点列表（含 votedBy, note）
- `doc.getArray('chat')` — 只追加的消息记录

Zustand store (`frontend/src/stores/`) 管理本地 UI 状态，Yjs 负责多人同步状态。

### API 接口

- `POST /api/chat` — SSE 流式响应，payload: `{messages, room_id, thread_id}`
- `POST /api/optimize` — 同步 JSON 响应，payload: `{places, trip_days, thread_id}`
- `POST /api/room` — 创建房间
- `GET /api/room/{id}/state` — 获取房间元数据

## Sprint 进度

### Sprint 1（RAG 升级）— 进行中
- [ ] R1：混合检索（BM25 + pgvector + RRF）
- [ ] R2：Re-ranking（bge-reranker-v2-m3 本地推理）
- [ ] R3：HyDE 查询扩展
- [ ] R6：RAGAS 自动化评估框架

### Sprint 2（Agent + Memory）— 待开始
- [ ] T1：标准化 Tool 层（LangChain @tool 装饰器）
- [ ] A1：Router ReAct 模式升级
- [ ] M1：Working Memory（AgentState 结构化偏好）
- [ ] M2：Long-term Memory（跨会话用户偏好）

### Sprint 3（微调）— 待开始
- [ ] F1：Qwen2.5-1.5B LoRA 微调 Router 分类器（DeepSeek 数据蒸馏 + 4060 本地训练）

### Sprint 4（工程收尾）— 待开始
- [ ] X1：真实 SSE 流式（graph.astream_events()）
- [ ] 前后端打磨 + Demo 录制
