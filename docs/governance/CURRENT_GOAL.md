# 当前状态

更新：2026-09-05。目标 `TC-EXPERIENCE-V1`，状态 `IN_PROGRESS`。唯一产品定义为 [产品定义](../product/PROJECT_CHARTER.md)，顺序与验收为 [实施计划](IMPLEMENTATION_PLAN.md)。

## 保护与方向

- BreezeTravel 为唯一产品主仓；TripCheck 为只读供体。
- 原 BreezeTravel 提交 `6f1337300fd62950857786eee0ca41a8b196a832`，原 TripCheck 提交 `01bc0ad65cbb1dc4f8fb4db2cabb6ff9211e3681` 保留。
- 新工作树 `D:/CODEX/BreezeTravel-experience-v1`，分支 `codex/experience-v1`；供体交接工作树 `D:/CODEX/TripCheck-donor-handoff`。
- 原 BreezeTravel 未提交 AGENTS 修改未触碰，并备份到 `D:/CODEX/rebuild-checkpoints/20260905`。
- 旧目标、工作包与签名／blind／候选限制均为 `SUPERSEDED`；原历史结果不改写，不转为通过。原指导见[归档](archive/pre-convergence-20260905/INDEX.md)。

## 已完成与当前切片

阶段 0 已完成：指导入口、三份事实文档、新机器合同、定向 CI 与供体说明一致。供体交接已保存为本地提交 `c1b5807`。此状态不代表产品已完成。

现有 v3 持久化、账号、版本、删除、后台地图可复用；真实匿名入口、地点候选闭环、结构化时间、完整修改预览、撤销和新真实地图界面尚未完成。

## 实际验证

当前完整体验版：`NOT_RUN`。真实模型与地图三城闭环：`NOT_RUN`。12 条主流程：`NOT_RUN`。四尺寸浏览器验收：`NOT_RUN`。真人与生产：`NOT_RUN`。

历史单测或回放只证明对应旧实现，不计入当前交付。阶段 0 两个合同 CLI 均 PASS（仅 CONTRACT_ONLY）；新合同单测 26 通过。选定旧产品回归 172 通过、2 失败，失败已在原工作树复现，涉及秦岭归日／重复和张家界重复；未改写历史结果。新工作流 YAML 与归档字节校验通过。PostgreSQL 尚未启动，数据库验证 NOT_RUN。

## 下一自主动作

保存方向切换本地 checkpoint，立即接通匿名真实输入并精简启动链；对齐 v3 时间、候选、撤销与地图类型，前后端按独立文件并行实施完整体验。无需项目所有者诊断或重复授权。
