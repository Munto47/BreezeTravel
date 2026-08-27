# PREDEFINED GOAL：V0.6 显式记忆与分享

## Metadata

- Goal ID：`TC-VNEXT-G06-MEMORY-SHARE`
- Program ID：`TC-VNEXT-2026`
- Product version：`V0.6`
- Status：`DRAFT`
- Activation：G05 Knowledge Admission Gate通过并归档后
- Required gate：`Consent & Share Gate`
- Next Goal：`TC-VNEXT-G07-CANDIDATE`

## Dependencies

- 唯一激活依赖是G05归档且Knowledge Admission Gate通过；随后G06置为`APPROVED`。
- 首个preflight填写branch/baseline、G01 source删除、现有认证与privacy threat model；缺失lane标记`NOT_READY`并继续其他安全独立切片，不能把source删除推迟到本Goal。

## User Outcome

用户可以主动选择记住步行容忍度、出发时间、餐饮、酒店和强度偏好，随时查看、修改或清空；还可以生成朋友可读、可撤销的分享行程，而不重新引入房间号和项目黑话。

## Scope

- memory consent、view/update/delete；
- 结构化`PreferenceMemory`；
- correction/adoption/rejection/voluntary feedback；
- training/eval consent分离；
- 最小披露`ShareProjection`；
- 不可枚举、可撤销、可过期分享token；
- 权限、删除、恢复和审计；
- 用户友好分享UI。

## Pre-approved actions

- consent合同通过后`033_user_memory_and_feedback.sql`；
- 新memory和share v3 API；
- 现有认证与PostgreSQL；
- 不新增第三方CRM、analytics或数据仓库。

## Decisions locked

- 记忆默认关闭。
- 不长期保存原始攻略、截图或聊天。
- G06只新增结构化偏好和反馈consent，不改变G01的30天source TTL和删除权。
- 产品记忆不等于训练同意。
- 用户删除必须真实删除或匿名化并可回读。
- 分享只使用用户投影，不含内部字段。
- 不恢复六位房间号作为入口。
- 分享接收者默认只读。

## Non-goals

- 多人实时协同/Yjs；
- 自动社交发布；
- 广告画像；
- 未经同意训练；
- CRM和增长自动化；
- 商业付费。

## Acceptance

完全继承Consent & Share Gate：

- default-off；
- view/update/delete全通过；
- 原文/截图/聊天长期留存0；
- consent分离；
- 越权0；
- token不可枚举、撤销/过期有效；
- 分享页内部字段0；
- 删除后fresh readback无残留业务值。

## Verification

- consent状态机；
- PostgreSQL 033；
- access control、IDOR和token；
- deletion/recovery；
- public payload/DOM scan；
- browser memory/share/revoke；
- privacy/secret；
- H1/商业：`NOT_RUN`。

## Authority

- `AGENTS.md`、Charter、Spec、v3 API、Architecture；Program、Roadmap、Release Gates、Provider Admission、Risk Register；ADR-011、ADR-012。

## Baseline

- branch/commit/upstream、G05 subject/transition、G01 source deletion readback和现有auth边界：激活时填写；
- 旧memory/Yjs/room只作frozen asset，不是consent证明；H1/商业：`NOT_RUN`。

## Invariants

- 记忆default-off、字段allowlist、可查看/更改/清空；产品记忆/反馈/训练同意分离；
- 删除是fresh readback可证明的真实删除或不可逆匿名化，不保留业务值；
- 分享只消费UserFacing投影，token不可枚举、可撤销、过期且接收者默认只读；
- 原文/截图/聊天不进入长期记忆，内部ID/证据不进入分享或分析。

## Budget

- 只保存已批准结构化偏好字段和最小反馈事件；token TTL、访问频率和审计保留期在RunSpec冻结；
- 不新增CRM、analytics、仓库、外部分享账号或付费服务；每切片checkpoint。

## HITL

训练/eval consent范围、第三方共享、外部analytics/CRM、付费、新依赖、H1/商业/公网/生产/`main`需批准。

## Checkpoint ledger

| 时间 | 用户结果 | Commit | Verification | Evidence level | Remaining | Risk/failure | Next autonomous action |
|---|---|---|---|---|---|---|---|
| 激活时填写 |  |  |  |  |  |  |  |

## Auto-advance

- Required gate：`Consent & Share Gate`；Next template：`TC-VNEXT-G07-CANDIDATE.md`；
- subject push/readback、Gate PASS、clean tree、无Stop后，最终归档并原子激活G07；H1/商业不自动启动。

## Completion record

- Status / Subject commits / Remote branch：激活后填写；
- Verification / Evidence / Gate result / `structurally_valid`：激活后填写；
- User-visible result / Remaining risks / Goal archived / Next activated：激活后填写；
- Promotion decision：`NOT_REQUESTED`。

## Stop conditions

- 无法保证删除或consent分离；
- 需要保存原文实现个性化；
- 需要实时协同扩大范围；
- 需要第三方账号、付费服务或外部数据共享；
- 分享无法避免越权或内部泄漏。
