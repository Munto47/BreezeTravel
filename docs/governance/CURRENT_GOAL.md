# CURRENT GOAL：P0-G02 纵向 Program 指导与合同重构

## Metadata

- Goal ID：`TC-P0-G02`
- Program ID：`TC-V1-INTERVIEW-2026`
- Phase：`P0`
- Status：`IN_PROGRESS`
- Branch：`codex/trip-check-p0-vertical-program`
- Baseline commit：`d0f786744f0f011c2e8bbca7bdf8ffc5037f7558`
- Approved by / at：用户批准完整实施方案 / 2026-08-22
- Predecessor gate：`TC-P0-G01 COMPLETED`

## Outcome

仓库形成一套可直接驱动 P1～P6 和 H1 的面试优先 Program：第 6 周前交付文本纵向闭环，pilot 从首个切片开始，架构/API/数据/Gate/自动推进均无需实现者临场决定。

## Scope

- 重写仓库指导、架构、产品规格、Roadmap、Release Gates 和 Goal 模板；
- 新增 Portfolio Mission、Program、模块化单体 ADR 和固定 API 合同；
- 归档已完成 Goal 和漂移的 Evaluation/Evidence/Reliability 说明；
- 更新 README、文档索引与能力状态口径；
- 仅修改文档，不实现 API、schema、migration、依赖或运行代码。

## Non-goals

- 不实现 TripBriefRevision、TripCheckRun、OCR、Advice 或 OR-Tools；
- 不执行 migration、真实 Provider、公网部署或真人测试；
- 不改变三城、2～5 人、2～5 天单城市范围；
- 不晋级任何 evidence。

## Authority

`AGENTS.md`、`../product/PROJECT_CHARTER.md`、`../product/TRIP_CHECK_SPEC.md`、`PORTFOLIO_MISSION.md`、`PROGRAM.md`、本文件及本 Goal 新增的 ADR/API 合同。

## Baseline

- 工作树开始时 clean；
- 当前分支从 `d0f7867` 创建；
- P0 基线：1227 passed、25 skipped、Ruff PASS、frontend build PASS；
- V1 G2～G6、live Provider、公网与真人证据仍为 `NOT_RUN`。

## Invariants

- 旧 migration、revision、evidence 和运行代码不修改；
- `UNKNOWN/UNAVAILABLE`、revision、postcheck、CandidateSet、隐私和证据等级边界不降低；
- 历史资料可追溯但不重新成为权威；
- 文档改写不能让 release manifest 晋级。

## Acceptance cases

- 权威文件不存在互相冲突的主链、阶段顺序或完成状态；
- API 合同固定路径、前置条件、错误码和兼容边界；
- Roadmap 在第 6 周前产生文本闭环，18 pilot 从 P1 开始；
- Gate 定义 D1、可靠性、Solver、Evaluation、Candidate 和 H1；
- 当前 Goal 完成后能按 Program 生成 P1 Goal。

## Verification

- Markdown 链接与旧权威引用审计；
- `git diff --check`、敏感信息扫描、显式 staged diff；
- 后端离线测试/Ruff、前端 build；
- PostgreSQL、snapshot、live Provider、浏览器、manifest 与真人层级保持 `NOT_RUN`。

## Budget

- 外部 API/模型：0；
- migration/生产依赖：0；
- 每个文档切片验证后 commit/push，最长 60 分钟形成 checkpoint。

## Pre-approved actions

- Program 内文档新增、归档与权威迁移；
- Gate 通过后创建 `TC-P1-G01-text-vertical-slice`。

## HITL

本 Goal 无额外 HITL。P1 的 migration/API 实现受 `PROGRAM.md` 预批准约束；公网、live paid Provider 和真人仍需现场批准。

## Stop conditions

发现文档合同与现有不可变领域主干无法兼容、需要修改运行代码/历史 migration、证据矛盾或必须降低 Gate 时停止。

## Auto-advance

完成并远端保存后，归档本 Goal，生成 P1 Goal；不得自动合并 `main`。

## Completion record

- Commits：待填写；
- Remote branch：待填写；
- Verification results：待填写；
- Evidence paths：待填写；
- Next Goal：`TC-P1-G01-text-vertical-slice`；
- Promotion decision：`NOT_REQUESTED`。
