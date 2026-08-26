# BreezeTravel「行程查」

BreezeTravel 当前只建设「行程查」：帮助 2～5 人核验北京、上海或杭州的 2～5 天单城市行程，发现地点、时间、交通、住宿、偏好、强度、天气和风险问题，并给出可执行调整或有真实依据的备选地点。

```text
文本/截图 → OCR/结构解析 → TripBrief 确认 → 地点消歧
→ 事实采集与核验 → 风险与 Advice → 采纳
→ 新 Revision → 完整 postcheck
```

## 当前状态

当前执行切片只以 [`CURRENT_GOAL.md`](docs/governance/CURRENT_GOAL.md) 为准，完整阶段定义见 [`PROGRAM.md`](docs/governance/PROGRAM.md)。现有代码已经包含文本导入、revision、EvidenceSnapshot、Audit、Repair/EditCommand、PostgreSQL 恢复等可复用底座，但不等于「行程查 V1」已经完成。

以下是冻结的历史技术资产，不再作为产品愿景或无条件完成声明：

- LangGraph/ReAct/Critic 与多 Agent Planner；
- BM25 + pgvector + reranker + HyDE 的 RAG；
- Qwen LoRA Router；
- Yjs 多人协同与路线 Builder；
- K-Means/TSP 路线优化、MCP 和旧评测/发布证据。

它们只保留最低回归；当前禁止扩城、跨城、新增 Agent、MQ、Kubernetes、GraphRAG、重新微调或主动建设拖拽 Builder。

## 权威文档

开发前依次读取：

1. [AGENTS.md](AGENTS.md)
2. [产品章程](docs/product/PROJECT_CHARTER.md)
3. [V1 行为规格](docs/product/TRIP_CHECK_SPEC.md)
4. [作品集使命](docs/governance/PORTFOLIO_MISSION.md)
5. [V1 Program](docs/governance/PROGRAM.md)
6. [当前唯一 Goal](docs/governance/CURRENT_GOAL.md) 与 [纵向 Roadmap](docs/governance/ROADMAP.md)
7. [Release Gates](docs/governance/RELEASE_GATES.md)
8. [能力与证据状态](docs/dual-entry/capability-status.md)

旧双入口 Final 2.0 已移入 [历史方案目录](docs/archive/plans/)，仅供追溯。

## 本地启动

准备环境文件：

```powershell
Copy-Item .env.example .env
Copy-Item frontend/.env.local.example frontend/.env.local
```

启动 PostgreSQL、Redis、y-websocket 和后端：

```powershell
docker compose up -d --build
docker compose logs -f backend
```

启动前端：

```powershell
cd frontend
npm install
npm run dev
```

本地入口：前端 `http://localhost:3000`，后端 OpenAPI `http://localhost:8000/docs`。

`DEMO_MODE=true` 和 `AMAP_MOCK=true` 只代表 fixture/演示路径，不证明真实 Provider、V1 Gate 或公开可用性。真实 API 调用必须在当前 Goal 明确授权后执行。

## 开发与验证

后端开发：

```powershell
cd backend
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload --port 8000
```

默认离线检查：

```powershell
cd backend
python -m pytest tests/ -q
python -m ruff check app tests scripts

cd ../frontend
npm run build
```

真实 PostgreSQL、固定 snapshot、真实高德/天气/Brave、浏览器恢复与性能、release manifest 分属不同 Gate，必须在同一 commit/config/dataset/model/receipt 上分别重跑。未运行即 `NOT_RUN`，不能由 unit/fixture 或历史结果推断通过。

## 目录

```text
backend/             FastAPI、领域模型、Provider、migration、测试与 evidence
frontend/            Next.js UI 与浏览器测试
y-websocket/         历史协同服务（冻结资产）
docs/product/        当前产品章程与 V1 规格
docs/governance/     Program、Roadmap、Goal 合同、Release Gates 与基线
docs/adr/            架构决策
docs/archive/        历史方案与 Review
```

## 能力声明边界

- 代码存在不等于能力完成；
- unit、integration、snapshot、live、public、human 是不同证据等级；
- 自动 Judge 只属于 `automated_proxy_judge`，不等于真人验证；
- 当前没有「行程查 V1 已发布」或「真人内测已通过」的声明。

## License

MIT
