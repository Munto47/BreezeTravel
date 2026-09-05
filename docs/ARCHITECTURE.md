# 技术边界入口

当前事实以 [产品定义](product/PROJECT_CHARTER.md)、[实施计划](governance/IMPLEMENTATION_PLAN.md)及[当前状态](governance/CURRENT_GOAL.md)为准。

模块化单体：Next.js / React → FastAPI / Pydantic v3 API → PostgreSQL；已有 worker 处理理解和地图，Redis 仅缓存、限流及可重建状态。模型提出语义，服务端核实地点、时间和路线，所有修改通过版本事务。

当前 v3 是固定流水线，并非旧多 Agent 图；RAG 与自然语言编辑为后续能力，不宣称已经进入核心链。旧房间、OCR 与候选治理不是重建默认运行依赖。历史 ADR 仅在与当前产品定义一致时可复用。

[旧架构原件](governance/archive/pre-convergence-20260905/docs/ARCHITECTURE.md)保留。
