# Goal Contract 模板

> `Status` 只能是 `DRAFT / APPROVED / IN_PROGRESS / BLOCKED / COMPLETED / REJECTED`。只有一个 Goal 可为 `APPROVED` 或 `IN_PROGRESS`。

## Metadata

- Goal ID：
- Program ID：
- Phase：
- Status：
- Branch：`codex/trip-check-p<n>-<scope>`
- Baseline commit：
- Approved by / at：
- Predecessor gate：

## Outcome

本切片结束后，用户能完成什么可观察行为。

## Scope

- 允许修改的模块：
- 允许新增的文件/依赖：
- 唯一纵向链路：

## Non-goals

明确本切片不做的功能、城市、Provider、数据、基础设施和重构。

## Authority

引用适用的 Charter、Spec、Roadmap、Gate、ADR 和 schema/API 合同。

## Contract versions

- API/schema version：
- Dataset increment：
- Fault profiles：
- Evidence output path：

## Baseline

- 分支/commit/dirty tree：
- 已通过测试：
- 已知失败或 `NOT_RUN`：
- 当前 evidence 等级：

## Invariants

列出不能破坏的 revision、UNKNOWN、evidence、privacy、idempotency、compatibility 边界。

## Acceptance cases

列出可执行输入、预期输出和失败行为，不以“增加测试”代替。

## Verification

- Targeted：
- G1/G2/G3/G4/G5：
- Readback / raw artifacts：
- 必须保持 `NOT_RUN` 的层级：

## Budget

- 时间窗口：
- 外部 API/模型预算：
- 最大重试：
- diff/切片边界：

## Pre-approved actions

列出 Program 已明确批准、无需逐文件重复请求的 migration、依赖、Provider 模式和开发分支动作。
同时列出无人值守开发动作：synthetic/dev/regression 数据与截图的子代理生成/独立复核、本地依赖服务自动启停、定向/完整 Gate、故障诊断和 checkpoint commit/push。

## HITL

只列出受保护边界：新增或付费 Provider、新账号/绑卡、扩大外部数据范围、未预批准 schema/migration/依赖、修改 frozen blind/oracle、证据晋级、H1/真人/公网、合并 `main`、release/deploy。不要把普通开发测试或既有零增量费用 Provider 矩阵写成人工阻塞。

## Auto-advance

- Required gate：
- Next Goal template：
- 自动推进必须同时满足：验收全 PASS、clean tree、commit 已推送、evidence 可回读、无 Stop condition。

## Stop conditions

至少包含：连续两个切片无改善、范围扩大、新基础设施、blind/oracle 修改、证据矛盾、成本超限。

## Completion record

- Commits：
- Remote branch / upstream：
- Verification results：
- Evidence paths：
- Gate result：
- Next Goal generated：
- Remaining red lights：
- Promotion decision：`NOT_REQUESTED / REJECT / APPROVE_NEXT_PHASE`
