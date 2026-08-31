# IN_PROGRESS GOAL：V0.6 显式记忆与分享

Goal ID: TC-VNEXT-G06-MEMORY-SHARE
Status: IN_PROGRESS
Goal type: PRODUCT_ENHANCEMENT

<!-- PRODUCT_DELIVERY_CURRENT_GOAL_STATE
{
  "schema_version": "product-delivery-current-goal-state-v1",
  "program_id": "TC-VNEXT-2026",
  "goal_id": "TC-VNEXT-G06-MEMORY-SHARE",
  "goal_status": "IN_PROGRESS",
  "gate_profile": "PRODUCT_DELIVERY_GATE",
  "required_gate": "Consent & Share Gate + PRODUCT_DELIVERY_PASS",
  "completion_status": "PENDING",
  "gate_result": "PRODUCT_DELIVERY_NOT_RUN",
  "goal_archived": false,
  "last_completed_goal_id": "TC-VNEXT-G05-CITY-KNOWLEDGE",
  "next_goal_id": "TC-VNEXT-G07-CANDIDATE",
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

- Goal ID：`TC-VNEXT-G06-MEMORY-SHARE`
- Program ID：`TC-VNEXT-2026`
- Product version：`V0.6`
- Mainline phase：`PRODUCT_ENHANCEMENT`
- Gate profile：`PRODUCT_DELIVERY_GATE`
- Status：`IN_PROGRESS`
- Goal type：`PRODUCT_ENHANCEMENT`
- Governance transition baseline：`origin/develop@c416dcdc40fcef2aef56627ab28c6f4049dc7dd9`
- Exact implementation baseline：`origin/develop@e383eeef39b0246ce35dd3cb8481a02bbebd1130`
- Activation branch / worktree：`codex/g05-g06-transition` / `D:/munto/code/claudeProject/agentTravel-g05-g06-transition`
- Canonical implementation branch / worktree：`codex/g06-memory-share` / `D:/munto/code/claudeProject/agentTravel-g06-integration`，在本治理过渡PR合并后从新的fresh `origin/develop`创建
- Upstream / remote readback：`origin/develop` / `e383eeef39b0246ce35dd3cb8481a02bbebd1130`，2026-08-31 fresh fetch、`rev-parse`与`ls-remote`三方一致
- Predecessor：G05 product `363daed34d25b991ad9699a7381ac0d64e658e8b`、delivery receipt `4000c814973c16a13424b7294e3131743ed32ef7`、PR #18 integration `c416dcdc40fcef2aef56627ab28c6f4049dc7dd9`；develop exact-tip GitHub Actions `33389970986 PASS`
- Required gate：`Consent & Share Gate + PRODUCT_DELIVERY_PASS`
- Next Goal：`TC-VNEXT-G07-CANDIDATE`

## Dependencies

- 唯一激活依赖是G05归档且Knowledge Admission Gate与`PRODUCT_DELIVERY_PASS`通过；该依赖已由耐久回执、PR #18、`develop@c416dcdc40fcef2aef56627ab28c6f4049dc7dd9`远端readback和exact-tip CI满足。
- 本治理过渡只完整归档G05并激活G06，不实现记忆、分享、反馈、033、公共API、UI或Provider调用。
- 首个G06 preflight在过渡PR合并后填写fresh implementation baseline，并回读G01 source删除、现有认证边界与privacy threat model；缺失lane标记`NOT_READY`并继续其他安全独立切片，不能把source删除推迟到本Goal。
- G04方案A两个精确历史失败例外保持原样，必须在G07 exact-binding验收前移除；G06不得扩大例外或宣称整仓pytest零失败。

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

| Package | Owned paths（首个产品preflight精确化） | Dependencies | Acceptance | Activation state |
|---|---|---|---|---|
| `WP-G06-INTEGRATOR` | governance、033、共享schema/repository/API、OpenAPI/client、分享UI、CI与最终E2E | fresh `origin/develop@e383eeef39b0246ce35dd3cb8481a02bbebd1130` | Consent & Share Gate与`PRODUCT_DELIVERY_PASS` | `INTEGRATOR_ONLY / IN_PROGRESS` |
| `WP-G06-CONSENT-MEMORY` | consent、结构化偏好、查看/修改/清空 | G01删除合同 + 033由集成者编号 | default-off、删除fresh readback | `INTEGRATED / LOCAL_VERIFIED` |
| `WP-G06-SHARE-PROJECTION` | 分享投影、创建/撤销/只读访问 | UserFacingTripResult | token摘要、不可枚举、撤销后不可访问 | `INTEGRATED / LOCAL_VERIFIED` |
| `WP-G06-FEEDBACK` | 地点纠正、删除/替换、建议反馈事件 | 独立训练consent | 最小事件且无原文/聊天长期留存 | `INTEGRATED / LOCAL_VERIFIED` |

当前registry只激活唯一集成者；本次未使用贡献Agent。集成者已按consent/权限领域→API/持久化→分享UI→隐私E2E串行完成本地产品切片，远端CI、耐久回执和`develop`集成仍待执行。

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
- 商业付费；
- G07产品或候选收口工作；
- H1、公网、生产、发布、部署、release或`main`合并。

## Acceptance / Gate

完全继承Consent & Share Gate：

- default-off；
- view/update/delete全通过；
- 原文/截图/聊天长期留存0；
- consent分离；
- 越权0；
- token不可枚举、fragment交换后立即清除、撤销/过期有效；
- 分享页内部字段0；
- 删除后fresh readback无残留业务值；
- 当前记忆/分享定向测试、PostgreSQL 033、client/OpenAPI、frontend build与browser E2E全部PASS，并生成耐久`PRODUCT_DELIVERY_PASS`。

## Verification

- consent状态机；
- PostgreSQL 033；
- access control、IDOR、fragment-to-cookie交换和token；
- deletion/recovery；
- public payload/DOM scan；
- browser memory/share/revoke；
- privacy/secret；
- 当前记忆/分享用户旅程定向测试与浏览器E2E；候选复审和blind留到G07；
- H1、公网、生产、商业：`NOT_RUN`。

## Authority

- `AGENTS.md`、Charter、Spec、v3 API、Architecture；Program、Roadmap、Release Gates、Product Delivery Gate、Product Mainline Execution Guide、Provider Admission、Risk Register；ADR-011、ADR-012、ADR-013、ADR-014。

## Baseline

- 治理过渡branch/upstream：`codex/g05-g06-transition` / `origin/develop@c416dcdc40fcef2aef56627ab28c6f4049dc7dd9`；G06产品分支在过渡PR合并后从新的远端subject创建，并在首个checkpoint记录exact implementation baseline；
- G05 product / delivery receipt / integration：`363daed34d25b991ad9699a7381ac0d64e658e8b` / `4000c814973c16a13424b7294e3131743ed32ef7` / `c416dcdc40fcef2aef56627ab28c6f4049dc7dd9`；PR #18与远端CI均`PASS`；
- G01 source deletion fresh readback和现有auth/privacy threat model已在exact implementation baseline执行：18项定向测试`PASS`；
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

训练/eval consent范围、第三方共享、外部analytics/CRM、付费、新依赖、H1/商业/公网/生产/release/deploy/`main`需批准。

## Checkpoint ledger

| 时间 | 用户结果 | Commit | Verification | Evidence level | Product progress | Governance ratio | Remaining | Risk/failure | Next autonomous action |
|---|---|---|---|---|---|---|---|---|---|
| 2026-08-31 | G05三城有来源建议已并入`develop`并完整归档；G06显式记忆与分享合同原子激活，尚未修改G06产品代码或创建033 | 治理过渡commit在提交后由远端readback记录；精确产品基线`c416dcdc40fcef2aef56627ab28c6f4049dc7dd9` | G05回执tip CI `33389553342 PASS`；PR #18 `MERGED`；develop exact-tip GitHub Actions `33389970986 PASS`；fresh fetch、`rev-parse`与`ls-remote`一致 | `PRODUCT_DELIVERY_PASS / REMOTE_INTEGRATION_PASS / GOAL_TRANSITION` | `Product progress=NONE / GOAL_TRANSITION` | `Governance ratio=100% / atomic archive and activation only` | 合并本治理过渡PR；从新develop创建G06实现分支，先完成删除/auth/privacy preflight与consent合同，再进入033和产品切片 | G06产品、033、删除fresh readback、权限/分享、PostgreSQL、浏览器与交付Gate均`NOT_RUN`；G04两个精确历史例外仍须在G07移除 | 校验归档/绑定/范围，提交push并通过受保护PR；合并后fresh readback再建立唯一G06实现分支 |
| 2026-08-31 | 用户已可主动开启并查看、修改、清空结构化旅行偏好；产品反馈和训练/评测同意独立；登录行程可创建7天只读分享并撤销 | 待首个G06产品checkpoint提交；exact baseline `e383eeef39b0246ce35dd3cb8481a02bbebd1130` | 删除/auth/privacy preflight `18 passed`；G06定向`5 passed`；033 PostgreSQL fresh/after-034/运行时`3 passed`；browser `3 passed`；frontend build、OpenAPI/client build、Ruff、治理29项`PASS` | `AUTOMATED_TEST / LOCAL_PRODUCT_SLICE_PASS` | `Product progress=memory + consent + feedback + share end-to-end` | `Governance ratio=checkpoint binding only` | 提交并push subject，通过exact-tip CI后生成耐久`G06.product-delivery.json`，再次CI并受保护PR合入`develop` | 当前证据为本地fixture/自动化；远端CI、交付回执与集成尚`NOT_RUN`；G04两个精确历史例外保持不变并须在G07移除 | 完成最终diff审查、checkpoint push和exact-tip CI |

## Auto-advance

- Required gate：`Consent & Share Gate + PRODUCT_DELIVERY_PASS`；Next template：`TC-VNEXT-G07-CANDIDATE.md`；
- subject push/readback、耐久`PRODUCT_DELIVERY_PASS`、clean tree、无Stop后，最终归档，并原子更新Goal binding与work-package registry激活G07；不登记外部ledger、不预创建authority generation；H1/商业不自动启动。

## Completion record

- Status / Subject commits / Remote branch：`IN_PROGRESS / 首个G06产品checkpoint待提交 / origin/codex/g06-memory-share待首次push`；
- Verification / Evidence / Gate result / `structurally_valid`：`LOCAL_TARGETED_PASS / AUTOMATED_FIXTURE_AND_POSTGRESQL / PRODUCT_DELIVERY_NOT_RUN / true`；本地通过不代表远端产品交付；
- H1 / production / commercial：`NOT_RUN / NOT_RUN / NOT_RUN`；公网、release、deploy和`main`同样未运行或未请求；
- User-visible result / Remaining risks / Goal archived / Next activated：`显式结构化偏好、独立反馈/训练同意和可撤销只读分享已完成本地端到端实现 / 远端CI、耐久交付回执和develop集成仍未运行；G04两个历史例外保持原样 / false / false`；
- Promotion decision：`NOT_REQUESTED`。

## Stop conditions

- 无法保证删除或consent分离；
- 需要保存原文实现个性化；
- 需要实时协同扩大范围；
- 需要第三方账号、付费服务或外部数据共享；
- 分享无法避免越权或内部泄漏；
- fresh `origin/develop`不再由G06合法接棒，或激活必须修改产品字节。
