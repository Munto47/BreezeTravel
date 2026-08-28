# Goal 合同模板

> 每个Goal复制本模板。`CURRENT_GOAL.md`只能存在一个`APPROVED`或`IN_PROGRESS` Goal。

## Metadata

- Goal ID：
- Program ID：`TC-VNEXT-2026`
- Product version：
- Status：`DRAFT / APPROVED / IN_PROGRESS / EVIDENCE_READY / COMPLETED / REJECTED`
- Goal type：
- Branch：
- Baseline commit：
- Approved by / at：
- Required gate：
- Next Goal：

## Dependencies

- 唯一激活依赖是上一Goal已归档、FINAL_GATE签名PASS已写入仓库外append-only Goal pass ledger，且本Goal的序号/前驱/自动Gate合同与不可变authority policy一致；满足后按Program顺序置为`APPROVED`。
- 当前环境已有Provider凭据由程序安全自动readback，未暴露字段写`NOT_EXPOSED_BY_PROVIDER`；缺失lane标记`NOT_READY`，不得伪造或fallback，但不阻止其他安全独立切片。
- 只有新增账号/付费/数据权限、改变产品目标或Gate、H1/公网/生产等真实外部动作才按HITL/Stop请求；不得留下0个active Goal。

## User Outcome

本Goal结束后，普通用户能完成什么可观察行为。禁止用“新增模块”“增加测试”代替。

## Scope

- 唯一纵向链：
- 允许修改的产品子系统：
- 允许新增的API/schema/migration：
- 数据集与Provider范围：
- 用户界面变化：

## Non-goals

明确本Goal不做的产品能力、Provider、数据、基础设施、部署、真人和商业行为。

## Authority

引用适用的AGENTS、Charter、Spec、API、Architecture、Program、Roadmap、Release Gates和ADR。

## Baseline

- branch/commit/upstream：
- root worktree保护边界：
- 当前用户可见行为：
- 已知失败：
- 已通过验证：
- `NOT_RUN`：
- 历史证据边界：

## Decisions locked

列出本Goal不得重新发明的产品和技术决策，包括默认值、用户文案、状态机、模型、Provider、失败和兼容策略。

## Invariants

列出：

- 用户视图禁止字段；
- revision/CAS/idempotency；
- LLM与确定性权威；
- `UNKNOWN/UNAVAILABLE`；
- map/stay snapshot；
- privacy/licensing；
- compatibility；
- evidence等级：`AUTOMATED_TEST / LIVE_PROVIDER_EVIDENCE / MULTI_AGENT_SIMULATED_REVIEW / SEALED_AGENT_BLIND / HUMAN_USABILITY / PRODUCTION_EVIDENCE`。

## Acceptance cases

列出可执行输入、用户输出、内部状态和失败行为。每个案例应能由测试、浏览器或回读证明。

## Required Gate

引用`RELEASE_GATES.md`的具体Gate，并写出本Goal的零容忍项、指标、数据split和性能目标。G01～G07必须引用`AGENT_GATE_PROTOCOL.md`并以`AGENT_GATE_PASS`作为自动晋级条件。

## Verification

- Targeted tests：
- Backend/Frontend：
- PostgreSQL：
- Snapshot：
- Live Provider：
- Browser：
- Security/privacy：
- Diff/compatibility：
- Remote readback：
- 必须保持`NOT_RUN`的层级：

## Budget

- 模型/API账本：
- 费用与账号边界：
- deadline/retry：
- 每切片最大diff：
- checkpoint频率：

## Pre-approved actions

只列Program已批准的migration、API、依赖、Provider模式、离线数据、commit和push。未列出的受保护动作不是默认授权。

## HITL

只列新账号/付费、扩大外部数据、未批准schema/migration/依赖、读取或修改blind truth/oracle、H1/真人/公网、`main`、release/deploy和破坏性数据操作。按协议启动隔离Agent审查和sealed blind任务不属于HITL。

## Checkpoint ledger

每个切片追加一行：

| 时间 | 用户结果 | Commit | Verification | Evidence level | Remaining | Risk/failure | Next autonomous action |
|---|---|---|---|---|---|---|---|

## Auto-advance

- Required gate：
- Next Goal template：
- 条件：Outcome完成、同绑定候选取得`AGENT_GATE_PASS`、clean tree、subject checkpoint push/readback、无Stop condition。
- 过渡：先耐久物化当前FINAL_GATE签名PASS并登记到仓库外Goal pass ledger，再从当前Goal完整内容生成最终completed归档；同一治理过渡commit把`CURRENT_GOAL.md`替换为下一完整`APPROVED`合同，原子更新`current_goal_binding.json`并把authority generation精确加一，冻结下一Goal专属scorer/threshold/schema/exporter。下一Goal的Goal序号、前驱、canonical ref和自动Gate合同路径/hash必须逐项等于跨代稳定Program表；transition push/readback后由独立custody登记新generation anchor，且只有一个active Goal。
- H1、生产、公网、商业和`main`永不自动推进。

## Stop conditions

至少包括：

- 产品目标或Program顺序改变；
- 未预批准公共合同或依赖；
- 新账号/付费/外部数据；
- sealed blind/oracle修改；
- 证据矛盾；
- 真人、部署、release或破坏性操作。

普通测试、Agent Gate或blind失败留在当前Goal继续诊断；只有解决需要改变产品/Gate、扩大付费/数据权限或进入人工阶段时停止。

## Completion record

- Status：
- Subject commits：
- Remote branch/upstream：
- Verification：
- Evidence/artifacts：
- Gate result：
- H1 / production / commercial：
- `structurally_valid=true/false`：
- User-visible result：
- Remaining red lights：
- Goal archived：
- Next Goal activated：
- Promotion decision：`NOT_REQUESTED / REJECT / APPROVE_NEXT_PHASE`
