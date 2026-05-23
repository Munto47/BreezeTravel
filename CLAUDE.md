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

---

## 岗位优化 Sprint（2026-05 面试准备）

### Phase 1 — RAG 质量提升 ✅
- [x] 城市数据扩容：4 城市 → **7 城市**（新增广州 / 深圳 / 杭州）
- [x] 游记数量扩容：每城 20 篇 → **50 篇**，总量 80 → **347 篇 / 2075 chunk**（10× 增幅）
- [x] Persona 扩展：5 种 → 10 种，生成 Prompt 加入城市特色地标引导词
- [x] RAGAS 评估集：5 条 → **21 条**（7 城市 × 3 意图类型：景点/美食/交通住宿避坑）
- [x] HyDE 查询路由：短查询（<12字）或精确地标名直接跳过 HyDE，减少语义漂移
- [x] 修复 `rag_retrieval._extract_city` bug：优先使用 `state.trip_city`，避免无城市名查询回退错误城市

入库命令（从项目根执行）：
```bash
$env:PYTHONPATH="backend"
python backend/scripts/ingest_notes.py
```

### Phase 2 — Agent 评测体系 ✅
- [x] `backend/scripts/eval_agent.py`：50 条 eval 数据集（7 城市 × 4 意图）
  - 离线模式：`python backend/scripts/eval_agent.py --mode offline`（FT Router 准确率，无需 API）
  - 集成模式：`python backend/scripts/eval_agent.py --mode full --n 10`（完整 pipeline，需 API+DB）
- [x] `backend/tests/test_agent_eval.py`：26 个离线单元测试，全部通过
  - 评估集结构验证（城市/意图覆盖度）
  - 工具选择判定逻辑（amap/rag/both/weather 边界）
  - Synthesizer 输出有效性判定
- [x] `/metrics` 端点升级：新增 Agent 级实时指标
  - `agent_success_rate`：有效地点输出率
  - `critic_trigger_rate`：Critic 反思触发率
  - `avg_react_iterations`：平均 ReAct 循环次数
  - `tool_call_distribution`：各工具调用次数分布
- [x] `backend/app/metrics.py`：独立指标模块（避免 main↔chat 循环导入）

**FT Router 离线评估结果**（50 条 eval 集）：
```
整体准确率：88.0%（目标 ≥70%）✅
  amap    : 20/20 = 100.0%
  rag     : 13/15 = 86.7%
  both    :  6/10 = 60.0%  ← 主要弱项，both 类边界模糊
  weather :  5/5  = 100.0%
```
评估结果存储：`backend/results/agent_eval_offline.json`

### Phase 3 — 压测与性能基准 ✅
- [x] `backend/scripts/load_test.py`：Locust 压测脚本 + `--quick` 独立基准模式
  - `ChatUser`：SSE 流式对话压测（DEMO_MODE=true，无 LLM 费用）
  - `OptimizeUser`：K-Means+TSP 排线算法压测
  - `HealthUser`：/health /metrics 基线对照

**实测延迟数据**（Docker localhost，keep-alive 连接，n=30）：
| 接口 | P50 | P95 | P99 |
|------|-----|-----|-----|
| `/health` | 1ms | 2ms | 2ms |
| `/metrics` | 1ms | 2ms | 2ms |
| `/api/optimize` | 1753ms | 2342ms | 3933ms |

完整并发压测：
```bash
pip install locust
locust -f backend/scripts/load_test.py --headless -u 50 -r 10 \
       --run-time 60s --host http://localhost:8000 \
       --csv backend/results/load_test
```

### Phase 5 — Demo 体验 + 可观测性 ✅
- [x] Mock fixture 补全：广州 / 深圳 / 杭州 三城市地点数据（共 34 条 Place 记录）
  - 文件：`backend/tests/fixtures/amap_mock_places.json`
  - Demo 模式下 7 城市全覆盖，不再回退成都数据
- [x] Demo 响应文案动态化（`synthesizer._build_demo_response`）：
  - 按品类分布（景点/美食/住宿）组织推荐亮点
  - 读取 `working_context` 偏好（旅行风格/预算）生成个性化提示
- [x] 前端 Memory 活跃徽章（`ChatPanel.tsx`）：
  - `useMemoryActive()` Hook 从 thinkingSteps 检测"历史偏好"信号
  - 检测到长期偏好加载时，标题栏右侧显示紫色「🧠 记住你了」标签
  - AnimatePresence 动画渐入，前端编译零错误

## RAGAS 评估结果（2026-05，LLM-Judge 方式）

### 评估配置
- 数据集：21 条（7 城市 × 3 意图类型）
- 游记语料：**347 篇 / 2075 chunk / 7 城市**
- 检索配置：HyDE + BM25+pgvector RRF
- 评估 LLM：DeepSeek deepseek-chat（n=1，自定义 Judge Prompt，规避 RAGAS n=3 与 DeepSeek 的兼容问题）

### 指标结果
| 指标 | 分数 | 目标 | 状态 |
|------|------|------|------|
| Faithfulness | **0.8762** | ≥ 0.75 | ✅ |
| Answer Relevancy | **0.9119** | ≥ 0.75 | ✅ |
| Context Recall | **0.4119** | ≥ 0.65 | ❌ |
| 综合平均 | **0.7333** | — | — |

### Context Recall 按意图类型
| 意图 | Recall | 说明 |
|------|--------|------|
| transport | 0.55 | 交通路线信息覆盖尚可 |
| scenic | 0.53 | 景点信息覆盖尚可 |
| food | 0.36 | 游记提餐厅但品牌名表述不精确 |
| tips | 0.33 | 避坑经验较分散 |
| hotel | 0.22 | 游记较少记录具体酒店名 |

Context Recall 偏低的根本原因：评估 ground_truth 包含极具体的品牌名/路线/价格（如"巴奴毛肚火锅"、"地铁2号线到犀浦换乘城际约40分钟"），而游记以口语化主观体验为主，精确词汇命中率自然偏低。Faithfulness 和 Answer Relevancy 均已超标，说明 RAG **生成质量优秀**，召回多样性是下一步改善重点。

评估脚本：`backend/scripts/run_ragas_eval.py`（自定义 LLM-Judge，兼容 DeepSeek）
评估结果：`backend/results/ragas_eval.{json,txt}`

### 待完成（下一步）
- [ ] Phase 4：PlannerAgent 多智能体升级（Optimizer → 独立 LangGraph 子图，A2A 调度）
- [ ] Phase 6：生产部署（Railway backend + Vercel frontend + Supabase PostgreSQL）
- [ ] Context Recall 改善：增加 hotel/tips 类型游记密度，改进 ground_truth 评估集设计
