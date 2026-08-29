# OWNER REVIEW HOLD：G01～G03 核心主线可体验里程碑

Goal ID: CORE_MVP_OWNER_REVIEW_PENDING
Status: OWNER_REVIEW_PENDING
Goal type: OWNER_REVIEW_HOLD

<!-- PRODUCT_DELIVERY_CURRENT_GOAL_STATE
{
  "schema_version": "product-delivery-current-goal-state-v1",
  "program_id": "TC-VNEXT-2026",
  "goal_id": "CORE_MVP_OWNER_REVIEW_PENDING",
  "goal_status": "OWNER_REVIEW_PENDING",
  "gate_profile": "PRODUCT_DELIVERY_GATE",
  "required_gate": "G03 Top-3 Audit Gate + PRODUCT_DELIVERY_PASS; owner experience review before G04",
  "completion_status": "DELIVERY_INTEGRATED",
  "gate_result": "PRODUCT_DELIVERY_PASS",
  "goal_archived": true,
  "last_completed_goal_id": "TC-VNEXT-G03-TOP3-AUDIT",
  "next_goal_id": "TC-VNEXT-G04-SCREENSHOT",
  "next_activated": false,
  "g04_status": "NOT_ACTIVATED",
  "fux03_status": "NOT_RUN",
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

- Program ID：`TC-VNEXT-2026`
- Mainline phase：`CORE_MVP`
- Gate profile：`PRODUCT_DELIVERY_GATE`
- Required gate：`G03 Top-3 Audit Gate + PRODUCT_DELIVERY_PASS; owner experience review before G04`
- Status：`OWNER_REVIEW_PENDING`
- Last completed Goal：`TC-VNEXT-G03-TOP3-AUDIT`
- Next planned Goal：`TC-VNEXT-G04-SCREENSHOT`
- G04：`NOT_ACTIVATED`
- 当前没有`APPROVED`或`IN_PROGRESS`产品Goal，也没有活动产品writer。

## Owner outcome

G01～G03核心主线已形成可运行的完整体验：用户粘贴攻略后得到逐日卡片和真实地点，查看步行/公交地图与整程住宿建议，再查看最多三项重要检查，预览并采纳最小改动，最后只在完整复检后看到新结果。

项目现在停在所有者体验验收点。所有者没有批准进入G04前，不开发截图增强，不创建G04分支或writer，也不把自动化结果表述为真人、生产或商业证据。

## Delivered scope

- G01：文本理解、地点解析、逐日卡片与首次地图任务；
- G02：地图剧场、步行/公交、手动更新、最多三家整程住宿与同店锚点；
- G03：无日期物化、lineage、冻结证据、AuditEngine全量Finding、公开Top-3、preview/adopt、新revision与完整postcheck；
- 公开接口和DOM保持脱敏，卡片编辑、住宿选择和修复采纳均不自动调用路线Provider；
- PostgreSQL、前端构建和浏览器主链已在G03产品PR的远端检查中通过。

## Acceptance state

- G01 Text Card Gate：`PRODUCT_DELIVERY_PASS / INTEGRATED`；
- G02 Map & Stay Gate：`PRODUCT_DELIVERY_PASS / INTEGRATED`；
- G03 Top-3 Audit Gate：`PRODUCT_DELIVERY_PASS / INTEGRATED`；
- G04：`NOT_ACTIVATED`；
- FUX-03/H1真人、公网、生产、商业：`NOT_RUN`；
- release、部署、合并`main`：`NOT_REQUESTED`。

## Authority

- `AGENTS.md`、Project Charter、Trip Check Spec、v3 API Contract与Architecture；
- Program、Roadmap、Release Gates、Product Delivery Gate和Risk Register；
- ADR-007、ADR-008、ADR-011、ADR-012、ADR-013、ADR-014；
- 已归档的G01、G02、G03完整合同及其指纹绑定交付回执。

## Baseline

- G03 activation baseline：`origin/develop@5a79c129f13914f4cd5a01789e641ada56d0b486`；
- G03 product commit：`68f7f9e01f0b5c5ecfa8b6286bf57394838b0c37`；
- G03 delivery commit：`3009372c07fd5b82a76655b2df5c831f4698ec93`；
- G03 product fingerprint：`0c09093bfa1eca32942bf68d3b5d665d0470604139d48968e127ff2cad9c54a1`；
- G03 product PR #11 integration：`origin/develop@3999ee583faf4c14ff5c16e2d573bf5cc12689cb`；
- GitHub `core-mainline` run `33269598302 PASS`。

## Invariants

- `CURRENT_GOAL.md`在本状态下不包含活动产品Goal；G04合同继续保持`DRAFT`；
- `current_work_packages.json`只保留一个已合并的G03历史包，`writer_activation=NONE`；
- G03交付回执必须继续绑定当前产品指纹，治理收口不得改动产品运行时；
- owner review不是H1、发布、生产、商业或`main`合并授权；
- 未经所有者新的明确批准，不得从本状态自动推进。

## Work packages

唯一`WP-G03-INTEGRATOR`已在产品PR #11中合并：ready commit为`3009372c07fd5b82a76655b2df5c831f4698ec93`，merge commit为`3999ee583faf4c14ff5c16e2d573bf5cc12689cb`。当前活动writer数量为0，没有预备或隐藏的G04包。

## Non-goals

- 不激活G04～G07；
- 不启动FUX-03或H1真人研究；
- 不运行公网、生产或商业验证；
- 不发布、不部署、不合并`main`；
- 不建设额外治理、候选审查或运行时多Agent体系。

## Verification

- G03 targeted与既有v3回归：`16 PASS`；
- G03 PostgreSQL及fresh/existing migration：`3 PASS`；
- frontend production build、trip-check client build、OpenAPI contract：`PASS`；
- G03 Playwright完整主链：`1 PASS`；
- GitHub PR #11 `core-mainline`：`PASS`；
- 当前治理过渡仅运行治理定向测试、核心主线validator和交付回执回读；
- H1、公网、生产、商业：`NOT_RUN`。

## Budget

本状态不授权产品开发、Provider调用、外部费用、数据扩大或部署。后续如获准激活G04，应从届时现场`origin/develop`重新建立精确基线，并只执行G04最小充分验证。

## HITL

只有项目所有者完成体验验收并明确批准进入G04，才可把planned G04合同切换为`APPROVED`。FUX-03/H1、付费Provider、公网、生产、发布、部署和`main`仍分别需要独立批准。

## Checkpoint ledger

| 时间 | 用户结果 | Commit | Verification | Evidence level | Product progress | Governance ratio | Remaining | Risk/failure | Next action |
|---|---|---|---|---|---|---|---|---|---|
| 2026-08-30 | G01～G03主链已集成并停在所有者体验验收点；没有激活G04或新writer | G03 delivery `3009372c`；integration `3999ee58` | GitHub run `33269598302 PASS`；G03 receipt与远端subject readback `PASS` | `CONTROLLED_FIXTURE / REAL_POSTGRESQL / LOCAL_BROWSER / REMOTE_AUTOMATED` | `Product progress=NONE / GOAL_TRANSITION` | `Governance ratio=100% / terminal transition only` | 所有者体验验收；如未来批准，再激活G04 | FUX-03/H1、公网、生产、商业均未运行；release、部署、main均未请求 | 保持`CORE_MVP_OWNER_REVIEW_PENDING`，等待所有者未来明确决策 |

## Auto-advance

- 自动推进：`DISABLED`；
- G04：`NOT_ACTIVATED`；
- writer activation：`NONE`；
- FUX-03/H1、公网、生产、商业：`NOT_RUN`；
- release、部署、合并`main`：`NOT_REQUESTED`。

## Completion record

- Last completed Goal：`TC-VNEXT-G03-TOP3-AUDIT / COMPLETED / ARCHIVED`；
- Gate result：`PRODUCT_DELIVERY_PASS`；integration：`3999ee583faf4c14ff5c16e2d573bf5cc12689cb`；
- User-visible result：卡片、地图、住宿、Top-3检查、最小改动和完整复检主链均可运行；
- Remaining：项目所有者体验验收，不属于自动化工程PASS；
- H1 / public network / production / commercial：`NOT_RUN / NOT_RUN / NOT_RUN / NOT_RUN`；
- release / deployment / main merge：`NOT_REQUESTED / NOT_REQUESTED / NOT_REQUESTED`；
- Promotion decision：`NOT_REQUESTED`；`structurally_valid=true`只表示终态合同结构有效。

## Resume conditions

项目所有者若明确批准进入G04，新的治理过渡必须从当时最新`origin/develop`开始，记录体验验收边界，原子激活完整G04合同与唯一writer；否则本状态持续保持。
