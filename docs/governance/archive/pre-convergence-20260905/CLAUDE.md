# CLAUDE.md

本文件不复制项目说明，避免与仓库级指导漂移。

在本仓库工作前，依次读取：

1. `AGENTS.md`；
2. `docs/product/PROJECT_CHARTER.md`；
3. `docs/product/TRIP_CHECK_SPEC.md`；
4. `docs/product/TRIP_CHECK_API_CONTRACT.md`；
5. `docs/ARCHITECTURE.md` 与当前Goal引用的Accepted ADR；
6. `docs/governance/PORTFOLIO_MISSION.md` 与 `docs/governance/PROGRAM.md`；
7. `docs/governance/AGENT_GATE_PROTOCOL.md` 与 `docs/governance/PRODUCT_MAINLINE_EXECUTION_GUIDE.md`；
8. `docs/governance/CURRENT_GOAL.md`、`docs/governance/current_goal_binding.json`、`docs/governance/current_work_packages.json`、`docs/governance/WORK_PACKAGE_PROMPT_TEMPLATE.md`、`docs/governance/ROADMAP.md` 与 `docs/governance/RELEASE_GATES.md`；
9. 当前commit/config/dataset对应的evidence。

没有且仅有一个处于 `APPROVED`或`IN_PROGRESS`的`CURRENT_GOAL.md`，或本worktree的指导hash/Goal binding/工作包prompt、branch、独立worktree登记不一致时，只能做只读诊断和方案讨论。主对话是唯一集成者；长期功能必须使用用户可见独立功能对话，子Agent不得拥有产品分支或提交产品代码。历史方案、旧evidence和README不能扩大当前开发范围，也不能把Blueprint目标写成已实现能力。
