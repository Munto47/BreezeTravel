# OWNER REVIEW HOLD：G01～G03 核心主线返修后可体验里程碑

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

G01～G03核心主线与本轮P1返修已经进入`develop`。用户粘贴攻略后，可得到只保留真正计划地点的逐日卡片；北京、上海、杭州采用保守地点匹配，证据不足时显示“地点待确认”。结果页可查看和手动更新地图、选择整程住宿、查看Top-3，并用桌面拖拽、键盘或移动端等价操作编辑；失败和409以服务端权威结果复原。

项目现在停在所有者体验验收点。所有者没有批准进入G04前，不开发截图增强，不创建G04分支或writer，也不把自动化结果表述为真人、生产或商业证据。

## Delivered scope

- G01：文本理解、地点解析、逐日卡片与首次地图任务；
- G02：地图剧场、步行/公交、手动更新、最多三家整程住宿与同店锚点；
- G03：无日期物化、lineage、冻结证据、AuditEngine全量Finding、公开Top-3、preview/adopt、新revision与完整postcheck；
- G03R语义：过滤推荐、经过、排除、描述、URL与预约噪声，保持原子地点、日序和日内顺序；
- G03R地点：版本化三城900地点词典与保守Provider消歧，错城、错类、行政区冲突和同层歧义不自动匹配；
- G03R结果页：拖拽、键盘/移动等价操作、删除确认、焦点恢复、409回读，以及可取消、单飞、有界的地图/住宿增强读取；
- 公开接口和DOM保持脱敏，卡片编辑、住宿选择和修复采纳均不自动调用路线Provider。

## Acceptance state

- G01 Text Card Gate：`PRODUCT_DELIVERY_PASS / INTEGRATED`；
- G02 Map & Stay Gate：`PRODUCT_DELIVERY_PASS / INTEGRATED`；
- G03 Top-3 Audit Gate及G03R返修：`PRODUCT_DELIVERY_PASS / INTEGRATED`；
- G04：`NOT_ACTIVATED`；
- H1、公网、生产、商业：`NOT_RUN`；FUX-03、sealed blind、live AMap：`NOT_RUN`；
- release、部署、合并`main`：`NOT_REQUESTED`。

## Authority

- `AGENTS.md`、Project Charter、Trip Check Spec、v3 API Contract与Architecture；
- Program、Roadmap、Release Gates、Product Delivery Gate和Risk Register；
- 已归档的G01、G02、G03完整合同，以及G03R完成附录与当前指纹绑定交付回执。

## Baseline and integration

- G03R activation baseline：`origin/develop@8a33a4b22a405135f310376d8766d9170d80097d`；
- G03R delivery checkpoint：`2d74a88ffd97988f50fbaa271b5d66f7411c2155`；
- G03R product fingerprint：`aa76dbafb3fe48d28f4e54efcbac1530aa2bf0c706c86a6e8facbf6d7b5079ee`；
- PR #13 integration：`origin/develop@1f5a93c62aeeefc83486778d446780710977529d`；
- GitHub `core-mainline` run `33307676730 PASS`，subject祖先与远端tip回读`PASS`。

## Invariants

- `CURRENT_GOAL.md`在本状态下不包含活动产品Goal；G04合同继续保持`DRAFT`；
- `current_work_packages.json`只保留一个已合并的G03R历史包，`writer_activation=NONE`；
- G03交付回执继续绑定当前产品指纹，治理收口不改动产品运行时；
- live AMap因零增量额度未证明保持`NOT_RUN / 0 calls`，不得外推为Provider实测；
- owner review不是H1、发布、生产、商业或`main`合并授权；未经所有者明确批准不得推进。

## Work packages

语义、地点和UI贡献包已由唯一主集成串行验收。最终集成包`WP-G03R-INTEGRATOR`的ready commit为`2d74a88ffd97988f50fbaa271b5d66f7411c2155`，merge commit为`1f5a93c62aeeefc83486778d446780710977529d`。当前活动writer数量为0，没有预备或隐藏的G04包。

## Non-goals

- 不激活G04～G07，不启动FUX-03或H1真人研究；
- 不运行live AMap、公网、生产或商业验证；
- 不发布、不部署、不合并`main`；
- 不新增媒体代理、公共API、migration、依赖、额外治理或运行时多Agent体系。

## Verification

- 语义、地点与既有v3组合回归：`131 PASS`；G03 Top-3：`3 PASS`；
- G03 PostgreSQL隔离链：`1 PASS`；固定数据：`90 VALID / blind Gate NOT_RUN`；
- frontend production build、trip-check client build、OpenAPI contract：`PASS`；
- 结果页E2E：`29/29`；默认3 workers、retries=0的repeat3：`87/87`；
- 清空真实Provider密钥的local fixture完整主链：`1/1 PASS`；
- GitHub PR #13 `core-mainline` run `33307676730 PASS`，PR `MERGED`；
- 当前治理过渡只运行治理定向测试、核心主线validator和交付回执回读。

## Budget and HITL

本状态不授权产品开发、Provider调用、外部费用、数据扩大或部署。只有项目所有者完成体验验收并明确批准进入G04，才可从届时最新`origin/develop`建立精确基线并原子激活完整G04合同。FUX-03/H1、付费Provider、公网、生产、发布、部署和`main`仍分别需要独立批准。

## Checkpoint ledger

| 时间 | 用户结果 | Commit | Verification | Evidence level | Remaining | Risk/failure | Next action |
|---|---|---|---|---|---|---|---|
| 2026-08-30 | G03R准确语义、保守地点和稳定结果页已合入`develop`；项目停在所有者体验验收点，G04未激活 | delivery `2d74a88f`；integration `1f5a93c6` | local全矩阵PASS；GitHub run `33307676730 PASS`；PR #13 MERGED；远端祖先回读PASS | `LOCAL_AUTOMATED / POSTGRESQL_INTEGRATION / LOCAL_BROWSER / REMOTE_AUTOMATED` | `Product progress=NONE / GOAL_TRANSITION`；`Governance ratio=100% / terminal transition only` | 所有者体验验收；如未来批准，再激活G04 | live AMap、sealed blind、FUX-03/H1、公网、生产、商业均未运行 | 保持`CORE_MVP_OWNER_REVIEW_PENDING`，等待所有者明确决策 |

## Auto-advance

- 自动推进：`DISABLED`；G04：`NOT_ACTIVATED`；writer activation：`NONE`；
- FUX-03/H1、公网、生产、商业：`NOT_RUN`；
- release、部署、合并`main`：`NOT_REQUESTED`。

## Completion record

- Last completed Goal：`TC-VNEXT-G03-TOP3-AUDIT / G03R COMPLETED / ARCHIVED`；
- Gate result：`PRODUCT_DELIVERY_PASS`；integration：`1f5a93c62aeeefc83486778d446780710977529d`；
- User-visible result：准确卡片、保守地点、地图、住宿、Top-3、稳定编辑和完整复检主链均可运行；
- Remaining：项目所有者体验验收，不属于自动化工程PASS；
- H1 / public network / production / commercial：`NOT_RUN / NOT_RUN / NOT_RUN / NOT_RUN`；
- release / deployment / main merge：`NOT_REQUESTED / NOT_REQUESTED / NOT_REQUESTED`；
- Promotion decision：`NOT_REQUESTED`；`structurally_valid=true`只表示终态合同结构有效。

## Resume conditions

项目所有者若明确批准进入G04，新的治理过渡必须从当时最新`origin/develop`开始，记录体验验收边界，原子激活完整G04合同与唯一writer；否则本状态持续保持。
