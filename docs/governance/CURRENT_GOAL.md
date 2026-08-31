# APPROVED GOAL：V0.5 三城有来源知识层

Goal ID: TC-VNEXT-G05-CITY-KNOWLEDGE
Status: APPROVED
Goal type: PRODUCT_ENHANCEMENT

<!-- PRODUCT_DELIVERY_CURRENT_GOAL_STATE
{
  "schema_version": "product-delivery-current-goal-state-v1",
  "program_id": "TC-VNEXT-2026",
  "goal_id": "TC-VNEXT-G05-CITY-KNOWLEDGE",
  "goal_status": "APPROVED",
  "gate_profile": "PRODUCT_DELIVERY_GATE",
  "required_gate": "Knowledge Admission Gate + PRODUCT_DELIVERY_PASS",
  "completion_status": "PENDING",
  "gate_result": "PRODUCT_DELIVERY_NOT_RUN",
  "goal_archived": false,
  "last_completed_goal_id": "TC-VNEXT-G04-SCREENSHOT",
  "next_goal_id": "TC-VNEXT-G06-MEMORY-SHARE",
  "next_activated": false,
  "h1_status": "NOT_RUN",
  "public_network_status": "NOT_RUN",
  "production_status": "NOT_RUN",
  "commercial_status": "NOT_RUN",
  "release_status": "NOT_REQUESTED",
  "deployment_status": "NOT_REQUESTED",
  "main_merge_status": "NOT_REQUESTED"
}
-->

## Metadata

- Goal ID：`TC-VNEXT-G05-CITY-KNOWLEDGE`
- Program ID：`TC-VNEXT-2026`
- Product version：`V0.5`
- Mainline phase：`PRODUCT_ENHANCEMENT`
- Gate profile：`PRODUCT_DELIVERY_GATE`
- Required gate：`Knowledge Admission Gate + PRODUCT_DELIVERY_PASS`
- Status：`APPROVED`
- Goal type：`PRODUCT_ENHANCEMENT`
- Governance transition baseline：`origin/develop@c5a6f847f6c904dfaa81a397c2af878b18f089a6`
- Activation branch / worktree：`codex/g04-g05-transition` / `D:/munto/code/claudeProject/agentTravel-g04-g05-transition`
- Canonical implementation branch / worktree：`codex/g05-city-knowledge` / `D:/munto/code/claudeProject/agentTravel-g05-integration`，在本治理过渡PR合并后从新的fresh `origin/develop`创建
- Upstream / remote readback：`origin/develop` / `c5a6f847f6c904dfaa81a397c2af878b18f089a6`，2026-08-31 fresh fetch与`ls-remote`一致
- Predecessor：G04 delivery `8542db708f8a1d450750d9050424088ce7756bf1`、PR #16 integration `c5a6f847f6c904dfaa81a397c2af878b18f089a6`、GitHub Actions `33357640834 PASS`
- Next Goal：`TC-VNEXT-G06-MEMORY-SHARE`

## Dependencies

- 唯一激活依赖是G04归档且Screenshot Parity Gate与`PRODUCT_DELIVERY_PASS`通过；该依赖已由正式Paddle回执、delivery tip、PR #16和`origin/develop@c5a6f847f6c904dfaa81a397c2af878b18f089a6`远端readback满足。
- 本治理过渡只归档G04并激活G05，不实现KnowledgeClaim、migration、抓取、检索、UI或Provider调用。
- 首个G05 preflight在过渡PR合并后填写exact implementation baseline，并逐来源readback许可、robots/API条款、保存权、撤回和更新；不清的来源lane标记`NOT_READY`或移出当前实验，不以模型常识替代，也不阻止已准入来源的独立切片。
- G04方案A历史兼容例外仍须在G07 exact-binding验收前移除，但不改变G04已交付产品字节，也不授权G05修改冻结Trip NLU资产。

## User Outcome

北京、上海、杭州用户获得有来源、会过期的典型游览时长、适合上午/下午/夜间、季节、预约和夜景建议；建议说明依据，绝不伪装成当前硬事实。

## Scope

- Provider/Data Admission；
- `KnowledgeClaim` schema、来源、条件和时效；
- 官方/政府/运营方来源；
- 获得授权的创作者或用户内容；
- 检索、引用、过期和撤回；
- 行程时段/时长建议；
- Knowledge on/off消融；
- 三城覆盖和缺口披露。

## Pre-approved actions

- 许可Gate通过后追加`032_knowledge_claims.sql`；
- 官方公开页面和明确允许的API；
- 授权创作者内容；
- PostgreSQL检索；pgvector只有消融证明必要且经依赖审查后另行批准；
- 不授权抓取小红书。

## Parallel work packages

| Package | Branch / worktree | Owned outcome | Initial state |
|---|---|---|---|
| `WP-G05-INTEGRATOR` | `codex/g05-city-knowledge` / `...-g05-integration` | governance、032、共享schema/repository/API、OpenAPI/client、首页装配、CI与最终E2E | `IN_PROGRESS`（仅在过渡PR合并后开始产品写入） |
| `WP-G05-SOURCE-ADMISSION` | `codex/g05-source-admission` / 独立worktree待绑定 | 来源准入、授权、导入、撤回与过期策略 | `PROVISIONING_HOLD` |
| `WP-G05-KNOWLEDGE-CLAIMS` | `codex/g05-knowledge-claims` / 独立worktree待绑定 | KnowledgeClaim领域、检索和版本化 | `PROVISIONING_HOLD` |
| `WP-G05-ADVICE-INTEGRATION` | `codex/g05-advice-integration` / 独立worktree待绑定 | 时长、时段、夜景、季节和预约建议 | `PROVISIONING_HOLD / WAITING_FOR_WRITER_SLOT` |

过渡PR合并后，主对话从新的fresh `origin/develop`建立实现分支，再创建三个用户可见独立任务并生成完整v1提示词，登记任务ID、prompt SHA-256、exact binding commit、branch/worktree、owned/forbidden paths和验收命令。最多同时运行两个贡献writer，第三包保持`WAITING_FOR_WRITER_SLOT`；任一前两包冻结后再启动第三包。子Agent只读复核或诊断，不得写产品代码或改Goal状态。全部贡献冻结后，集成者按来源领域/许可→032与内部API→建议UI/消融/E2E串行集成；贡献包不得改治理、migration、共享合同或自行合并，最多两轮修复复审。

## Decisions locked

- RAG只用于建议。
- POI、路线、营业和HARD Finding不由RAG决定。
- 每条claim必须有source/effective/expires/license。
- 过期或冲突claim不进入当前建议。
- 社交热度不是事实。
- 三城优先，不暗示全国知识覆盖。

## Non-goals

- 小红书或其他未授权抓取；
- GraphRAG；
- 全网实时舆情；
- 用模型常识补来源；
- 医疗、安全或客流保证；
- 在本治理过渡提交中实现G05产品代码或调用外部来源。

## Acceptance / Gate

完全继承Knowledge Admission Gate：

- required字段和来源100%；
- 未授权来源0；
- 过期claim使用0；
- RAG决定POI/路线/HARD为0；
- 同validation set消融达到：建议precision≥90%、unsupported=0、actionability提升≥5个百分点、P95回退≤20%；
- 页面依据可理解且不泄漏内部receipt；
- 删除/撤回来源可回读；
- `core_mainline_contract / g05_knowledge_targeted / g05_postgresql / frontend_build / g05_browser_e2e`全部PASS，并生成耐久`PRODUCT_DELIVERY_PASS`。

## Verification

- source/license/admission审计；
- claim schema/expiry/conflict；
- retrieval与ablation；
- user wording；
- PostgreSQL 032 fresh与既有数据库升级；
- three-city browser cases；
- privacy/copyright scan；
- 当前知识用户旅程定向测试与浏览器E2E；候选复审和blind留到G07；
- H1、公网、生产、商业：`NOT_RUN`。

## Authority

- `AGENTS.md`、Charter、Spec、v3 API、Architecture；Program、Roadmap、Release Gates、Product Delivery Gate、Product Mainline Execution Guide、Provider Admission、Risk Register；ADR-011、ADR-012、ADR-013、ADR-014。
- Program预批准在来源许可Gate通过后新增追加式`032_knowledge_claims.sql`；当前不授权任何其他migration、付费数据、pgvector、新依赖、账号、费用、公网或生产调用。

## Baseline

- 治理过渡branch/upstream：`codex/g04-g05-transition` / `origin/develop@c5a6f847f6c904dfaa81a397c2af878b18f089a6`；G05产品分支在过渡PR合并后从新的远端subject创建，并在首个checkpoint记录exact activation subject；
- G04 product candidate / delivery / integration：`525af072c47a3f318d88c722bf8067d6ff30907c` / `8542db708f8a1d450750d9050424088ce7756bf1` / `c5a6f847f6c904dfaa81a397c2af878b18f089a6`；远端CI与readback `PASS`；
- 旧RAG/Public corpus只作frozen baseline，不能自动进入KnowledgeClaim；三城来源必须逐项重新准入；
- H1、公网、生产、商业：`NOT_RUN`。

## Invariants

- `KnowledgeClaim`绑定canonical place、来源、条件、observed/effective/expires、许可和撤回；
- RAG只给建议，不决定POI、路线、营业硬事实或Finding；过期、冲突、未授权claim不进入当前建议；
- 不抓取小红书或绕过登录、robots、付费墙；用户内容不自动公共化或训练；
- public projection说明依据但不泄漏receipt、内部评分或来源抓取诊断；
- PostgreSQL是claim版本、准入、撤回、过期和引用的唯一事实源；缓存只保存可重建结果。

## Budget

- 每类来源先小样本admission，再按三城覆盖扩展；抓取频率、TTL、存储量和检索deadline在RunSpec冻结；
- 不预批准付费数据、pgvector、GraphRAG或新抓取基础设施；每切片checkpoint；
- 最多两个贡献writer并行，每包最多两轮修复复审；未准入来源不会阻断已准入来源的独立交付切片。

## HITL

新增或扩大来源、书面授权、付费/API账号、pgvector或新依赖、许可矛盾、未预批准migration、H1/商业/公网/生产/release/deploy/`main`需Owner批准。普通代码、测试、构建和已授权来源的许可readback由总指挥继续诊断。

## Checkpoint ledger

| 时间 | 用户结果 | Commit | Verification | Evidence level | Product progress | Governance ratio | Remaining | Risk/failure | Next autonomous action |
|---|---|---|---|---|---|---|---|---|---|
| 2026-08-31 | G04截图入口已并入`develop`并完整归档；G05三城有来源知识层合同原子激活，但尚未修改G05产品代码或访问外部来源 | transition commit待提交 | G04 delivery精确tip GitHub Actions `33357640834 PASS`；PR #16 `MERGED`；fresh `origin/develop@c5a6f847f6c904dfaa81a397c2af878b18f089a6` fetch/ls-remote一致；G04正式Paddle和产品交付回执均可回读 | `PRODUCT_DELIVERY_PASS / REMOTE_INTEGRATION_PASS / GOAL_TRANSITION` | `Product progress=NONE / GOAL_TRANSITION` | `Governance ratio=100% / atomic archive and activation only` | 合并本治理过渡PR；从新develop创建G05实现分支，完成来源准入后再创建贡献任务和产品切片 | 来源许可尚未逐项readback；G05产品、032、外部来源、PostgreSQL、浏览器与交付Gate均`NOT_RUN`；不得把G04方案A外推为普通全量pytest零失败 | 校验归档/绑定/范围，提交push并通过受保护PR；合并后fresh readback再建立唯一G05实现分支 |

## Auto-advance

- Required gate：`Knowledge Admission Gate + PRODUCT_DELIVERY_PASS`；Next template：`TC-VNEXT-G06-MEMORY-SHARE.md`；
- subject push/readback、耐久`PRODUCT_DELIVERY_PASS`、clean tree、无Stop后，最终归档，并原子更新Goal binding与work-package registry激活G06；不登记外部ledger、不创建authority generation；H1/公网/生产/商业不自动启动。

## Completion record

- Status：`APPROVED`；Subject commits / Remote branch：治理过渡为本commit（push/readback待执行）/ `origin/codex/g04-g05-transition`；G05实现分支尚未创建；
- Verification / Evidence / Gate result / `structurally_valid`：`NOT_RUN / GOAL_TRANSITION_ONLY / PRODUCT_DELIVERY_NOT_RUN / true`；结构有效不代表G05产品交付；
- H1 / production / commercial：`NOT_RUN / NOT_RUN / NOT_RUN`；公网、release、deploy和`main`同样未运行或未请求；
- User-visible result / Remaining risks / Goal archived / Next activated：`G04截图体验已交付，G05仅完成可追溯激活 / G05来源准入、产品代码、032、自动化、浏览器与交付回执全部未运行 / false / false`；
- Promotion decision：`NOT_REQUESTED`。

## Stop conditions

- 来源许可或保存权不清；
- 需要抓取未授权内容；
- `KnowledgeClaim`无法与`EvidenceFact`隔离；
- 需要新增向量依赖但消融未证明价值；
- 建议无法标明时效和条件；
- fresh `origin/develop`不再由G05合法接棒，或激活必须修改产品字节。
