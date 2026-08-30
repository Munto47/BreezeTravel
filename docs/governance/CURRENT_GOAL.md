# IN_PROGRESS GOAL：V0.4 截图与文本一致

Goal ID: TC-VNEXT-G04-SCREENSHOT
Status: IN_PROGRESS
Goal type: PRODUCT_ENHANCEMENT

<!-- PRODUCT_DELIVERY_CURRENT_GOAL_STATE
{
  "schema_version": "product-delivery-current-goal-state-v1",
  "program_id": "TC-VNEXT-2026",
  "goal_id": "TC-VNEXT-G04-SCREENSHOT",
  "goal_status": "IN_PROGRESS",
  "gate_profile": "PRODUCT_DELIVERY_GATE",
  "required_gate": "Screenshot Parity Gate + PRODUCT_DELIVERY_PASS",
  "completion_status": "PENDING",
  "gate_result": "PRODUCT_DELIVERY_NOT_RUN",
  "goal_archived": false,
  "last_completed_goal_id": "TC-VNEXT-G03-TOP3-AUDIT",
  "next_goal_id": "TC-VNEXT-G05-CITY-KNOWLEDGE",
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

- Program ID：`TC-VNEXT-2026`
- Product version：`V0.4`
- Mainline phase：`PRODUCT_ENHANCEMENT`
- Gate profile：`PRODUCT_DELIVERY_GATE`
- Required gate：`Screenshot Parity Gate + PRODUCT_DELIVERY_PASS`
- Status：`IN_PROGRESS`
- Product baseline：`origin/develop@0531c0642f437932fb4e305a0a99fbb66b19e4bc`
- Activation branch：`codex/g04-activation`
- Canonical implementation branch：`codex/g04-screenshot-integration`
- Integration worktree：`D:/munto/code/claudeProject/agentTravel-g04-integration`
- Upstream / remote readback：`origin/develop` / `0531c0642f437932fb4e305a0a99fbb66b19e4bc`，2026-08-30 fresh fetch与`ls-remote`一致
- Predecessor：G03/G03R已交付并归档；owner-review closure为`0531c0642f437932fb4e305a0a99fbb66b19e4bc`
- Next Goal：`TC-VNEXT-G05-CITY-KNOWLEDGE`

## Dependencies

- G03 Top-3 Audit Gate、G03R返修和`PRODUCT_DELIVERY_PASS`已经合入`develop`。
- 项目所有者于2026-08-30明确要求“开始G04”，该指令解除`CORE_MVP_OWNER_REVIEW_PENDING`。
- 项目所有者同时明确批准G04所需的一份追加式PostgreSQL migration；编号冻结为`034_trip_understanding_screenshot_batches.sql`，保留G05/G06既定的032/033。
- 当前统一语义编译、加密SourceDocument与主动删除能力存在；截图v3批次、到期物理清理和截图输入UI尚未实现。
- Paddle baseline冻结为`paddleocr==3.7.0`、`paddlepaddle==3.3.1`；候选本地硬件为Ryzen 9 7945HX、32 logical processors、RTX 4060 Laptop 8GB，正式性能RunSpec在Gate前记录可用CPU/GPU/内存。
- 当前没有可回读的Qwen-VL exact account/region/model binding，因此默认为`NOT_RUN_NO_EXACT_BINDING`，不得调用或静默fallback。

## User Outcome

登录用户上传最多6张聊天、备忘录、攻略或AI回复截图，不填写前置表单，即可得到与粘贴文本相同的逐日卡片、地图准备、住宿和Top-3核验体验；原始像素在任何终态都不会长期保留。

## Scope

- 登录态multipart `POST /api/v3/screenshot-batches`，1～6张PNG/JPEG/WebP、单张不超过10MB、总体不超过61MiB，禁止Base64 JSON；
- owner-bound、短期、不透明且数据库只存摘要的`batch_ref`；
- PaddleOCR 3.x、确定性阅读顺序、bbox/span与低置信确认映射；
- 加密`ScreenshotSourceDocumentV1`及一次性原子消费；
- `POST /api/v3/trip-understandings`增加`FULL + SCREENSHOT_BATCH`严格union，并复用同一TripUnderstanding语义编译器和用户投影；
- 成功、失败、取消、超时、TTL的原图清理receipt，以及SourceDocument 30天到期物理清理；
- 首页多图选择、排序、删除、partial/重试/刷新/断线体验；
- Qwen-VL只作有资格时的冻结消融，不胜出不进入运行时；
- 当前Goal所需的PostgreSQL、build、browser、隐私与parity验证。

## Public and internal contracts locked at activation

- `POST /api/v3/screenshot-batches`要求登录与`Idempotency-Key`，文件字段固定为重复的`screenshots`；清理全部成功后才返回`201 {batch_ref, expires_at, outcome: COMPLETE|PARTIAL, message}`和`Cache-Control: no-store`。
- `POST /api/v3/trip-understandings`新增`{"mode":"FULL","source":{"type":"SCREENSHOT_BATCH","batch_ref":"..."}}`；同一幂等键重放原202，不同键重复消费409，不存在和跨账号统一404，owner已过期410。
- 公开结果、SSE、DOM和日志不得返回batch ref、OCR原文、bbox、confidence、source span、hash、receipt、模型或Provider。
- 内部batch状态固定为`PROCESSING / READY / PARTIAL / CONSUMED / FAILED / CANCELLED / TIMED_OUT / EXPIRED / PRIVACY_BLOCKED`；内部清理失败不得伪装成功。
- `ScreenshotSourceDocumentV1`包含图片顺序/结果、OCR行、四点bbox、confidence、reading index、Unicode code-point半开span、`requires_confirmation`、engine/config binding和document hash，整体加密。
- batch未消费TTL 15分钟；OCR并发1、单图deadline 15秒、整批45秒；低置信阈值0.85；立即清理最多3次，失败公开为`503 SCREENSHOT_CLEANUP_RETRY_REQUIRED`且不返回ref。
- 与低置信span相交的可执行地点不得查询POI，只能显示“地点待确认”；单图失败保留其他成功来源，全部无文本返回`422 SCREENSHOT_TEXT_NOT_FOUND`。

## Parallel work packages

| Package | Branch / worktree | Owned outcome | Initial state |
|---|---|---|---|
| `WP-G04-INTEGRATOR` | `codex/g04-screenshot-integration` / `...-g04-integration` | governance、034、共享API/持久化/worker、OpenAPI/client、首页装配、CI、最终E2E | `IN_PROGRESS` |
| `WP-G04-EPHEMERAL-UPLOAD` | `codex/g04-ephemeral-upload` / `...-g04-upload` | 有界multipart、临时文件、终态清理 | `MERGED`（`9403a989`） |
| `WP-G04-PADDLE-OCR` | `codex/g04-paddle-ocr` / `...-g04-ocr` | Paddle、阅读顺序、bbox/span、评测runner | `MERGED`（`f1c75d95`） |
| `WP-G04-VL-PARITY` | `codex/g04-vl-parity` / `...-g04-vl-parity` | VL消融合同和隔离截图输入组件 | `MERGED`（`206312c0`） |

主对话是唯一集成者。三个长期贡献包必须使用用户可见独立任务、独立branch/worktree和完整prompt hash；子Agent只可只读标注、复核或诊断。集成者加最多两个贡献writer同时活动；任一前两包经集成者验收冻结后才启动VL/UI包。贡献包不得改治理、migration、共享OpenAPI/生成物或锁文件，不得自行合并。集成顺序固定为OCR→上传/清理→共享持久化/API→VL/UI→OpenAPI/client/homepage→E2E，每个问题最多两轮修复复审。

## Invariants

- 文本与截图共用同一语义编译和严格用户投影，不形成第二套业务逻辑。
- PostgreSQL是batch、幂等、消费、receipt和SourceDocument权威；原始像素不进入数据库、日志或Git。
- 只有全部临时像素确认删除后才能向用户返回可消费ref；清理失败为内部`PRIVACY_BLOCKED`。
- OCR证据只在加密SourceDocument中保存，最长30天或主动删除，以先到者为准；到期必须物理清空并回读receipt。
- 低置信地点宁可待确认也不得误匹配；错城、错类别、描述句/URL成为地点为零容忍。
- PaddleOCR是默认baseline；Qwen-VL无资格或不胜出时保持`NOT_RUN/EXPERIMENT`，不作fallback。
- fixture、synthetic、自动化、Agent转写、真人、生产证据严格分层。

## Acceptance / Gate

- 格式、数量、大小、顺序正确；JSON Base64成功数0；跨账号、过期、重复终态消费成功数0；
- 许可清晰真实来源OCR关键字段F1≥95%；低置信关键字段确认召回100%；reading-order adjacency-F1≥97%；
- 同源paired set上截图端到端可执行地点precision和recall较文本各下降≤1个百分点；严重错城、错类、整句地点为0；
- 三张1080×1920图片在冻结环境、并发1、2次预热后20次测量P95≤12秒；
- 原图、OCR证据和内部字段泄漏0；成功、失败、取消、超时、TTL清理receipt 100%；
- partial图片不抹掉成功来源；Qwen-VL若晋级，所有指标不差且至少一项错误率相对下降≥20%；
- `core_mainline_contract / g04_screenshot_targeted / g04_postgresql / frontend_build / g04_browser_e2e`全部PASS，并生成耐久`PRODUCT_DELIVERY_PASS`。

## Verification

- upload/OCR/API/pipeline定向测试及旧截图、既有v3回归；
- fresh PostgreSQL和既有031升级、状态约束、原子消费、并发单胜者、主动删除、TTL物理清理；
- Ruff、backend full suite、OpenAPI导出、shared client generate/typecheck/build、frontend build；
- Playwright登录、多图顺序/删除、partial、刷新、断线、卡片/地图/住宿/Top-3与DOM隐私；
- 许可清晰真实来源与SYNTHETIC格式集分层；两个隔离Agent转写与一个新裁决仅记`MULTI_AGENT_SIMULATED_REVIEW`；
- sequence 4的`g04_screenshot_targeted / g04_postgresql / frontend_build / g04_browser_e2e`是四个独立CI job；其中OCR固定为fixture，只证明自动回归，不生成或替代真实Paddle截图一致性证据；
- `PRODUCT_DELIVERY_PASS`必须回读已纳入Git的脱敏正式receipt，并同时校验receipt文件hash、候选commit/tree、跨平台稳定product fingerprint、Paddle 3.7.0/PaddlePaddle 3.3.1、hardware hash、冻结oracle/runner/scorer hash及3图并发1的2次预热+20次测量；缺失、过期、空分母、循环oracle、`NOT_EVALUABLE`或非PASS均失败关闭；
- 真实许可原图只可位于Git外临时证据目录；正式receipt仅保存许可清单与清理回执hash，不保存原图、OCR原文、bbox、置信度或本机路径；
- H1、公网、生产、商业：`NOT_RUN`。

## Authority

- 权威继承`AGENTS.md`、Charter、Trip Check Spec、v3 API Contract、Architecture、Program、Roadmap、Release Gates、Product Delivery Gate、Provider Admission、Risk Register及ADR-007、ADR-009、ADR-011、ADR-012、ADR-013、ADR-014。
- 本轮Owner批准：激活G04、上述追加v3截图API、`034_trip_understanding_screenshot_batches.sql`、独立功能任务/worktree、离线/本地自动验证、checkpoint commit/push和受保护`develop` PR。
- 未授权：新账号/费用、对象存储、付费OCR、扩大外部数据、真人招募/consent、sealed blind修改、H1、公网、生产、release/deploy和`main`合并。

## Non-goals

- 视频、PDF、网页抓取、手写识别承诺或恢复旧房间截图入口；
- 新对象存储、付费OCR、长期原图留存、运行时多Agent；
- 用synthetic或Agent转写冒充真实来源、真人或生产证据；
- 修改sealed文本blind/oracle，或提前实施G05/G06/G07。

## Budget

- 上传预算：1～6张、单张≤10MB、总体≤61MiB；TTL 15分钟；OCR并发1、15秒/图、45秒/批；
- 性能测量：3×1080×1920，2次预热、20次测量；
- 不新增依赖、对象存储、账号或费用；每个可回滚切片checkpoint并远端readback；
- 每包最多两轮修复复审，失败时采用可靠保守降级或由集成者接管。

## HITL

只有新增真人来源/consent、扩大数据或Provider权限、新账号/费用、对象存储/付费OCR、降低Gate、H1/公网/生产/release/deploy/`main`时再次请求Owner。普通代码、测试、构建、数据许可查验和Provider未准入由当前Goal继续处理或诚实标记`NOT_RUN`。

## Checkpoint ledger

| 时间 | 用户结果 | Commit | Verification | Evidence level | Product progress | Governance ratio | Remaining | Risk/failure | Next autonomous action |
|---|---|---|---|---|---|---|---|---|---|
| 2026-08-30 | Owner已批准截图入口；G04完整合同、034授权和唯一集成者从fresh develop激活 | activation commit待提交 | fresh fetch/ls-remote一致；根工作树和历史脏工作树保持不动；Paddle依赖/硬件readback | `LOCAL_READBACK / OWNER_AUTHORIZATION` | `Product progress=NONE / GOAL_TRANSITION` | `Governance ratio=100% / authorized activation only` | 提示词/任务绑定、三个贡献包、产品实现与Gate | 当前无eligible Qwen-VL binding；真实来源截图集待许可清晰化 | push/readback激活提交并经PR合入develop，再绑定三个用户可见任务 |
| 2026-08-30 | 三个用户可见功能任务已创建并绑定独立branch/worktree；Upload与OCR可在远端绑定后启动，VL等待writer名额 | binding commit待提交 | PR #15/core-mainline PASS并合入`develop@2d74a2cf`；三份prompt hash和dialogue/worktree readback | `LOCAL_READBACK / REMOTE_AUTOMATED` | `Product progress=NONE / GOAL_TRANSITION` | `Governance ratio=100% / GOVERNANCE_SCOPE_GUARD binding exception` | 贡献包实现、冻结、串行集成和G04 Gate | Codex管理的独立worktree路径替代计划中的建议路径，隔离与branch合同不变 | 提交并push prompt binding，随后把两个活动任务切到binding commit并发送完整提示词 |
| 2026-08-30 | OCR、上传清理与VL/UI三个用户可见贡献包均已冻结、远端回读并按OCR→Upload→VL/UI顺序集成；共享截图主链进入总集成 | `f1c75d95` / `9403a989` / `206312c0`；registry checkpoint `313c5347` | 三包远端tip与ready commit一致；stable patch-id一致；45项贡献测试与Ruff通过 | `REMOTE_AUTOMATED / MULTI_DIALOGUE_CONTRIBUTION` | `Product progress=RUNTIME+UI / IN_PROGRESS` | `Governance ratio=control-plane checkpoint` | 034、原子幂等、TTL、OpenAPI、浏览器和最终Gate | Qwen-VL保持`NOT_RUN_NO_EXACT_BINDING`；真实Paddle与真实来源Gate仍需单独实跑 | 集成者完成共享持久化/API/前端/CI并运行Screenshot Parity Gate |
| 2026-08-30 | G04四项自动检查拆为独立CI job，fixture回归与真实Paddle一致性Gate建立机器可判别边界 | 工作树待集成者checkpoint | 治理定向测试覆盖receipt缺失、`NOT_EVALUABLE`、空分母、循环oracle、候选过期、Paddle/hardware与2+20绑定 | `LOCAL_AUTOMATED / GOVERNANCE_CONTRACT_ONLY` | `Product progress=RUNTIME+UI / IN_PROGRESS` | `Governance ratio=delivery evidence fail-closed` | 真实许可数据、真实Paddle执行、全量产品检查和耐久delivery receipt | 当前正式receipt状态仍为`NOT_RUN`，不得推断Screenshot Parity Gate通过 | 完成产品验收后生成脱敏正式receipt，再生成并验证G04 product-delivery receipt |
| 2026-08-31 | 截图批次、034持久化、原子一次性消费、隐私清理、统一语义主链、首页与独立CI聚合已形成可验证候选；许可真实截图oracle已由双隔离转写加独立裁决冻结 | 本候选提交（提交后远端readback） | G04定向`137 passed, 1 skipped`；自然路线/正式Gate`38 passed`；Ruff PASS；Windows临时文件安全`47 passed`，Linux容器`36 passed, 2 skipped`；fresh/031 PostgreSQL真实升级与事务测试PASS；live fixture Playwright `5 passed`；OpenAPI、client typecheck/build、frontend production build PASS | `LOCAL_AUTOMATED / POSTGRESQL_INTEGRATION / BROWSER_E2E_FIXTURE / MULTI_AGENT_SIMULATED_REVIEW / LICENSED_REAL_SCREENSHOT_DATASET` | `Product progress=RUNTIME+UI / VERIFYING` | `Governance ratio=product candidate plus fail-closed evidence contract` | 真实Paddle 2+20、backend full suite终态与耐久delivery receipt | Qwen-VL保持`NOT_RUN_NO_EXACT_BINDING`；正式Paddle receipt尚未生成；全量后端仅剩冻结Trip NLU旧manifest与当前validator绑定相互矛盾的2项历史失败，未改冻结资产 | 冻结并push候选commit，回读远端tip后运行真实Paddle；继续寻找不改冻结资产的全量回归解法 |
| 2026-08-31 | 许可真实截图已在冻结候选上通过Screenshot Parity Gate；失败首轮暴露同起点嵌套字段排序缺陷，改用紧框优先几何规则后复跑通过，原图终态删除 | candidate `525af072c47a3f318d88c722bf8067d6ff30907c`；正式receipt待本checkpoint提交 | PaddleOCR 3.7.0/PaddlePaddle 3.3.1 GPU；关键字段F1、reading adjacency-F1、低置信确认召回、清理覆盖率均`1.0`；地点precision/recall下降`0pp`；严重错误/泄漏`0`；3图2+20 P95 `551.451ms` | `REAL_PADDLE_LICENSED_SCREENSHOT_PARITY / LICENSED_REAL / MULTI_AGENT_SIMULATED_REVIEW` | `Product progress=RUNTIME+UI / SCREENSHOT_PARITY_PASS` | `Governance ratio=formal evidence frozen, delivery not yet claimed` | backend full suite历史2项冲突、耐久`PRODUCT_DELIVERY_PASS`、远端CI与PR | cuDNN编译9.9/加载9.5.1但主ABI一致；Qwen-VL仍`NOT_RUN_NO_EXACT_BINDING`；H1/公网/生产/商业未运行 | 提交push/readback脱敏正式receipt，再完成delivery receipt和远端主线验证 |
| 2026-08-31 | 草稿PR #16已绑定正式证据tip；首次远端预检发现PR合成merge commit被误作第一父遍历起点，现改为显式校验真实PR head SHA，未放松工作包顺序规则 | CI修复 `b0e573d`；本治理checkpoint待提交 | 同一base/head本地`validate_core_mainline` PASS；工作流合同与回归`11 passed`；Ruff/diff检查PASS；产品指纹仍为`5dd2f43d2a429450088613079afe692967f47692412a3ca142feb9f7654872b3` | `LOCAL_AUTOMATED / REMOTE_CI_DIAGNOSTIC` | `Product progress=RUNTIME+UI / SCREENSHOT_PARITY_PASS` | `Governance ratio=CI provenance fix, delivery not yet claimed` | 重跑PR #16四项远端Job；backend full suite历史2项冻结冲突；耐久`PRODUCT_DELIVERY_PASS` | 首次远端CI为预检FAIL且四项G04 Job未运行，不能计为远端产品通过 | push/readback修复checkpoint并等待PR #16真实head上的完整CI |
| 2026-08-31 | PR #16第二次预检确认工作流已传入真实head，但工作包子校验仍硬编码合成`HEAD`；现把同一head显式传播到第一父链证明，保留原顺序和冻结commit要求 | 子校验修复 `696581c`；本治理checkpoint待提交 | 工作包/交付治理回归`21 passed`；Ruff/diff检查PASS；显式非checkout head单测覆盖参数传播 | `LOCAL_AUTOMATED / REMOTE_CI_DIAGNOSTIC` | `Product progress=RUNTIME+UI / SCREENSHOT_PARITY_PASS` | `Governance ratio=validator provenance fix, delivery not yet claimed` | 第三次重跑PR #16；backend full suite历史2项冻结冲突；耐久`PRODUCT_DELIVERY_PASS` | 第二次远端CI仍为预检FAIL且四项G04 Job未运行，不能计为远端产品通过 | push/readback参数传播修复并等待完整远端CI |
| 2026-08-31 | PR #16第三次预检已通过范围约束；随后合同测试确认直接校验调用仍看到GitHub合成merge checkout，现让预检及四个G04 Job统一检出冻结PR真实tip | checkout修复 `1d2c317`；本治理checkpoint待提交 | 工作包/交付治理回归`21 passed`；Ruff/diff检查PASS；五个checkout均由合同测试绑定PR head SHA | `LOCAL_AUTOMATED / REMOTE_CI_DIAGNOSTIC` | `Product progress=RUNTIME+UI / SCREENSHOT_PARITY_PASS` | `Governance ratio=exact-tip CI binding, delivery not yet claimed` | 第四次重跑PR #16；backend full suite历史2项冻结冲突；耐久`PRODUCT_DELIVERY_PASS` | 第三次远端CI在合同测试FAIL且四项G04 Job未运行，不能计为远端产品通过 | push/readback精确tip checkout并等待完整远端CI |
| 2026-08-31 | PR #16第四次运行已让预检、截图定向与浏览器E2E通过，真实PostgreSQL生命周期也通过；Linux暴露OpenAPI跨Pydantic渲染漂移和历史套件缺少字体/Windows junction环境，现以无产品字节变化的规范化与兼容层修复 | OpenAPI规范化 `e903c44`；历史Linux兼容 `d865622`；本治理checkpoint待提交 | 远端`core-mainline-preflight`、`g04_screenshot_targeted`、`g04_browser_e2e` PASS；`g04_postgresql`生命周期PASS后全量回归`2480 passed, 20 skipped, 40 failed, 8 errors`；双平台OpenAPI check、client typecheck/build、治理回归`14 passed`及Ruff PASS；产品指纹仍为`5dd2f43d2a429450088613079afe692967f47692412a3ca142feb9f7654872b3` | `REMOTE_AUTOMATED / POSTGRESQL_INTEGRATION / BROWSER_E2E_FIXTURE / CI_ENVIRONMENT_DIAGNOSTIC` | `Product progress=RUNTIME+UI / SCREENSHOT_PARITY_PASS` | `Governance ratio=environment noise isolated, delivery not yet claimed` | 重跑前端与Linux历史全量套件；确认环境噪声清除后处理冻结Trip NLU冲突；耐久`PRODUCT_DELIVERY_PASS` | 第四次远端aggregator FAIL；前端未进入bundle build；全量失败多数为字体/Pydantic/Windows命令环境，且两项冻结Trip NLU合同冲突仍真实存在 | push/readback跨平台修复并在真实GitHub clone上复跑四项Job |

## Auto-advance

- Required gate：`Screenshot Parity Gate + PRODUCT_DELIVERY_PASS`；Next template：`TC-VNEXT-G05-CITY-KNOWLEDGE.md`。
- subject push/readback、耐久PASS、clean tree、无Stop后归档G04，并在独立治理过渡中原子激活完整G05；不创建外部authority generation，不自动进入H1/公网/生产。

## Completion record

- Status：`PENDING`；Subject commits / Remote branch：激活后逐checkpoint填写；
- Verification / Evidence / Gate result / structurally_valid：`LOCAL_AUTOMATED_REGRESSION_COMPLETE / REAL_PADDLE_LICENSED_SCREENSHOT_PARITY / PRODUCT_DELIVERY_NOT_RUN / true`（`structurally_valid=true`）；
- 当前结构有效或G04自动验证通过，不得因此宣称 `V1_CANDIDATE_READY`；该发布门仍不在本Goal授权和证据范围内。
- H1 / production / commercial：`NOT_RUN / NOT_RUN / NOT_RUN`；
- User-visible result / Remaining risks / Goal archived / Next activated：`PENDING / PENDING / false / false`；
- Promotion decision：`NOT_REQUESTED`。

## Stop conditions

- 原图无法在所有终态可靠删除；
- 截图链必须绕开统一语义编译器；
- 必须新增费用、对象存储、账号、生产依赖或未授权数据范围；
- Qwen-VL只能靠弱化bbox、隐私或地点门禁晋级；
- 许可清晰代表性集不足且必须引入真人来源/consent；
- fresh `origin/develop`不再由G04合法接棒，或需要降低Screenshot Parity Gate。
