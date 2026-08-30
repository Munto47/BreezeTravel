# IN_PROGRESS GOAL：G03R 行程语义与地点解析 P1 返修

Goal ID: TC-VNEXT-G03-TOP3-AUDIT
Status: IN_PROGRESS
Goal type: BLOCKING_DEFECT

<!-- PRODUCT_DELIVERY_CURRENT_GOAL_STATE
{
  "schema_version": "product-delivery-current-goal-state-v1",
  "program_id": "TC-VNEXT-2026",
  "goal_id": "TC-VNEXT-G03-TOP3-AUDIT",
  "goal_status": "IN_PROGRESS",
  "gate_profile": "PRODUCT_DELIVERY_GATE",
  "required_gate": "Top-3 Audit Gate + PRODUCT_DELIVERY_PASS",
  "completion_status": "PENDING",
  "gate_result": "PRODUCT_DELIVERY_NOT_RUN",
  "goal_archived": false,
  "repair_slice_id": "G03R-SEMANTIC-PLACE-P1",
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
- Required gate：`Top-3 Audit Gate + PRODUCT_DELIVERY_PASS`
- Status：`IN_PROGRESS`
- Work kind：`BLOCKING_DEFECT / P1`
- Active slice：`G03R-SEMANTIC-PLACE-P1`
- Owner authorization：`OWNER_APPROVED_G03_P1_REPAIR_2026-08-30`
- Place authorization：`OWNER_APPROVED_G03_PLACE_REPAIR_2026-08-30`
- G04：`NOT_ACTIVATED`

## User Outcome

用户粘贴攻略后，逐日卡片只保留真正计划到访的原子地点，并保持原文日序与日内顺序。推荐、听说、经过、换乘、条件选择、明确排除、描述句、URL、电话、预约说明和元指令只留在内部语义记录或被跳过，不进入地点解析和公共行程卡片。

北京、上海、杭州的常见地点由版本化900条词典帮助形成准确检索词，但最终身份必须由实时高德结果确认；错城、错类别、行政区矛盾、同层多候选或Provider证据不足时宁可显示“地点待确认”，不得自动匹配错误地点。

## Blocking defect

- Reproduction：同一组`54 dev + 18 validation`非blind输入中，阿拉伯数字“第1天”使计划地点日序及顺序精确匹配只有`336/432`；四个实际`REFERENCE`出现位置丢失；本地回退会漏计划地点并产生描述型额外`PLANNED`。
- Place reproduction：现有三城解析没有版本化的安全别名与保守唯一候选层，错城、同名、分店/航站楼/校区和类别/行政区冲突无法形成统一的拒绝边界。
- Impact chain：错误角色或边界会把非行程内容送入POI解析与公共卡片，日标题缺口会把真实地点放入错误日期或顺序；地点消歧过宽会把错误POI直接展示给用户并污染路线准备。
- Minimum fix：只修复日序/原文顺序、五类角色、原子地点边界与统一资格判断，并增加三城版本化词典、私有加载器及必须经高德确认的保守唯一候选选择。
- Severity：`P1`，属于G03交付阻断缺陷；历史G03自动交付回执不适用于变更后的产品指纹。

## Dependencies

- Product baseline：现场最新`origin/develop@8a33a4b22a405135f310376d8766d9170d80097d`，已完成远端fetch与subject回读。
- 历史72条Qwen输出和历史人工裁决只作`DEVELOPMENT_DIAGNOSTIC`；不得称为当前Gate、blind、真人或生产证据。
- 正式72条比较仅允许复用既有已批准Qwen凭据，且不得打印或写入仓库；若执行环境仍未注入凭据，状态保持`BLOCKED_EXTERNAL`。
- 三城词典以Wikidata CC0记录作候选发现，并由政府、场馆/景区、交通运营方或品牌官方网站核验；不得以高德结果或模型猜测反向构造词典。
- 地点贡献包只运行离线测试；live AMap矩阵仅由主对话在精确clean候选、远端回读和零增量费用被证明后运行。
- 本返修不依赖G04，且不得创建G04分支、writer或产品代码。

## Scope

1. 日序与顺序：识别`第1天 / 第 2 天 / 第一天 / Day 3 / D4`，按最近前置标题确定日序；接受的mention按原文span排序并按日重建顺序。
2. 五类角色：按元说明跳过、无条件取消`EXCLUDED`、条件选择`OPTIONAL`、仅经过/换乘`PASS_THROUGH`、推荐/听说/非本次安排`REFERENCE`、明确到访`PLANNED`的局部优先级处理每个真实出现位置。
3. 原子边界与召回：仅保留原文逐字、URL外、字符合法的原子地点；本地回退按局部子句、动作锚点和并列连接词提取；描述、导航、电话、预约与元指令不得成为地点。
4. `pipeline.py`用同一个资格判断控制POI调用和公共卡片，只有带合法原子地点及日序的`PLANNED`可进入两者。
5. 新增独立回归`backend/tests/test_g03r_trip_semantics.py`并覆盖重复、重叠、URL同名、噪声、未知地点、五种标题、乱序模型输出及两类调用集合一致性。
6. 三城词典：新增版本化JSONL共900条，北京/上海/杭州各300条；每城210条景点/场馆/公园/街区、60条交通枢纽、15条餐饮、15条酒店，只保存最小来源化语义字段。
7. 私有加载与保守解析：城市内返回零/一/多匹配，只允许规范名、审核别名和完整后缀白名单三层精确等价；最终POI必须通过高德城市、行政区、类别及唯一性确认，否则保持待确认。
8. 新增独立地点词典与解析回归，覆盖配额、schema、来源、稳定排序、禁止字段、归一化正反例、错城/错类/行政区冲突、Provider失败和词典外实时搜索。

## Parallel work packages

- 主对话在`codex/g03r-activation`和`D:/munto/code/claudeProject/agentTravel-product-reset`中独占Goal、binding、registry、过渡校验、复核和串行集成。
- 语义贡献包`WP-G03R-SEMANTIC`使用`codex/g03r-semantic`和`D:/munto/code/claudeProject/agentTravel-g03r-semantic`，只修改登记的三个运行时文件、Qwen语义提示词和独立测试。
- 地点贡献包`WP-G03R-PLACE`使用`codex/g03r-place-resolution`和`D:/munto/code/claudeProject/agentTravel-g03r-place-resolution`，按“词典/私有加载器”和“高德保守解析/测试”两个串行提交，只修改登记的七条路径。
- 子Agent不拥有分支、提交或产品写入；本切片未授权运行时多Agent。

## Non-goals

- 不修改模型、exact snapshot、temperature、7秒deadline、768 output tokens、并发1、一次schema repair或失败策略；
- 不修改schema、公共OpenAPI、migration、依赖、数据库、锁文件或公共枚举；
- 不修改公共API、模型配置或语义文件来适配地点包；不把词典升级为POI事实库，不保存高德ID、坐标、地址、电话、营业时间、价格、评分、房态或原始响应；
- 不读取、修改或运行sealed blind/oracle，不概率性重跑Provider；
- 不激活G04、FUX-03、H1、公网、生产、商业、发布、部署或`main`合并；
- 不自行合并贡献分支，不把离线测试或历史输出称为当前正式72条Provider证据。

## Authority

- `AGENTS.md`、Project Charter、Trip Check Spec、v3 API Contract、Architecture；
- Program、Roadmap、Release Gates、Product Delivery Gate与Risk Register；
- ADR-007、ADR-008、ADR-011、ADR-012、ADR-013、ADR-014；
- 项目所有者本轮明确授权的G03R返修计划；
- 历史G03完成合同继续约束未被本P1返修替代的产品行为。

## Baseline

- Product baseline / upstream：`origin/develop@8a33a4b22a405135f310376d8766d9170d80097d`；
- 控制分支：`codex/g03r-activation`；语义与地点分支分别在各自完整prompt binding commit后创建；
- 固定比较集：`54 dev + 18 validation`，不读取blind；
- 基线诊断：结构化结果`72/72`、计划原子召回`432/432`、额外计划地点`0`、角色`716/720`、日序及顺序`336/432`；本地回退另有计划召回与额外描述地点缺陷；
- 当前终端Qwen凭据：`NOT_PRESENT`，正式新鲜72条比较为`NOT_RUN`。

## Invariants

- `ActivityMention`固定区分`PLANNED / OPTIONAL / REFERENCE / EXCLUDED / PASS_THROUGH`；只有合法原子地点的`PLANNED`可自动搜索POI并成为公共卡片。
- 同名地点每个真实出现位置独立判断，复合名称不能吞掉后续独立出现。
- 模型输出顺序不得覆盖原文span顺序；无标题时只为真正`PLANNED`使用Day 1软默认。
- 推荐内容保留为内部`REFERENCE`，不进入行程；URL、电话、预约、描述和元示例不得迁移成地点。
- 三城词典只辅助检索，不承担地点身份权威；高德未确认、同层不唯一、错城、错类别、行政区矛盾或字段不足一律保持待确认。
- 地名归一化必须保留数字、方向、分店、航站楼、校区和括号限定；不得删除`馆/院/店/站/园`等单字或使用模糊猜测消歧。
- 任何Provider超时、环境失败或不可比较结果记`UNKNOWN`，不得算语义成功或失败。
- G04保持`NOT_ACTIVATED`。

## Acceptance and Gate

每个质量因素必须在同一72条输入上独立比较；不满足门槛的提交创建可审计revert，最多两轮并换一种局部策略：

- 描述、URL、预约或其他禁入内容成为地点：`0`；
- 额外`PLANNED`地点：`0`；
- 真正计划地点原子召回：`432/432`；
- 五类地点角色最终精确匹配：`720/720`；
- 计划地点日序及日内顺序最终精确匹配：`432/432`；
- 72条全部得到可比较结构化结果；外部失败保持`UNKNOWN`。
- 三城词典总量`900`且每城`210/60/15/15`配额、字段白名单、来源、稳定ID/排序和禁止字段测试全部通过；
- 地点解析严重自动误配`0`；词典命中但未获高德确认、同层多候选、错城/错类别/行政区冲突和Provider失败全部保持待确认。

正式G03产品交付状态仍是`PRODUCT_DELIVERY_NOT_RUN`；只有贡献包被主对话验收并串行集成、受影响G03验证和当前产品回执全部通过后，才能重新进入`CORE_MVP_OWNER_REVIEW_PENDING`。

## Verification

- `python -m pytest tests/test_g03r_trip_semantics.py tests/test_qwen_trip_understanding.py tests/test_trip_understanding_v3.py -q`；
- `python -m evals.trip_text_cards_v1.validator`；
- 每个单因素一次固定72条Qwen比较；无凭据则`NOT_RUN / BLOCKED_EXTERNAL`；
- `python -m pytest tests/test_g03r_place_lexicon.py tests/test_g03r_place_resolution.py tests/test_amap_trip_understanding.py -q`；
- 地点贡献包只跑离线回归；主对话在精确提交远端回读后决定是否运行一次live AMap矩阵；
- `python -m scripts.validate_core_mainline`；
- 主对话另跑治理定向测试、路径复核、clean worktree、远端tip readback；
- sealed blind、H1、公网、生产、商业：`NOT_RUN`。

## Budget

- 模型、temperature、7秒deadline、768 output tokens、并发1、最多一次schema repair保持字节绑定不变；
- 每因素只允许一次固定72条比较，环境失败不以概率性重跑替代；
- 地点贡献包live AMap预算为`0`；主对话后续矩阵最多2500次，且仅在现有账户配额内零增量费用被证明后执行；
- 最多两轮修复复审；不新增Provider、账号、费用、数据或依赖；
- 只做登记的最小语义运行时/提示词/测试，以及三城词典、私有加载器、两处地点运行时和独立测试改动。

## HITL

既有已批准凭据若未注入，正式72条比较需要外部凭据运行环境恢复；不得要求用户把密钥粘贴到对话。高德live矩阵若不能证明零增量费用或现有配额/权限，保持`NOT_RUN / BLOCKED_EXTERNAL`。新账号、费用、Provider、blind、H1、公网、生产、发布、部署和`main`仍分别需要人工批准。

## Stop conditions

- 需要改变模型、deadline、token、temperature、并发、retry、schema、blind/oracle或公共API；
- 需要新增migration、依赖、数据权限或运行时多Agent；
- 需要以高德结果/模型猜测构造词典、扩大词典字段、修改公共API/数据库/模型配置/语义文件，或无法证明live AMap零增量费用；
- 需要激活G04或降低任何验收门槛；
- 两轮不同局部策略后仍无法同时消除额外计划地点并保持全部真实计划地点。

## Checkpoint ledger

| 时间 | 用户结果 | Commit | Verification | Evidence level | Product progress | Governance ratio | Remaining | Risk/failure | Next autonomous action |
|---|---|---|---|---|---|---|---|---|---|
| 2026-08-30 | 所有者授权G03 P1语义返修；G04继续未激活；尚未修改语义运行时 | activation pending | 远端`origin/develop@8a33a4b`回读；治理基线validator PASS；治理定向测试24 PASS | `LOCAL_AUTOMATED / DEVELOPMENT_DIAGNOSTIC` | `Product progress=NONE / GOAL_TRANSITION` | `Governance ratio=100% / activation only` | prompt绑定、三个单因素、离线回归、正式72条 | 当前终端无Qwen凭据，正式72条可能`BLOCKED_EXTERNAL` | 提交激活点，登记prompt和独立语义工作树 |
| 2026-08-30 | G03R控制面已激活，语义贡献包已运行；现登记三城900地点词典与保守高德解析包，地点产品代码尚未修改 | activation `d4ce966de55b6d72e0b5daced9764223d7a6913a`；place binding以本次远端subject回读为准 | core-mainline、治理定向测试、prompt完整性/哈希与registry一致性由本控制切片复核 | `LOCAL_AUTOMATED / CONTROL_PLANE` | `Product progress=NONE / GOAL_TRANSITION / GOVERNANCE_SCOPE_GUARD` | `Governance ratio=100% / semantic + place prompt binding` | 地点分支/工作树、两个串行提交、离线回归、正式语义72条 | 词典来源需逐条复核；Qwen凭据未注入；live AMap仅主对话且受零增量费用边界约束 | 推送place binding并从该提交创建`codex/g03r-place-resolution` |
| 2026-08-30 | 已把语义提示词纳入所有者授权的G03 P1返修边界；schema、配置和其他冻结评测资产仍被拒绝 | 本控制checkpoint commit | 治理定向测试24 PASS；精确提示词路径PASS；相邻schema路径同时触发两项冻结错误 | `LOCAL_AUTOMATED / CONTROL_PLANE` | `Product progress=NONE / GOAL_TRANSITION / GOVERNANCE_SCOPE_GUARD` | `Governance ratio=100% / exact-path exception` | 从新控制点重建语义贡献分支、重跑语义回归及core-mainline、正式72条 | Qwen凭据未注入，正式72条仍可能`BLOCKED_EXTERNAL` | 推送控制checkpoint并回读，然后无历史改写地重建`codex/g03r-semantic` |

## Auto-advance

- 自动推进G04：`DISABLED`；G04：`NOT_ACTIVATED`；
- 不得自动激活G04；只有本P1返修复核完成后回到所有者体验验收点；
- 语义与地点贡献分支只commit/push并请求验收，不自行合并；
- 主对话按`WP-G03R-SEMANTIC → WP-G03R-PLACE`验收路径、tip、clean、比较与测试后才可串行集成；
- 受影响G03产品验证完成后回到`CORE_MVP_OWNER_REVIEW_PENDING`，不激活G04。

## Completion record

- Status：`IN_PROGRESS`；Goal archived：`NO`；
- Product result / current delivery Gate：`PENDING / PRODUCT_DELIVERY_NOT_RUN`；
- Contribution packages / final commits / remote readback：`WP-G03R-SEMANTIC + WP-G03R-PLACE / PENDING / NOT_RUN`；
- Fresh 72-case Qwen comparison：`NOT_RUN`；当前无凭据时最终状态必须为`BLOCKED_EXTERNAL`；
- FUX-03 / H1 / public network / production / commercial：`NOT_RUN / NOT_RUN / NOT_RUN / NOT_RUN / NOT_RUN`；
- Release / deployment / main merge：`NOT_REQUESTED / NOT_REQUESTED / NOT_REQUESTED`；
- Next activated：`NO`，G04保持`NOT_ACTIVATED`。
