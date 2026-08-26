# IN_PROGRESS GOAL：测试行程内容答案整理

## Metadata

- Goal ID：`TC-MAINT-G01-test-content-answers`
- Program ID：`TC-V1-INTERVIEW-2026`
- Phase：`POST-P6-DATA-CURATION`
- Status：`IN_PROGRESS`
- Branch：`codex/test-itinerary-content-answers`
- Baseline commit：`763f29796278e81b25193d108763cb2920f4ea1d`
- Approved by / at：User / 2026-08-26
- Predecessor gate：`Candidate Gate=PASS`；本 Goal 不晋级任何证据等级

## Outcome

将仓库根目录 `tests/` 中 19 个用户提供的文本按“城市+连续序号”机械改名，并在 `tests/答案/` 交付逐文件、结构化、内容独立识别且经另一子 Agent 复核的答案。

## Scope

- 只修改 `docs/governance/CURRENT_GOAL.md`、完成记录和根目录 `tests/`；
- 北京 7、上海 6、杭州 6 个 `.txt` 只改文件名，正文与 SHA-256 保持不变；
- 新增 19 个 `itinerary-content-answer-v1` JSON、`manifest.json` 和 `README.md`；
- 识别子 Agent 只接收匿名 case ID 和正文，独立审查子 Agent 不审查自己的首次结果；主 Agent 是唯一写入者。

## Non-goals

- 不修改产品代码、公共 API/schema、migration、依赖、正式评测集、frozen blind/oracle；
- 不核验景点事实、营业时间、交通可行性，不调用 Provider、外部 API 或付费模型 API；
- 不进入 H1，不称为真人 ground truth、正式 oracle、候选或发布证据；
- 不创建 PR，不合并 `main`，不部署，不发布。

## Authority

1. `AGENTS.md`；
2. `docs/product/PROJECT_CHARTER.md`；
3. `docs/product/TRIP_CHECK_SPEC.md`；
4. `docs/governance/PROGRAM.md`；
5. 本 Goal；
6. 用户于 2026-08-26 批准的实施计划。

## Contract versions

- Answer schema：`itinerary-content-answer-v1`，仅为本地数据资产合同；
- Dataset increment：19 个用户提供文本及 19 个自动识别答案；
- Evidence output path：`tests/答案/`；
- Review provenance：`automated_agent` + `independent_agent_reviewed`。

## Baseline

- 基线分支/commit：`codex/trip-check-p6-governance-archive` / `763f29796278e81b25193d108763cb2920f4ea1d`；
- dirty tree：仅 `?? tests/`，19 个未跟踪用户输入；
- 输入配额：北京 7、上海 6、杭州 6；
- backend/frontend/Provider/PostgreSQL/Candidate Gate：本 Goal 均为 `NOT_RUN`。

## Invariants

- 改名不改正文、长度或 SHA-256；序号只由原文件复制关系决定，不读取内容；
- 地点、天数、人数按正文识别，允许单人、范围和未知，不向产品 2～5 人边界强行收敛；
- 无 Day 结构的候选活动进入 `unassigned_events`，不得强造逐日安排；
- 精确时刻、时段、相对时间、时长和未知分开，禁止补造钟点；
- 备选、互斥、条件和整日替换不得扁平化成同一硬顺序；
- 图片/参考链接、住宿建议、交通说明、事实时间和必吃清单不得自动成为每日事件；
- 答案明确披露为自动识别与独立自动审查，不构成人类证据。

## Acceptance cases

- 19 个输入完整，命名严格为 `北京1..7.txt`、`上海1..6.txt`、`杭州1..6.txt`；
- 改名前后旧名/新名/长度/SHA-256 一一对应且内容哈希不变；
- `tests/答案/` 有 19 个同名 JSON、一个 manifest 和一个 README；
- 所有 JSON 可解析、必填字段完整、事件 ID 唯一、关系引用存在、证据行号有效；
- 每份答案审查状态只能为 `PASS` 或 `PASS_WITH_AMBIGUITIES`，`NEEDS_CORRECTION=0`；
- manifest、源文件和答案中的文件名、哈希、状态全部一致。

## Verification

- Targeted：输入数量/命名/哈希、JSON 解析与合同、事件关系、证据行号、审查状态、secret/privacy scan；
- Git：定向 diff、显式暂存、staged diff、`git diff --cached --check`、push/upstream readback；
- backend、frontend、Provider、PostgreSQL、G0～G6、Candidate Gate：`NOT_RUN`。

## Budget

- 外部 API/Provider 调用：0；
- 子 Agent：三路匿名识别，随后由新的独立子 Agent 复核；
- 最大重试：单份答案最多两轮修正，仍有不确定性时显式保留 ambiguity；
- diff 边界：Goal 激活、数据交付、Goal 归档三个 checkpoint。

## Pre-approved actions

- 新建并推送本开发分支；
- 机械重命名 19 个用户输入；
- 使用开发子 Agent 识别和独立复核；
- 写入 `tests/答案/`，运行离线结构/哈希检查；
- 按 checkpoint 显式暂存、commit 和 push。

## HITL

- 只有范围扩大、公共 schema/API、migration/依赖、Provider/付费、正式 oracle、证据晋级、H1/真人、公网、`main` 合并、release/deploy 才需新的人工批准。

## Auto-advance

- Required gate：`Test Content Answer Gate`；
- Next Goal template：无；
- 完成后仅归档本 Goal，不改变 P6 Candidate Gate 或 H1 状态。

## Stop conditions

- 任一原文件哈希变化、输入丢失、文件名与正文地点证据冲突且无法保守表示；
- 需要扩大到产品代码、正式评测、外部事实核验、Provider 或新基础设施；
- 子 Agent 输出不能追溯到正文证据，独立审查两轮后仍存在未修正错误；
- secret、个人信息、原始截图或 evidence 等级混淆进入交付物。

## Completion record

- Commits：`PENDING`
- Remote branch / upstream：`PENDING`
- Verification results：`PENDING`
- Evidence paths：`tests/答案/`
- Gate result：`NOT_RUN`
- Next Goal generated：`NO`
- Remaining red lights：`PENDING`
- Promotion decision：`NOT_REQUESTED`
