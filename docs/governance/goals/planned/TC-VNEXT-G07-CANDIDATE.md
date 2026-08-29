# PREDEFINED GOAL：V0.9 候选版收口

## Metadata

- Goal ID：`TC-VNEXT-G07-CANDIDATE`
- Program ID：`TC-VNEXT-2026`
- Product version：`V0.9`
- Mainline phase：`CANDIDATE_HARDENING`
- Gate profile：`HARDENED_CANDIDATE_GATE`
- Status：`DRAFT`
- Activation：G06 Consent & Share Gate通过并归档后
- Required gate：`Candidate Evidence Gate G0～G7 + AGENT_GATE_PASS`
- Next Goal：`TC-H1-G01-HUMAN-USABILITY`（仅人工批准后）

## Dependencies

- 唯一激活依赖是G06归档且Consent & Share Gate通过；随后G07置为`APPROVED`。
- 首个preflight填写branch/baseline、候选RunSpec、Provider绑定和全部required Gate矩阵；缺失项标记`NOT_RUN/NOT_READY`并自主修复，只有确需新授权或人工阶段时才按HITL处理，不能把缺失证据包装成PASS。

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

| Package | Owned paths（激活时精确化） | Dependencies | Acceptance |
|---|---|---|---|
| `WP-G07-PERFORMANCE` | 性能、资源预算和基准回执 | G01～G06冻结候选 | 主链P95与资源预算通过 |
| `WP-G07-RELIABILITY` | 并发、恢复、lease、幂等与故障矩阵 | 同commit候选 | 重复副作用0、恢复可回读 |
| `WP-G07-PRIVACY-DEMO` | 隐私/权限审查、manifest和演示材料 | 同commit公共投影 | 泄漏0、材料与边界一致 |

集成者串行运行同commit全量Gate、`HardeningDecision`、manifest和远端readback。不得在贡献包内增加产品功能；最多两轮修复复审，非阻断项按风险排序披露。

## Decisions locked

- 候选commit上重新运行G0～G7。
- 历史证据不得拼接。
- 自动/fixture/live/browser/public/human分层披露。
- `VNEXT_CANDIDATE_READY_AGENT_VERIFIED`不等于H1、生产或商业。
- 新功能请求进入未来Program，不在收口Goal扩展。
- 所有`NOT_RUN`明确列出。
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

- G0～G7同一subject全部PASS，并取得`AGENT_GATE_PASS`；
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

- branch/commit/upstream、G06 transition、候选依赖锁、OpenAPI/migration/provider/model/dataset版本：激活时填写；
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
| 激活时填写 |  |  |  |  |  |  |  |  |  |

## Auto-advance

- Candidate Gate与Agent Gate通过后只可归档G07并标记`VNEXT_CANDIDATE_READY_AGENT_VERIFIED`；
- 不自动创建或激活H1 Goal，不自动部署、公网、release、商业或合并`main`；必须等待用户明确批准。

## Completion record

- Status / Subject commits / Remote branch：激活后填写；
- Verification / Evidence / Gate result / `structurally_valid`：激活后填写；
- H1 / production / commercial：激活时固定为`NOT_RUN / NOT_RUN / NOT_RUN`；
- User-visible result / Remaining risks / Goal archived：激活后填写；
- Next Goal activated：固定`NO_PENDING_HUMAN_APPROVAL`；
- Promotion decision：`NOT_REQUESTED`，除非用户另行批准H1。

## Stop conditions

- 需要新增产品功能才能通过；
- 需要降低任何Gate；
- 需要拼接历史证据；
- 需要新增Provider权限/费用，或隐私/事实矛盾只能通过改变Gate解决；
- 需要公网部署、H1、付费、release或`main`；
- 需要降低candidate blocker门槛而非继续技术诊断。
