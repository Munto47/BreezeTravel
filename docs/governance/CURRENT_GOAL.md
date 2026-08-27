# IN_PROGRESS GOAL：Blueprint 1.0 长期产品与治理重构

Goal ID: TC-BP-G00-BLUEPRINT
Status: IN_PROGRESS
Goal type: GOVERNANCE_AND_ARCHITECTURE_ONLY

## Metadata

- Goal ID：`TC-BP-G00-BLUEPRINT`
- Program ID：`TC-VNEXT-2026`
- Product version：`Blueprint 1.0`
- Status：`IN_PROGRESS`
- Goal type：`GOVERNANCE_AND_ARCHITECTURE_ONLY`
- Branch：`codex/trip-check-product-reset`
- Baseline commit：`1c3adf3e61964501b670f68ca2fd1071918699d4`
- Approved by / at：User / 2026-08-27
- Required gate：`Blueprint Gate`
- Next Goal：`TC-VNEXT-G01-TEXT-CARDS`

## User Outcome

项目拥有一套彼此一致、可以直接驱动后续Codex长期开发的产品、架构、API、版本、风险、Provider、门禁和Goal合同；在此之前不修改产品代码。

## Scope

- 审计现有资产并标记`KEEP / ADAPT / FREEZE / REMOVE_FROM_ENTRY / ARCHIVE`；
- 重写AGENTS、产品章程、产品规格、目标API和Architecture；
- 重写Program、Roadmap、Release Gates和Goal模板；
- 新增语义/用户投影、地图快照、模型中立、住宿、知识/隐私和Goal治理ADR；
- 建立风险登记与Provider准入表；
- 创建G01～G07预定义Goal；
- 归档旧Current Goal；
- 完成产品、架构、反方和商业独立审查。

## Non-goals

- 不修改backend、frontend、miniapp或测试代码；
- 不添加或执行migration；
- 不添加依赖或API实现；
- 不调用Qwen、高德、天气或其他真实Provider；
- 不读取/修改sealed blind；
- 不部署、不进入H1、不合并`main`；
- 不删除旧API、旧房间数据或历史证据。

## Authority

- `AGENTS.md`；
- `docs/product/PROJECT_CHARTER.md`；
- `docs/product/TRIP_CHECK_SPEC.md`；
- `docs/product/TRIP_CHECK_API_CONTRACT.md`；
- `docs/ARCHITECTURE.md`；
- `docs/governance/PROGRAM.md`；
- `docs/governance/RELEASE_GATES.md`；
- `docs/governance/BLUEPRINT_VALIDATION.md`与`BLUEPRINT_REVIEW_RESOLUTION.md`；
- ADR-007～ADR-012。

## Baseline

- 根工作区`develop`落后远端且包含用户未跟踪`miniapp/`与`tests/`；本Goal在隔离worktree执行，不接触这些文件。
- 远端基线：`origin/develop@1c3adf3`。
- 当前首页仍以登录、城市/天数和房间创建为入口。
- 当前文本materialization仍可能把整句、URL或描述作为地点。
- 当前workspace暴露内部术语、长ID和原文证据。
- 当前地图一套是坐标虚线，旧房间地图默认驾车。
- 当前Program/Current Goal只描述已结束的Intake优化，没有长期产品路线。
- 历史Intake与Candidate证据保持原状态；不得因此宣称 `V1_CANDIDATE_READY` 或新版ready。

## Decisions locked

- 普通用户不看到原文映射、置信度、内部ID、模型、Provider和后端流程。
- 卡片首次READY后后台准备地图；卡片编辑不自动重算，用户手动rerender。
- 地图只比较walking/transit，差值≤10分钟优先步行。
- 无酒店时综合各过夜日第一/最后站，按2/4/8km扩大区域，最多展示3家连锁酒店；用户选择后整程同店。
- Qwen为主模型族，DeepSeek为冻结Baseline，业务使用模型中立接口。
- G00只改文档；G01之后才实现v3 API和migration。
- 北京/上海/杭州深核验，其他城市基础整理。
- Program按G00→G07固定推进，H1及以后人工批准。

## Invariants

- 用户计划不得被旧实现限制反向改写。
- 目标合同必须明确`NOT_IMPLEMENTED`，不得把蓝图写成现有能力。
- 历史证据、旧Goal和旧ADR保留并标注取代关系。
- 产品代码、migration、依赖锁文件diff必须为0。
- 只允许一个active Goal。
- 文档不得授权生产、真人、公网、付费账号、抓取或`main`合并。
- 兼容治理测试需要出现`structurally_valid=true`；只有实际验证通过后才能在Completion record标为PASS。

## Acceptance

- 权威文档不存在强制前置确认、房间优先、默认驾车、卡片展示原文等目标要求；
- G01～G07均有预定义Outcome、Scope、Non-goals、Gate、授权和Stop conditions；
- 风险登记覆盖错误映射、内部泄漏、地图stale、酒店误导、模型漂移、Provider许可、隐私和证据漂移；
- Provider表区分开发、生产、留存和待批准状态；
- API和migration均按Goal预批准且未实现；
- 历史Goal已归档；
- 独立复核高优先级意见已处理；
- 文档/链接/现有治理测试通过；
- `git diff`只包含文档；
- checkpoint已push并远端readback。

## Verification

- 权威文件与链接检查；
- 禁止旧目标文案扫描；
- v3目标状态和版本引用检查；
- 现有release manifest治理测试；
- 相关文档合同测试；
- `git diff --name-only`产品代码命中0；
- branch/upstream/remote readback；
- Backend全套、Frontend build、PostgreSQL、Provider、Browser、H1：`NOT_RUN`，因G00不修改产品代码。

## Budget

- 模型/Provider调用：0；
- 增量费用：0；
- 新账号/绑卡：禁止；
- 每个文档切片完成diff审查和checkpoint；
- 同一审查问题最多重复两次，之后改变文档结构或验证方式。

## Pre-approved actions

- 在隔离worktree修改文档；
- 新增ADR、风险/Provider表和planned Goal合同；
- 运行只读检查与现有文档/治理测试；
- 明确暂存文档、commit和push `codex/trip-check-product-reset`；
- G00通过后归档本文件并激活G01。

## HITL

只有需要偏离已批准蓝图、改变Program顺序、扩大G00到产品代码、触发新费用/账号、修改sealed blind、H1/公网/部署/`main`或删除数据时请求用户。

## Checkpoint ledger

| 时间 | 用户结果 | Commit | Verification | Evidence level | Remaining | Risk/failure | Next autonomous action |
|---|---|---|---|---|---|---|---|
| 2026-08-27 | 已保护根工作区，并从最新`origin/develop`建立隔离分支 | pending | Git/worktree/branch审计 | G00 in progress | 权威文档、ADR、Goal、验证 | 无用户阻塞 | 重写Blueprint 1.0合同 |
| 2026-08-27 | Blueprint权威、G01～G07完整合同、风险/Provider表和旧资产边界已形成；三路复审P0/P1为0 | subject checkpoint（本commit，hash由归档记录） | planned字段/单active/历史归档/链接/职责漂移PASS；历史兼容pytest 12 PASS；docs-only | BLUEPRINT_CANDIDATE | subject push/readback、最终归档、原子激活G01 | 产品代码与外部能力仍NOT_IMPLEMENTED/NOT_RUN | 提交并push subject，远端回读后执行治理transition |

## Auto-advance

- Required gate：`Blueprint Gate`；
- Next Goal template：`docs/governance/goals/planned/TC-VNEXT-G01-TEXT-CARDS.md`；
- 只有Outcome、Gate、clean tree、push、readback和归档全部通过才激活；
- 不自动实现G01代码，不自动进入H1、公网、生产或`main`。

## Stop conditions

- 需要改变用户批准的产品方向；
- 需要修改产品代码、migration或依赖才能让蓝图成立；
- 权威文件出现不可消解冲突；
- 需要新账号、费用、外部数据或Provider调用；
- 需要修改sealed blind/oracle；
- 独立审查发现高优先级安全/隐私问题且无法在文档范围修正。

## Completion record

- Status：`PENDING`；
- Subject commits：`PENDING`；
- Remote branch：`PENDING`；
- Verification：`PENDING`；
- Evidence level：`BLUEPRINT_ONLY`；
- Gate result：`PENDING`；
- `structurally_valid=true`：`PENDING_VALIDATION`；
- Product code changes：0（待最终diff验证）；
- Next Goal activated：`NO`；
- Promotion decision：`NOT_REQUESTED`。
