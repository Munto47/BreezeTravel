# PREDEFINED GOAL：V0.6 显式记忆与分享

## Metadata

- Goal ID：`TC-VNEXT-G06-MEMORY-SHARE`
- Program ID：`TC-VNEXT-2026`
- Product version：`V0.6`
- Mainline phase：`PRODUCT_ENHANCEMENT`
- Gate profile：`CORE_AGENT_GATE`
- Status：`DRAFT`
- Activation：G05 Knowledge Admission Gate通过并归档后
- Required gate：`Consent & Share Gate + AGENT_GATE_PASS`
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

## Parallel work packages

| Package | Owned paths（激活时精确化） | Dependencies | Acceptance |
|---|---|---|---|
| `WP-G06-CONSENT-MEMORY` | consent、结构化偏好、查看/修改/清空 | G01删除合同 + 033由集成者编号 | default-off、删除fresh readback |
| `WP-G06-SHARE-PROJECTION` | 分享投影、创建/撤销/只读访问 | UserFacingTripResult | token摘要、不可枚举、撤销后不可访问 |
| `WP-G06-FEEDBACK` | 地点纠正、删除/替换、建议反馈事件 | 独立训练consent | 最小事件且无原文/聊天长期留存 |

激活时主对话为三包生成完整v1提示词并登记独立用户可见功能对话、branch/worktree、prompt hash与exact baseline；先启动两个包，第三包`WAITING_FOR_WRITER_SLOT`，有一个经集成者验收冻结后再启动。子Agent只读复核/诊断，不得写产品代码或改状态。全部冻结后，集成者串行完成consent/权限领域→API/持久化→分享UI→隐私E2E。贡献包不得修改治理、migration、共享OpenAPI生成物或锁文件，不得自行合并；最多两轮修复复审。

## Decisions locked

- 记忆默认关闭。
- 不长期保存原始攻略、截图或聊天。
- G06只新增结构化偏好和反馈consent，不改变G01的30天source TTL和删除权。
- 产品记忆不等于训练同意。
- 用户删除必须真实删除或匿名化并可回读。
- 分享只使用用户投影，不含内部字段。
- 不恢复六位房间号作为入口。
- 分享接收者默认只读。
- 分享链接使用`/share/{share_ref}#s=<secret>`：fragment只在浏览器内使用，页面在任何日志/分析启动前以请求体换取短期HttpOnly capability并立即清除fragment。secret不得进入服务端可见URL、访问日志、Referer或分析事件；删除行程/清空账号旅行数据必须撤销相关分享并清理session与缓存。
- 提交反馈不代表训练/评测授权；数据用途同意是独立、默认关闭且可撤销的资源。“清空全部旅行数据”固定清除偏好和反馈并撤销全部分享。

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
- token不可枚举、fragment交换后立即清除、撤销/过期有效；
- 分享页内部字段0；
- 删除后fresh readback无残留业务值。

## Verification

- consent状态机；
- PostgreSQL 033；
- access control、IDOR、fragment-to-cookie交换和token；
- deletion/recovery；
- public payload/DOM scan；
- browser memory/share/revoke；
- privacy/secret；
- 三角色Agent审查、ultra裁决、所需sealed agent blind与同commit fresh readback；
- H1/商业：`NOT_RUN`。

## Authority

- `AGENTS.md`、Charter、Spec、v3 API、Architecture；Program、Roadmap、Release Gates、Agent Gate Protocol、Product Mainline Execution Guide、Provider Admission、Risk Register；ADR-011、ADR-012、ADR-013、ADR-014。

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

| 时间 | 用户结果 | Commit | Verification | Evidence level | Product progress | Governance ratio | Remaining | Risk/failure | Next autonomous action |
|---|---|---|---|---|---|---|---|---|---|
| 激活时填写 |  |  |  |  |  |  |  |  |  |

## Auto-advance

- Required gate：`Consent & Share Gate`；Next template：`TC-VNEXT-G07-CANDIDATE.md`；
- subject push/readback、耐久`AGENT_GATE_PASS`、clean tree、无Stop后，最终归档，并原子更新Goal binding与work-package registry激活G07；不登记外部ledger、不预创建authority generation；H1/商业不自动启动。

## Completion record

- Status / Subject commits / Remote branch：激活后填写；
- Verification / Evidence / Gate result / `structurally_valid`：激活后填写；
- H1 / production / commercial：激活时固定为`NOT_RUN / NOT_RUN / NOT_RUN`；
- User-visible result / Remaining risks / Goal archived / Next activated：激活后填写；
- Promotion decision：`NOT_REQUESTED`。

## Stop conditions

- 无法保证删除或consent分离；
- 需要保存原文实现个性化；
- 需要实时协同扩大范围；
- 需要第三方账号、付费服务或外部数据共享；
- 分享无法避免越权或内部泄漏。
