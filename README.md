# BreezeTravel「行程查」

BreezeTravel 当前只建设「行程查」：用户粘贴攻略或上传截图，系统先生成高准确率逐日卡片并后台准备路线，再用最多三个用户友好的重点问题帮助用户把计划变得可执行。北京、上海、杭州提供深度核验；其他国内城市先提供基础整理并明确边界。

```text
文本/截图 → 语义行程与原子地点 → 用户卡片
→ 后台步行/公交地图 → 住宿补全 → Top-3 核验
→ 最小修改 → 新版本 → 手动地图更新与完整复检
```

## 当前状态

当前执行切片只以 [`CURRENT_GOAL.md`](docs/governance/CURRENT_GOAL.md) 为准，完整阶段定义见 [`PROGRAM.md`](docs/governance/PROGRAM.md)。G01～G06已经归档；G07候选收口仍标记为`IN_PROGRESS`，但2026-09-02第三轮final panel已触发停止条件，候选未通过，不得自行开启第四轮修复或sealed blind。远端停止点是`codex/g07-candidate@71b8513d4dcdc61e585e1bee6c02ce004a6ee0ac`；跨电脑续工说明见[工作区交接检查点](docs/governance/WORKSPACE_HANDOFF_2026-09-02.md)。H1、公网、生产、商业、发布、部署和`main`合并仍为`NOT_RUN`或未请求。

`origin/develop`是唯一集成基线；当前分支收口、已合并资产和冻结分支见[分支整合记录](docs/governance/BRANCH_CONSOLIDATION.md)。新开发不得从历史实验分支继续生长。

以下是冻结的历史技术资产，不再作为产品愿景或无条件完成声明：

- LangGraph/ReAct/Critic 与多 Agent Planner；
- BM25 + pgvector + reranker + HyDE 的 RAG；
- Qwen LoRA Router；
- Yjs 多人协同与路线 Builder；
- K-Means/TSP 路线优化、MCP 和旧评测/发布证据。

它们只保留最低回归；当前禁止新增运行时Agent、MQ、Kubernetes、GraphRAG、重新微调或主动建设拖拽Builder。跨城输入可以忠实整理，但不能宣称已完成跨城深核验。

## 权威文档

开发前依次读取：

1. [AGENTS.md](AGENTS.md)
2. [产品章程](docs/product/PROJECT_CHARTER.md)
3. [目标行为规格](docs/product/TRIP_CHECK_SPEC.md) 与 [v3 API目标合同](docs/product/TRIP_CHECK_API_CONTRACT.md)
4. [目标架构](docs/ARCHITECTURE.md) 与已接受ADR
5. [作品集使命](docs/governance/PORTFOLIO_MISSION.md)
6. [Blueprint Program](docs/governance/PROGRAM.md)
7. [Agent Gate协议](docs/governance/AGENT_GATE_PROTOCOL.md)、[产品主线执行指南](docs/governance/PRODUCT_MAINLINE_EXECUTION_GUIDE.md)
8. [当前唯一Goal](docs/governance/CURRENT_GOAL.md)、[并行工作包清单](docs/governance/current_work_packages.json)、[独立功能对话提示词模板](docs/governance/WORK_PACKAGE_PROMPT_TEMPLATE.md)、[Roadmap](docs/governance/ROADMAP.md) 与 [Release Gates](docs/governance/RELEASE_GATES.md)
9. 当前commit/config/dataset对应的证据；[历史能力状态](docs/dual-entry/capability-status.md)不能晋级新版

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
miniapp/             已纳入Git的Taro小程序与自动化测试（当前为可复用客户端资产）
packages/            共享trip-check-client合同与生成类型
y-websocket/         历史协同服务（冻结资产）
docs/product/        当前产品章程、目标规格与v3 API合同
docs/governance/     Program、Roadmap、Goal 合同、Release Gates 与基线
docs/adr/            架构决策
docs/archive/        历史方案与 Review
```

根目录旧`tests/`旅行文本草稿已按项目所有者要求删除并禁止重新提交；自动化测试仍位于`backend/tests/`、前端测试目录和`miniapp/__test__/`。

## 能力声明边界

- 代码存在不等于能力完成；
- unit、integration、snapshot、live、public、human 是不同证据等级；
- 自动 Judge 只属于 `automated_proxy_judge`，不等于真人验证；
- 当前没有「V0.1已实现」「V0.9候选已通过」「真人内测已通过」或「已发布」的声明。

## License

MIT
