# 行程查 · BreezeTravel

把一段旅行攻略整理为可调整的逐日行程，核对真实地点与交通，并在修改前说明影响。

使用流程：匿名粘贴文字 → 逐日安排 → 确认地点 → 查看真实地图 → 发现问题 → 预览并采纳修改 → 更新路线 → 保存、恢复与撤销。

本仓是唯一产品主仓；TripCheck 为只读供体。北京、上海、杭州提供深度核验，其他国内城市提供基础整理；仅接受文本。实现与实际验证以当前状态为准，历史记录不作为新版结果。

在仓库根目录启动本地体验：

```powershell
.\scripts\experience.ps1 start
```

打开 [行程查](http://127.0.0.1:3106)。首页可以粘贴自己的攻略，也可以进入明确标识的固定示例。匿名行程保留 24 小时，登录保存后默认保留 30 天。建议可以先预览再采纳，修改后可以撤销；编辑后点击“更新路线”重新计算。

- [产品定义](docs/product/PROJECT_CHARTER.md)
- [实施计划与验收](docs/governance/IMPLEMENTATION_PLAN.md)
- [当前实现状态](docs/governance/CURRENT_GOAL.md)
- [开发约定](AGENTS.md)
- [环境准备、配置、停止与恢复](docs/EXPERIENCE_RUNTIME.md)

采用 Next.js / React、FastAPI / Pydantic、PostgreSQL、Redis 与已有后台 worker。自定义文本使用真实模型和地点服务；示例的行程、路线与住宿使用固定数据，高德底图仍需联网。新 API 入口为 `backend/app/experience_main.py`，本地数据和私有配置独立保存。历史 compose、旧房间及冻结功能不进入默认启动流程。

[旧指导原件](docs/governance/archive/pre-convergence-20260905/INDEX.md)及历史测试结果保留，不再作为当前交付门。
