# CURRENT GOAL：P0 指导文件与基线

## Metadata

- Goal ID：`TC-P0-G01`
- Phase：`P0`
- Status：`COMPLETED`
- Branch：`codex/trip-check-p0-guidance`
- Baseline commit：`85e1670ce0d85656be09311285bb9472af28517a`
- Approved by / at：用户在当前任务批准 / 2026-08-22

## Outcome

仓库只存在一套可执行的「行程查」产品目标、24 周顺序、证据门禁和单一 Goal 约束；旧双入口方案可追溯但不再驱动开发；当前代码、migration、测试与 evidence 状态被诚实记录。

## Scope

- 重写根 `AGENTS.md`、`README.md` 和文档索引；
- 定稿 Charter 和 Trip Check Spec；
- 新增 Roadmap、Release Gates、Goal 模板、本文件和基线报告；
- 归档 Final 2.0，新增单入口 ADR，重写能力状态；
- 仅为消除旧权威耦合而调整 release manifest 元数据及其测试。

## Non-goals

- 不实现 OCR、TripBrief、TripCheckRun、Brave、AdviceBundle 或 migration 022；
- 不修改公共 API/schema，不调用真实 Provider；
- 不开发 Builder，不扩城，不改 blind/oracle，不宣称任何新产品能力完成。

## Authority

`AGENTS.md`、`../product/PROJECT_CHARTER.md`、`../product/TRIP_CHECK_SPEC.md`、本文件、`ROADMAP.md`、`RELEASE_GATES.md`、ADR-004。

## Baseline

- 起点分支：`codex/repository-cleanup`；
- 起点 commit：`85e1670ce0d85656be09311285bb9472af28517a`；
- 起点工作树：clean；
- 远端：`origin` 已存在，Phase 0 分支已推送；
- migration 最新编号：021；
- 完整测试、PostgreSQL、snapshot、live Provider、公网与真人证据：必须以本 Goal 的基线报告为准，未跑不得写 PASS。

## Invariants

- 不删除或重写历史 migration、revision、evidence 或 Builder 代码；
- 旧文档移入 archive 并保留来源；
- 证据状态不因文档改写而晋级；
- release manifest 默认仍为 `REJECT/NOT_RUN`，不得借权威迁移生成发布批准。

## Verification

- Markdown 链接与旧权威引用审计；
- `git diff --check`、显式 staged diff 和敏感信息扫描；
- release manifest 定向单测；
- 后端离线测试/Ruff、前端 build、数据集 validator 能运行则记录原始结果；
- PostgreSQL、snapshot、真实 Provider、浏览器与真人层级保持 `NOT_RUN`，除非在同一 commit 实际重跑。

## Budget

- 外部 API/模型调用：0；
- migration：0；
- 新生产依赖：0；
- 每个小切片验证后 commit/push，最长 60 分钟远端 checkpoint。

## HITL

合并本分支、进入 P1、批准 migration 022/API schema、使用真实 Provider 均需用户批准。

## Stop conditions

发现文档迁移会破坏运行代码、需要修改公共 schema、需要扩展产品范围、证据互相矛盾或无法建立可复现基线时停止并报告。

## Completion record

- `e3511c8`：建立「行程查」产品权威、归档 Final 2.0；
- `4224f77`：建立 Roadmap、Release Gates 与 Goal 合同；
- `085f8d5`：README 对齐单入口与证据边界；
- `3d905d3`：release manifest 绑定新权威并保持 baseline-only/REJECT；
- Verification：见 `BASELINE_2026-08-22.md`；
- Remaining red lights：V1 实现、360 数据集、G1～G6 候选复跑、Judge 和真人内测均未开始；
- Promotion decision：`NOT_REQUESTED`。P1 必须由用户另行批准并建立新 Goal。
