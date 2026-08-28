# PREDEFINED GOAL：V0.5 三城有来源知识层

## Metadata

- Goal ID：`TC-VNEXT-G05-CITY-KNOWLEDGE`
- Program ID：`TC-VNEXT-2026`
- Product version：`V0.5`
- Status：`DRAFT`
- Activation：G04 Screenshot Parity Gate通过并归档后
- Required gate：`Knowledge Admission Gate + AGENT_GATE_PASS`
- Next Goal：`TC-VNEXT-G06-MEMORY-SHARE`

## Dependencies

- 唯一激活依赖是G04归档且Screenshot Parity Gate通过；随后G05置为`APPROVED`。
- 首个preflight填写branch/baseline并逐来源readback许可、robots/API条款、保存权、撤回和更新；不清的来源lane标记`NOT_READY`或移出当前实验，不以模型常识替代，也不阻止已准入来源的独立切片。

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

- 许可Gate通过后`032_knowledge_claims.sql`；
- 官方公开页面和明确允许的API；
- 授权创作者内容；
- PostgreSQL检索；pgvector只有消融证明必要且经依赖审查后另行批准；
- 不授权抓取小红书。

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
- 医疗、安全或客流保证。

## Acceptance

完全继承Knowledge Admission Gate：

- required字段和来源100%；
- 未授权来源0；
- 过期claim使用0；
- RAG决定POI/路线/HARD为0；
- 同validation set消融达到：建议precision≥90%、unsupported=0、actionability提升≥5个百分点、P95回退≤20%；
- 页面依据可理解且不泄漏内部receipt；
- 删除/撤回来源可回读。

## Verification

- source/license/admission审计；
- claim schema/expiry/conflict；
- retrieval与ablation；
- user wording；
- PostgreSQL 032；
- three-city browser cases；
- privacy/copyright scan；
- 三角色Agent审查、ultra裁决、所需sealed agent blind与同commit fresh readback；
- H1/商业：`NOT_RUN`。

## Authority

- `AGENTS.md`、Charter、Spec、v3 API、Architecture；Program、Roadmap、Release Gates、Agent Gate Protocol、Provider Admission、Risk Register；ADR-011、ADR-012、ADR-013。

## Baseline

- branch/commit/upstream、G04 subject/transition和三城已准入来源清单：激活时填写；
- 旧RAG/Public corpus只作frozen baseline，不能自动进入KnowledgeClaim；H1/商业：`NOT_RUN`。

## Invariants

- KnowledgeClaim绑定canonical place、来源、条件、observed/effective/expires、许可和撤回；
- RAG只给建议，不决定POI、路线、营业硬事实或Finding；过期/冲突/未授权不进入当前建议；
- 不抓取小红书或绕过登录/robots/付费墙；用户内容不自动公共化或训练；
- public projection说明依据但不泄漏receipt/内部评分。

## Budget

- 每类来源先小样本admission，再按三城覆盖扩展；抓取频率、TTL、存储量和检索deadline在RunSpec冻结；
- 不预批准付费数据、pgvector、GraphRAG或新抓取基础设施；每切片checkpoint。

## HITL

新增/扩大来源、书面授权、付费/API账号、pgvector或新依赖、许可矛盾、H1/商业/公网/生产/`main`需批准。

## Checkpoint ledger

| 时间 | 用户结果 | Commit | Verification | Evidence level | Remaining | Risk/failure | Next autonomous action |
|---|---|---|---|---|---|---|---|
| 激活时填写 |  |  |  |  |  |  |  |

## Auto-advance

- Required gate：`Knowledge Admission Gate`；Next template：`TC-VNEXT-G06-MEMORY-SHARE.md`；
- subject push/readback、耐久`AGENT_GATE_PASS`登记到仓库外Goal pass ledger、clean tree、无Stop后，最终归档，按Program稳定binding原子激活G06并创建generation 6权限锚；H1/商业不自动启动。

## Completion record

- Status / Subject commits / Remote branch：激活后填写；
- Verification / Evidence / Gate result / `structurally_valid`：激活后填写；
- H1 / production / commercial：激活时固定为`NOT_RUN / NOT_RUN / NOT_RUN`；
- User-visible result / Remaining risks / Goal archived / Next activated：激活后填写；
- Promotion decision：`NOT_REQUESTED`。

## Stop conditions

- 来源许可或保存权不清；
- 需要抓取未授权内容；
- KnowledgeClaim无法与EvidenceFact隔离；
- 需要新增向量依赖但消融未证明价值；
- 建议无法标明时效和条件。
