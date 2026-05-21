# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

**BreezeTravel** — AI 智能旅行协同规划系统，面向 AI 应用开发岗位的技术演示项目。

核心技术展示（已完成 + 升级中）：
- **LangGraph 多 Agent 编排**：ReAct 循环 + Critic 反思节点 + Tool 并发执行
- **Advanced RAG**：混合检索（BM25 + pgvector RRF）+ Cross-Encoder Re-ranking + HyDE 查询扩展
- **Memory 系统**：Working Memory（会话内规则提取）+ Long-term Memory（pgvector 持久化）
- **MCP Server**：三个核心工具暴露为标准 MCP Server，可被 Claude Desktop 直接调用
- **可观测性**：LangSmith 全链路追踪 + /health /metrics 端点 + GitHub Actions CI
- **微调**：Qwen2.5-1.5B LoRA 意图分类（DeepSeek 数据蒸馏，训练脚本就绪待执行）
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
python -m pytest tests/test_optimizer.py -v          # 排线算法（无需外部服务）
python -m pytest tests/test_rag.py -v                # RAG 单元测试（离线）
python -m pytest tests/test_rag.py::evaluate_rag_pipeline -v -s  # RAGAS 集成评估（需 API Key）
python -m pytest tests/test_router_ft_unit.py -v     # LoRA 分类器单元测试
```

### MCP Server 启动
```bash
cd backend
python -m app.mcp_server        # 启动 MCP Server（端口 8001）
# 或随 docker-compose 一起启动：docker-compose up -d
```

### LangSmith 追踪（可选）
```bash
# .env 中填写 LANGSMITH_API_KEY 后，所有 Agent 调用自动上报
# 控制台：https://smith.langchain.com/projects/BreezeTravel
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
        ├── tool=amap_search       → tool_executor → (循环，最多 3 轮)
        ├── tool=rag_search        → tool_executor → (循环，最多 3 轮)
        └── tool=both/weather      → tool_executor → (循环，最多 3 轮)
                                          ↓ (无 tool_calls 或达到上限)
                                      Synthesizer
                                          ↓
                                      Critic（质量反思）
                                          ├── 质量不足 → 回到 Router 重试（最多 1 次）
                                          └── 质量通过 → END
```

**各节点职责：**
- **Router** (`nodes/router.py`): ReAct Agent，LLM tool calling 选择工具，意图分类 + 查询改写
- **tool_executor** (`nodes/tool_executor.py`): 并行执行工具调用，累积 amap_places + rag_chunks
- **Synthesizer** (`nodes/synthesizer.py`): DeepSeek 合并数据，生成 Place 列表和回复文本
- **Critic** (`nodes/critic.py`): 规则驱动的质量反思节点；结果不足或品类漂移时触发重检索（最多 1 次）
- **Optimizer** (`nodes/optimizer.py`): **独立节点**，通过 `POST /api/optimize` 触发；K-Means + TSP 生成 Itinerary
- **AmapSearch / RAGRetrieval**: 保留供独立测试，主图由 tool_executor 内部调用

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

评估脚手架：RAGAS 框架（Faithfulness + Answer Relevancy + Context Recall），运行 `python -m scripts.ingest_notes` 入库后可执行：
```bash
python -m pytest tests/test_rag.py::evaluate_rag_pipeline -v -s
```

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

### Sprint 1（RAG 升级）— ✅ 已完成
- [x] R1：混合检索（BM25 + pgvector + RRF）
- [x] R2：Re-ranking（bge-reranker-v2-m3 本地推理）
- [x] R3：HyDE 查询扩展
- [x] R6：RAGAS 自动化评估框架

### Sprint 2（Agent + Memory）— ✅ 已完成
- [x] T1：标准化 Tool 层（LangChain @tool 装饰器）
- [x] A1：Router ReAct 模式升级
- [x] M1：Working Memory（AgentState 结构化偏好）
- [x] M2：Long-term Memory（跨会话用户偏好）

### Sprint 5（工程升级）— ✅ 已完成
- [x] C1：Critic 反思节点（规则驱动质量检查，低质结果触发重检索）
- [x] O1：LangSmith 全链路追踪（Agent 调用链路 + Token 消耗可视化）
- [x] M1：MCP Server（三工具标准 MCP 接口，支持 Claude Desktop 调用）
- [x] E1：/health + /metrics 监控端点
- [x] CI：GitHub Actions 自动化测试流水线

### Sprint 3（微调）— ✅ 已完成
- [x] F1：Qwen2.5-1.5B LoRA 微调 Router 分类器（DeepSeek 数据蒸馏 + 4060 本地训练）
  - ✅ 训练数据：1200 条（DeepSeek 数据蒸馏，4 类意图均衡）
  - ✅ 训练完成：RTX 4060 8GB，30 分钟，3 epochs，最终 loss 0.178
  - ✅ 分类准确率：**91.0%**（目标 ≥80% ✅），各意图均 >84%
  - ✅ 推理延迟：~1964ms（CUDA），FT_ROUTER_ENABLED=true 已启用
  - 评估结果：`backend/results/lora_{eval,scores}.{txt,json}`
  - `scripts/generate_training_data.py` — DeepSeek 数据蒸馏（1500 条，4 类意图）
  - `scripts/train_router.py` — SFTTrainer + LoRA（r=16, fp16，8GB VRAM）
  - `app/agents/nodes/router_classifier.py` — 本地推理 fast path（含降级）
  - `tests/test_router_ft.py` — 准确率 ≥ 80% + 延迟评估 + DeepSeek 基准对比
  - 环境变量：`FT_ROUTER_ENABLED=true`，`FT_ROUTER_MODEL_PATH=models/router_lora`

**训练流程：**
```bash
# Step 1：生成训练数据（需要 DEEPSEEK_API_KEY）
cd backend
python -m scripts.generate_training_data --samples 1500

# Step 2：安装微调依赖（需要 CUDA PyTorch）
pip install -r requirements_finetune.txt

# Step 3：LoRA 微调（RTX 4060，约 30-60 分钟）
python -m scripts.train_router --epochs 3

# Step 4：评估
python -m pytest tests/test_router_ft.py -v
# 或完整报告
python -m tests.test_router_ft --compare-deepseek

# Step 5：启用
echo "FT_ROUTER_ENABLED=true" >> .env
echo "FT_ROUTER_MODEL_PATH=backend/models/router_lora" >> .env
```

### Sprint 4（工程收尾）— ✅ 已完成
- [x] X1：真实 SSE 流式（graph.astream_events() v2）
  - `app/api/chat.py` 切换到 `astream_events(version="v2")`
  - `on_chain_start` 实时推送节点启动 thinking 事件
  - `on_chain_end` 提取节点输出，推送地点卡片 + 批量文字（12 字/帧）
  - 区分首轮 Router vs. ReAct 循环第 N 轮，避免重复事件
- [x] 前端 ThinkingSteps：新增 `tool_executor` 节点（🛠️ 工具执行）
- [x] 文字推送优化：逐字 → 批量（12 字/帧），减少 SSE 帧数约 12×
