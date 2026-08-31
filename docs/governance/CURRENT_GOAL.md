# IN PROGRESS GOAL：V0.9 候选版收口

Goal ID: TC-VNEXT-G07-CANDIDATE
Status: IN_PROGRESS
Goal type: CANDIDATE_HARDENING

<!-- PRODUCT_DELIVERY_CURRENT_GOAL_STATE
{
  "schema_version": "product-delivery-current-goal-state-v1",
  "program_id": "TC-VNEXT-2026",
  "goal_id": "TC-VNEXT-G07-CANDIDATE",
  "goal_status": "IN_PROGRESS",
  "gate_profile": "HARDENED_CANDIDATE_GATE",
  "required_gate": "Candidate Evidence Gate G0～G7 + HARDENED_CANDIDATE_GATE_PASS",
  "completion_status": "NOT_RUN",
  "gate_result": "HARDENED_CANDIDATE_GATE_NOT_RUN",
  "goal_archived": false,
  "last_completed_goal_id": "TC-VNEXT-G06-MEMORY-SHARE",
  "next_goal_id": "TC-H1-G01-HUMAN-USABILITY",
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

- Goal ID：`TC-VNEXT-G07-CANDIDATE`
- Program ID：`TC-VNEXT-2026`
- Product version：`V0.9`
- Mainline phase：`CANDIDATE_HARDENING`
- Gate profile：`HARDENED_CANDIDATE_GATE`
- Status：`IN_PROGRESS`
- Activation：G06 Consent & Share Gate与`PRODUCT_DELIVERY_PASS`已通过并归档
- Governance transition baseline：`origin/develop@9994be151923b9c349fc1129605777032a0b8ebe`
- Activation branch / worktree：`codex/g06-g07-transition` / `D:/munto/code/claudeProject/agentTravel-g06-g07-transition`
- Canonical implementation branch / worktree：`codex/g07-candidate` / `D:/munto/code/claudeProject/agentTravel-g07-candidate`，已从fresh `origin/develop`创建
- Upstream / remote readback：`origin/develop` / `ff36a10ecae98088742e9722da3f4bf3676f6d04`，2026-08-31 fresh fetch、`rev-parse`与`ls-remote`三方一致
- Predecessor：G06产品`e3de1b57b014439ec16eb0034e8b7e47867053d0`、交付回执`215770f2ad975ed89271047fa40780fdddbd02a0`、PR #20 integration `9994be151923b9c349fc1129605777032a0b8ebe`；develop exact-tip GitHub Actions `33402780730 PASS`
- Required gate：`Candidate Evidence Gate G0～G7 + HARDENED_CANDIDATE_GATE_PASS`
- Next Goal：`TC-H1-G01-HUMAN-USABILITY`（仅人工批准后）

## Dependencies

- 唯一激活依赖是G06归档且Consent & Share Gate与`PRODUCT_DELIVERY_PASS`通过；该依赖已由耐久回执、PR #20、`develop@9994be151923b9c349fc1129605777032a0b8ebe`远端readback和exact-tip CI满足。
- G06→G07治理过渡已由PR #21并入`develop@ff36a10ecae98088742e9722da3f4bf3676f6d04`，G07独立实现工作树已建立；候选评测、Provider、性能、可靠性、blind与复审仍未运行。
- 首个preflight先绑定fresh branch/baseline并修复G04方案A；随后冻结候选RunSpec、Provider绑定和全部required Gate矩阵。缺失项标记`NOT_RUN/NOT_READY`并自主修复，只有确需新授权或人工阶段时才按HITL处理，不能把缺失证据包装成PASS。
- G04方案A恰好两个历史失败例外保持原样披露；G07首个阻断动作是修复并移除该例外，移除前不得接受exact-binding或宣称整仓pytest零失败。

## User Outcome

用户可在候选环境稳定完成登录/体验、文本或截图输入、卡片编辑、地图查看与手动更新、住宿选择、Top-3核验、建议采纳、偏好和分享；每项能力有同一候选commit的可回读证据。

## Scope

- 只修复现有主链阻断和candidate regression；
- 性能、无障碍、隐私、安全和恢复；
- model/provider snapshot与live矩阵；
- PostgreSQL、并发、幂等、lease和重启；
- controlled public demo材料；
- architecture/recovery diagrams；
- model ablation；
- release manifest与最终disclosure。
- 将旧manifest生成器适配TC-VNEXT Goal/Gate、v3 OpenAPI、新数据集和同绑定receipts；旧360/三城测试只作历史兼容。

## Pre-approved actions

- 不预批准新产品功能、migration或Provider；
- 允许在既有合同内修复候选阻断；
- 允许当前已有零增量费用Provider Gate；
- 允许受控demo artifact、视频脚本和manifest；
- 公网部署本身仍需人工批准。

## Parallel work packages

| Package | Owned paths（首个候选preflight精确化） | Dependencies | Acceptance | Activation state |
|---|---|---|---|---|
| `WP-G07-INTEGRATOR` | G07治理、候选评测/回执、既有CI及阻断修复；首切片仅限Trip NLU candidate manifest与方案A移除路径 | G06冻结候选 | fresh baseline可回读；两个历史失败恢复普通PASS；例外执行器不再生效 | `INTEGRATOR_ONLY / CANDIDATE_HARDENING` |
| `WP-G07-PERFORMANCE` | 性能、资源预算和基准回执 | G01～G06冻结候选 | 主链P95与资源预算通过 | `NOT_STARTED` |
| `WP-G07-RELIABILITY` | 并发、恢复、lease、幂等与故障矩阵 | 同commit候选 | 重复副作用0、恢复可回读 | `NOT_STARTED` |
| `WP-G07-PRIVACY-DEMO` | 隐私/权限审查、manifest和演示材料 | 同commit公共投影 | 泄漏0、材料与边界一致 | `NOT_STARTED` |

当前registry只激活唯一集成者的G07候选加固切片。首切片先把Trip NLU candidate manifest精确绑定到当前validator/schema/scorer/gate/generator字节，并移除方案A运行时兼容判定；不修改数据内容、blind/oracle或历史交付回执。完成普通全量pytest零失败后，再冻结候选RunSpec、全Gate矩阵和后续路径所有权。任何后续并行写入都需当时适用指令明确允许；否则由集成者串行执行可靠性/隐私材料→性能收口→同commit全量E2E/Gate→`HardeningDecision`、manifest和远端readback。

## Decisions locked

- 候选commit上重新运行G0～G7。
- 历史证据不得拼接。
- 自动/fixture/live/browser/public/human分层披露。
- `VNEXT_CANDIDATE_READY_AGENT_VERIFIED`不等于H1、生产或商业。
- 新功能请求进入未来Program，不在收口Goal扩展。
- 所有`NOT_RUN`明确列出。
- G04方案A两个精确历史失败例外必须在G07 exact-binding验收前移除；移除证据与候选commit绑定，不得扩大或重命名例外。
- `HardeningDecision`只有两种：`NOT_REQUIRED_WITH_RATIONALE`记录威胁、替代控制和残余风险；`REQUIRED`只启用威胁模型点名的控制。不得因为旧代码存在默认恢复八角色签名、broker、远端anchor或OCI。

## Non-goals

- 新城市深核验；
- 新模型/Provider；
- 一键登录；
- 新知识来源；
- 商业付费；
- H1招募和consent；
- 自动部署、release或`main`合并。

## Acceptance

完全继承Candidate Evidence Gate：

- G0～G7同一subject全部PASS，并取得`HARDENED_CANDIDATE_GATE_PASS`；
- 所有版本零容忍0；
- browser主链、刷新、断线、并发、重启、partial和performance通过；
- Provider许可与隐私无阻断；
- 受控demo、90秒视频、5分钟脚本、架构图、恢复图、消融和manifest可回读；
- final disclosure准确列出candidate、NOT_RUN和风险；
- clean tree、push和远端readback。
- `HardeningDecision`与候选commit绑定；所选控制全部实际验证，未选控制明确为`NOT_REQUIRED_WITH_RATIONALE`而非伪装PASS。

## Verification

- full backend pytest/Ruff；
- frontend/miniapp适用build；
- PostgreSQL fresh/existing migration；
- snapshot/replay；
- live Provider矩阵；
- browser E2E和P95；
- accessibility/security/privacy；
- release manifest hash/readback；
- 三角色Agent审查、fresh ultra裁决、全部所需sealed agent blind与clean checkout fresh readback；
- H1、production、commercial：`NOT_RUN`。

## Authority

- `AGENTS.md`、全部Blueprint产品/架构/治理权威、Agent Gate Protocol、Product Mainline Execution Guide、ADR-007～ADR-012、ADR-013、ADR-014；
- G01～G06 completed归档、当前候选RunSpec和同subject evidence；历史V1 manifest仅作baseline。

## Baseline

- 激活baseline：`origin/develop@9994be151923b9c349fc1129605777032a0b8ebe`；治理过渡branch/worktree：`codex/g06-g07-transition` / `D:/munto/code/claudeProject/agentTravel-g06-g07-transition`；
- 候选实现baseline：`origin/develop@ff36a10ecae98088742e9722da3f4bf3676f6d04`；branch/worktree：`codex/g07-candidate` / `D:/munto/code/claudeProject/agentTravel-g07-candidate`；候选依赖锁、OpenAPI/migration/provider/model/dataset版本由后续RunSpec切片冻结；
- dirty tree或不同binding结果不得拼接；H1/production/commercial：`NOT_RUN`。

## Invariants

- 不新增产品功能、不降低Gate、不修改blind/oracle；
- G0～G7同一subject/config/dataset/model/rule/provider重新运行；
- fixture/snapshot/live/browser/public/human/commercial分层；UNKNOWN/NOT_RUN不算PASS；
- Provider许可、隐私删除、内部字段和事实正确性均为阻断项；
- `VNEXT_CANDIDATE_READY_AGENT_VERIFIED`不自动授权H1、公网、生产、release或`main`。

## Budget

- 只使用G01～G06已准入账号/Provider和现有无增量费用矩阵；候选RunSpec冻结并记录总调用/token/延迟/成本；
- 失败策略最多两次，同一blocker两个切片无改善触发独立诊断；每切片checkpoint。

## HITL

新功能/schema/migration/依赖/Provider、费用、修改blind、公开demo部署、H1招募/consent、release/`main`需批准。

## Checkpoint ledger

| 时间 | 用户结果 | Commit | Verification | Evidence level | Product progress | Governance ratio | Remaining | Risk/failure | Next autonomous action |
|---|---|---|---|---|---|---|---|---|---|
| 2026-08-31 | G06显式记忆与分享已交付、并入`develop`并完整归档；G07候选收口合同原子激活，尚未运行候选工作 | G06产品`e3de1b57b014439ec16eb0034e8b7e47867053d0`；回执`215770f2ad975ed89271047fa40780fdddbd02a0`；integration`9994be151923b9c349fc1129605777032a0b8ebe`；本治理过渡commit在提交后由远端readback记录 | G06首轮CI`33400646254 PASS`、回执tip CI`33402192501 PASS`、develop exact-tip CI`33402780730 PASS`；fresh fetch、`rev-parse`与`ls-remote`一致 | `PRODUCT_DELIVERY_PASS / REMOTE_INTEGRATION_PASS / GOAL_TRANSITION` | `Product progress=NONE / G07_NOT_STARTED` | `Governance ratio=100% / atomic G06 archive and G07 activation only` | 合并本过渡PR；从新develop建立G07实现分支；首先修复并移除G04方案A两个历史失败例外，再冻结候选RunSpec和exact bindings | G07全Gate、live Provider、90条统计、50链、复审、blind、性能、可靠性、隐私、供应链均`NOT_RUN`；H1、公网、生产、商业、发布、部署和main仍未运行或未请求 | 校验归档/绑定/范围，提交push并通过过渡CI；合并后fresh readback再开始G07 preflight |
| 2026-08-31 | G07已从最终G06→G07过渡tip建立隔离候选工作树；两个历史失败已在未修改基线上精确复现并定位为candidate manifest的validator/scorer/gate绑定过期 | baseline `ff36a10ecae98088742e9722da3f4bf3676f6d04`；本preflight checkpoint待提交 | fresh fetch/`ls-remote`一致；两节点原样`2 failed`，指纹均为`manifest evaluator/schema code binding mismatch`；当前schema/generator与清单一致，validator/scorer/gate三项不一致 | `LOCAL_AUTOMATED / EXACT_FAILURE_REPRODUCTION / CANDIDATE_HARDENING_PREFLIGHT` | `Product progress=EVAL_METRIC / G07_IN_PROGRESS` | `Governance ratio=preflight binding only` | 提交并远端回读preflight；只更新candidate manifest绑定并移除方案A执行器，再要求两节点和普通非P5全量pytest零失败 | G07其余Gate仍`NOT_RUN`；本checkpoint不接受exact-binding、不修改blind/oracle、不声明整仓绿 | 运行治理定向、scope guard与diff check，提交push后执行最小exact-binding修复 |

## Auto-advance

- Candidate Gate与Agent Gate通过后只可归档G07并标记`VNEXT_CANDIDATE_READY_AGENT_VERIFIED`；
- 不自动创建或激活H1 Goal，不自动部署、公网、release、商业或合并`main`；必须等待用户明确批准。

## Completion record

- Status / Subject commits / Remote branch：`IN_PROGRESS / preflight checkpoint待提交 / origin/codex/g07-candidate待首次push`；
- Verification / Evidence / Gate result / `structurally_valid`：`两个历史失败已精确复现 / LOCAL_AUTOMATED_PREFLIGHT / HARDENED_CANDIDATE_GATE_NOT_RUN / true`；结构有效不代表候选通过；
- H1 / production / commercial：`NOT_RUN / NOT_RUN / NOT_RUN`；H1、公网、生产、商业：`NOT_RUN`，release、deploy和`main`未请求；
- User-visible result / Remaining risks / Goal archived：`G06已交付，G07已在隔离fresh baseline开始候选收口 / G04两个历史例外尚未移除；候选全Gate与全部候选证据仍未运行 / false`；
- Next Goal activated：固定`NO_PENDING_HUMAN_APPROVAL`；
- Promotion decision：`NOT_REQUESTED`，除非用户另行批准H1。

## Stop conditions

- 需要新增产品功能才能通过；
- 需要降低任何Gate；
- 需要拼接历史证据；
- 需要新增Provider权限/费用，或隐私/事实矛盾只能通过改变Gate解决；
- 需要公网部署、H1、付费、release或`main`；
- 需要降低candidate blocker门槛而非继续技术诊断。
