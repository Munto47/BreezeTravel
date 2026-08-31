# COMPLETED GOAL：V0.5 三城有来源知识层

Goal ID: TC-VNEXT-G05-CITY-KNOWLEDGE
Status: COMPLETED
Goal type: PRODUCT_ENHANCEMENT

<!-- PRODUCT_DELIVERY_CURRENT_GOAL_STATE
{
  "schema_version": "product-delivery-current-goal-state-v1",
  "program_id": "TC-VNEXT-2026",
  "goal_id": "TC-VNEXT-G05-CITY-KNOWLEDGE",
  "goal_status": "COMPLETED",
  "gate_profile": "PRODUCT_DELIVERY_GATE",
  "required_gate": "Knowledge Admission Gate + PRODUCT_DELIVERY_PASS",
  "completion_status": "DELIVERY_INTEGRATED",
  "gate_result": "PRODUCT_DELIVERY_PASS",
  "goal_archived": true,
  "last_completed_goal_id": "TC-VNEXT-G05-CITY-KNOWLEDGE",
  "next_goal_id": "TC-VNEXT-G06-MEMORY-SHARE",
  "next_activated": true,
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
- Status：`COMPLETED`
- Goal type：`PRODUCT_ENHANCEMENT`
- Governance transition baseline：`origin/develop@c5a6f847f6c904dfaa81a397c2af878b18f089a6`
- Activation branch / worktree：`codex/g04-g05-transition` / `D:/munto/code/claudeProject/agentTravel-g04-g05-transition`
- Canonical implementation branch / worktree：`codex/g05-city-knowledge` / `D:/munto/code/claudeProject/agentTravel-g05-integration`
- Exact implementation baseline / upstream：`origin/develop@d1fa4905807a7361ad2d4a5524a9e389312c0a74` / `origin/develop`；2026-08-31再次fresh fetch、`rev-parse`与`ls-remote`三方一致
- Predecessor：G04 delivery `8542db708f8a1d450750d9050424088ce7756bf1`、PR #16 integration `c5a6f847f6c904dfaa81a397c2af878b18f089a6`；G04→G05 transition `ec7cdc73f99b30d24b29f29a3fca70c65e75fea7`经PR #17合入`develop@d1fa4905807a7361ad2d4a5524a9e389312c0a74`，exact-tip CI `33359977383 PASS`
- Integration / remote readback：PR #18经merge commit `c416dcdc40fcef2aef56627ab28c6f4049dc7dd9`合入`develop`；GitHub Actions `33389970986 PASS`；fresh fetch、`rev-parse origin/develop`与`ls-remote`三方一致
- Next Goal：`TC-VNEXT-G06-MEMORY-SHARE`，已由后续治理过渡原子激活

## Dependencies

- 唯一激活依赖是G04归档且Screenshot Parity Gate与`PRODUCT_DELIVERY_PASS`通过；该依赖已由正式Paddle回执、PR #16、PR #17和`origin/develop@d1fa4905807a7361ad2d4a5524a9e389312c0a74`远端readback满足。
- G05产品分支已从fresh `origin/develop@d1fa4905807a7361ad2d4a5524a9e389312c0a74`建立；来源准入先于032和用户投影执行。
- 首版逐来源回读身份、HTTPS、条款、保存权、撤回和更新；不清的来源标记`NOT_READY`，不以模型常识替代，也不阻止已准入来源的独立切片。
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

| Package | Branch / worktree | Owned outcome | Current state |
|---|---|---|---|
| `WP-G05-INTEGRATOR` | `codex/g05-city-knowledge` / `...-g05-integration` | 来源准入、032、共享schema/repository/API、OpenAPI/client、建议UI、消融、CI与最终E2E | `MERGED` |

G05以`INTEGRATOR_ONLY`完成，没有激活贡献writer或独立任务；同一集成者按来源领域/许可 → 032与内部API → 建议投影/UI/消融/E2E串行交付，避免共享合同并行写入。

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
- G06/G07产品工作、H1、公网、生产、商业、发布、部署或`main`合并。

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

- 实现branch/worktree/upstream：`codex/g05-city-knowledge` / `D:/munto/code/claudeProject/agentTravel-g05-integration` / `origin/develop@d1fa4905807a7361ad2d4a5524a9e389312c0a74`；
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
- 当前只启用唯一集成者；未准入来源不会阻断已准入来源的独立交付切片。

## HITL

新增或扩大来源、书面授权、付费/API账号、pgvector或新依赖、许可矛盾、未预批准migration、H1/商业/公网/生产/release/deploy/`main`需Owner批准。普通代码、测试、构建和已授权来源的许可readback由总指挥继续诊断。

## Checkpoint ledger

| 时间 | 用户结果 | Commit | Verification | Evidence level | Product progress | Governance ratio | Remaining | Risk/failure | Next autonomous action |
|---|---|---|---|---|---|---|---|---|---|
| 2026-08-31 | G05三城有来源知识层已通过准入、PostgreSQL、公开投影、浏览器和产品交付门，经PR #18合入`develop`；G06只由独立治理过渡激活 | 产品`363daed34d25b991ad9699a7381ac0d64e658e8b`；首轮CI绑定`9dcd911c85688cc8b5783a37e8c03f6cee413baa`；交付回执`4000c814973c16a13424b7294e3131743ed32ef7`；integration `c416dcdc40fcef2aef56627ab28c6f4049dc7dd9` | 回执tip CI `33389553342 PASS`；PR #18 `MERGED`；develop exact-tip CI `33389970986 PASS`；fresh fetch、`rev-parse origin/develop`与`ls-remote`一致；交付回执`PRODUCT_DELIVERY_PASS`和产品指纹`5e3838abbd35e500a6b505067d63fcf281b90d9d7127bc15cc92f34103a7881b`可回读 | `OFFICIAL_PUBLIC_HTTPS_FACTS_ONLY / REMOTE_AUTOMATED / REAL_POSTGRESQL / AUTOMATED_FIXTURE_BROWSER / REMOTE_INTEGRATION_PASS` | `Product progress=API+RUNTIME+UI / DELIVERY_INTEGRATED` | `Governance ratio=completed Goal archive; no product byte mutation` | G05产品工作为0；由同一治理过渡完整归档并原子激活G06 | 典型时长与15个地点仍是明确缺口；自动浏览器不等于公网或真人证据；G04两个精确历史失败例外仍须在G07 exact-binding前移除 | 原子激活G06完整合同，不启动G06产品、033、H1、公网、生产、商业、发布、部署或`main` |
| 2026-08-31 | 三城有来源建议、可撤回知识存储、动态读取与详情展示已通过全部远端产品门禁；耐久G05交付回执已生成并绑定不变产品指纹 | 首轮exact subject `9dcd911c85688cc8b5783a37e8c03f6cee413baa`；delivery receipt为本checkpoint | GitHub Actions `33386769272`：preflight、`g05_knowledge_targeted`、`g05_postgresql`、`frontend_build`、`g05_browser_e2e`与`core-mainline`全部PASS；产品commit `363daed34d25b991ad9699a7381ac0d64e658e8b`；产品指纹`5e3838abbd35e500a6b505067d63fcf281b90d9d7127bc15cc92f34103a7881b`；冻结manifest/oracle哈希回读一致 | `REMOTE_AUTOMATED / REAL_POSTGRESQL / AUTOMATED_FIXTURE_BROWSER / PRODUCT_DELIVERY_PASS` | `Product progress=API+RUNTIME+UI / KNOWLEDGE_ADMISSION_PASS` | `Governance ratio=durable delivery receipt; pending protected integration` | push/readback本回执，在回执精确tip重跑required CI，经PR #18合入`develop`，再独立归档G05并激活G06 | 典型时长与15个地点保持明确缺口；浏览器不是公网或真人证据；G04两个精确历史失败例外不扩大且须在G07 exact-binding前移除 | 本地验证delivery receipt，提交push并等待PR #18回执tip的完整CI |
| 2026-08-31 | 北京、上海、杭州地点卡已可动态装配最多3条有来源建议；未来版本不提前遮蔽当前建议，撤回、过期、冲突或检索失败不会改变行程、地图、POI、路线或Finding；删除行程会同步清除使用回执 | `363daed34d25b991ad9699a7381ac0d64e658e8b` | 来源Gate与消融 `PASS`：18地点逐一处置、4个准入来源、1个`NOT_READY`、4种有claim、典型时长明确缺口，precision `1.0`、unsupported `0`、actionability提升 `16.67pp`、P95回退低于`20%`；定向 `57 passed`；PostgreSQL 032 fresh/after-034、不同bundle并发导入、幂等重放、未来版本、版本升级、并发撤回、claim/source撤回、使用回执与隐私删除 `3 passed`；治理 `46 passed`；OpenAPI/client、frontend build、Playwright `3 passed`、Ruff、diff check、core-mainline范围校验和远端subject readback均通过 | `OFFICIAL_PUBLIC_HTTPS_FACTS_ONLY / LOCAL_POSTGRESQL / AUTOMATED_FIXTURE_BROWSER / REMOTE_SUBJECT_READBACK` | `Product progress=IMPLEMENTED / DELIVERY_VERIFY_IN_PROGRESS` | `Governance ratio=bounded to G05 contract` | 取得首轮exact-tip CI；生成耐久交付回执后在回执tip复跑CI并经PR合入 | 冻结集中典型游览时长没有合规精确来源，保持`EXPLICIT_GAP`；G04两个精确历史失败例外原样保留至G07；本地浏览器为自动fixture而非公网或真人证据 | 记录checkpoint绑定，创建PR并等待required CI |
| 2026-08-31 | G04截图入口已并入`develop`并完整归档；G05三城有来源知识层合同原子激活，激活时尚未修改G05产品代码或访问外部来源 | `ec7cdc73f99b30d24b29f29a3fca70c65e75fea7` | PR #17 `MERGED`；exact-tip GitHub Actions `33359977383 PASS`；fresh `origin/develop@d1fa4905807a7361ad2d4a5524a9e389312c0a74` fetch/ls-remote一致 | `PRODUCT_DELIVERY_PASS / REMOTE_INTEGRATION_PASS / GOAL_TRANSITION` | `Product progress=NONE / GOAL_TRANSITION` | `Governance ratio=100% / atomic archive and activation only` | 从fresh develop建立G05实现分支，先完成来源准入再进入032和用户建议 | 激活时G05来源准入、产品、PostgreSQL、浏览器与交付Gate均`NOT_RUN`；G04方案A不得外推为普通全量pytest零失败 | 建立唯一G05实现分支并登记exact baseline |

## Auto-advance

- Required gate：`Knowledge Admission Gate + PRODUCT_DELIVERY_PASS`已满足；Next template：`TC-VNEXT-G06-MEMORY-SHARE.md`；
- subject push/readback、耐久`PRODUCT_DELIVERY_PASS`、PR #18 integration与develop exact-tip CI均通过；本治理过渡完整归档G05并原子更新Goal binding与work-package registry激活G06。不登记外部ledger、不创建authority generation；H1/公网/生产/商业不自动启动。

## Completion record

- Status：`COMPLETED / DELIVERY_INTEGRATED`；Subject commits / Remote branch：产品`363daed34d25b991ad9699a7381ac0d64e658e8b`，首轮CI tip `9dcd911c85688cc8b5783a37e8c03f6cee413baa`，delivery receipt `4000c814973c16a13424b7294e3131743ed32ef7` / `origin/codex/g05-city-knowledge`；
- Verification / Evidence / Gate result / `structurally_valid`：`REMOTE_REQUIRED_CI_PASS + REMOTE_INTEGRATION_PASS / OFFICIAL_PUBLIC_HTTPS_FACTS_ONLY + REAL_POSTGRESQL + AUTOMATED_FIXTURE_BROWSER / PRODUCT_DELIVERY_PASS / true`；PR #18以`c416dcdc40fcef2aef56627ab28c6f4049dc7dd9`合入，develop exact-tip CI `33389970986 PASS`且远端readback一致；
- H1 / production / commercial：`NOT_RUN / NOT_RUN / NOT_RUN`；公网、release、deploy和`main`同样未运行或未请求；
- User-visible result / Remaining risks / Goal archived / Next activated：`三城动态有来源建议、详情展示、时效过滤和撤回生命周期已集成交付 / 典型时长与15个地点保持明确缺口；G04两个精确历史失败例外须在G07 exact-binding前移除 / true / true`；
- Promotion decision：`NOT_REQUESTED`。

## Stop conditions

- 来源许可或保存权不清；
- 需要抓取未授权内容；
- `KnowledgeClaim`无法与`EvidenceFact`隔离；
- 需要新增向量依赖但消融未证明价值；
- 建议无法标明时效和条件；
- fresh `origin/develop`不再由G05合法接棒，或激活必须修改产品字节。
