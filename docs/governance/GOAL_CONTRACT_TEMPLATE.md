# Goal 合同模板

> 每个Goal复制本模板。`CURRENT_GOAL.md`只能存在一个`APPROVED`或`IN_PROGRESS` Goal。

## Metadata

- Goal ID：
- Program ID：`TC-VNEXT-2026`
- Product version：
- Status：`DRAFT / APPROVED / IN_PROGRESS / EVIDENCE_READY / COMPLETED / REJECTED`
- Goal type：
- Mainline phase：`CORE_MVP / PRODUCT_ENHANCEMENT / CANDIDATE_HARDENING`
- Gate profile：`CORE_AGENT_GATE / HARDENED_CANDIDATE_GATE`
- Branch：
- Baseline commit：
- Approved by / at：
- Required gate：
- Next Goal：

## Dependencies

- 唯一激活依赖是上一Goal已归档、同绑定`AGENT_GATE_PASS`已耐久物化并完成远端readback，且本Goal序号、前驱、阶段和自动Gate合同与Program一致；满足后按Program顺序置为`APPROVED`。G01～G06不依赖外部Goal ledger、authority generation、签名或OCI。
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

### Parallel work packages

主对话框是唯一集成者；每个长期功能由一个用户可见的独立功能对话、独立branch/worktree承担。子Agent只读复核/诊断，不拥有工作包。每包填写`package_id / goal_id / baseline_commit / registry_activation_commit / branch / remote_branch / worktree_path / execution_mode / dialogue_ref / prompt_path / prompt_sha256 / role / dependencies / owned_paths / forbidden_paths / acceptance / merge_order / status / ready_commit / merged_commit`。

提示词遵循`WORK_PACKAGE_PROMPT_TEMPLATE.md`并写明用户目标、非目标、锁定接口、输入输出、定向测试、禁止修改路径和标准回报。集成者始终占一个writer名额，最多两个贡献包同时`IN_PROGRESS/BLOCKED_EXTERNAL`；第三个已生成提示词的当前包使用`WAITING_FOR_WRITER_SLOT`。功能对话只请求`READY_TO_MERGE`，由集成者验收并登记冻结commit。最多提前准备下一Goal两个`PREPARED_NOT_INTEGRATED`包。写明领域模型→持久化/API→前端→E2E的串行合并点。

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

| 时间 | 用户结果 | Commit | Verification | Evidence level | Product progress | Governance ratio | Remaining | Risk/failure | Next autonomous action |
|---|---|---|---|---|---|---|---|---|---|

## Auto-advance

- Required gate：
- Next Goal template：
- 条件：Outcome完成、同绑定候选取得`AGENT_GATE_PASS`、clean tree、subject checkpoint push/readback、无Stop condition。
- 过渡：先耐久物化当前Gate PASS并push/readback，再从当前Goal完整内容生成最终completed归档；同一治理过渡commit把`CURRENT_GOAL.md`替换为下一完整`APPROVED`合同，并原子更新`current_goal_binding.json + current_work_packages.json`，冻结下一Goal专属scorer/threshold/schema。下一Goal的Goal序号、阶段、前驱、canonical ref和自动Gate合同路径/hash必须逐项等于Program；G01～G06不登记外部ledger、不推进authority generation。G07只有`HardeningDecision=REQUIRED`时处理被点名的外部控制。
- G03通过后保存可体验里程碑并自动进入G04，不新增HITL；G07完成后停止。
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
