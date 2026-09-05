# 行程查 · BreezeTravel

把一段旅行攻略整理为可调整的逐日行程，核对真实地点与交通，并在修改前说明影响。

目标流程：匿名粘贴文字 → 逐日安排 → 确认地点 → 查看真实地图 → 发现问题 → 预览并采纳修改 → 更新路线 → 保存、恢复与撤销。

本仓是唯一产品主仓；TripCheck 为只读供体。本次重建进行中，旧版完成记录不代表新体验已经交付。北京、上海、杭州提供深度核验，其他国内城市提供基础整理；仅接受文本。

- [产品定义](docs/product/PROJECT_CHARTER.md)
- [实施计划与验收](docs/governance/IMPLEMENTATION_PLAN.md)
- [当前实现状态](docs/governance/CURRENT_GOAL.md)
- [开发约定](AGENTS.md)

采用 Next.js / React、FastAPI / Pydantic、PostgreSQL、Redis 与已有后台 worker。自定义文本使用真实模型和地点服务；示例回放单独标识。统一启动说明随可运行新主线交付，当前请先查看状态，勿将历史 compose 或旧房间入口作为重建版运行方式。

[旧指导原件](docs/governance/archive/pre-convergence-20260905/INDEX.md)及历史测试结果保留，不再作为当前交付门。
