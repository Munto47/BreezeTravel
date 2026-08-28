# PREDEFINED GOAL：V0.4 截图与文本一致

## Metadata

- Goal ID：`TC-VNEXT-G04-SCREENSHOT`
- Program ID：`TC-VNEXT-2026`
- Product version：`V0.4`
- Status：`DRAFT`
- Activation：G03 Top-3 Audit Gate通过并归档后
- Required gate：`Screenshot Parity Gate + AGENT_GATE_PASS`
- Next Goal：`TC-VNEXT-G05-CITY-KNOWLEDGE`

## Dependencies

- 唯一激活依赖是G03归档且Top-3 Audit Gate通过；随后G04置为`APPROVED`。
- 首个preflight填写branch/baseline并回读统一编译器、source删除、PaddleOCR、Qwen-VL binding和许可清晰的代表性截图集；不需要真人OCR或新consent才能进入G04 Gate，任何Agent结果不得冒充真人数据。

## User Outcome

用户上传最多6张聊天、备忘录、攻略或AI回复截图，得到与粘贴文本相同的逐日卡片、地图准备、住宿和Top-3核验体验；原图不会被长期保存。

## Scope

- PNG/JPEG/WebP批次、顺序和大小校验；
- 临时文件生命周期；
- PaddleOCR 3.x基线；
- OCR box/read order到SourceDocument；
- 复用同一TripUnderstanding语义编译；
- Qwen-VL冻结消融；
- 文本/截图用户结果parity；
- 所有终态清理receipt。

## Pre-approved actions

- 复用现有截图API/上传批次和PaddleOCR依赖；
- Qwen-VL只在账号/区域/exact binding现场readback后作受控实验；
- 不预批准新对象存储、付费OCR或长期原图留存；
- 未证明必要时不新增migration。

## Decisions locked

- 文本与截图不能形成两套语义业务逻辑。
- PaddleOCR是默认Baseline。
- Qwen-VL必须同时在关键字段、阅读顺序、最终卡片、bbox和性能胜出。
- 原图不入数据库、日志、Git或长期缓存。
- 清理失败为PRIVACY_BLOCKED。
- OCR证据对用户隐藏。

## Non-goals

- 新输入来源、视频、PDF或网页抓取；
- 手写识别承诺；
- 通过合成图片或Agent转写冒充真人OCR；
- 修改sealed文本blind。

## Acceptance

完全继承Screenshot Parity Gate：

- 格式/数量/大小/顺序正确；
- 许可清晰的代表性截图集关键字段F1≥95%；参考转写由双Agent隔离生成并由fresh ultra任务裁决；
- 低置信确认召回100%；
- 最终卡片与文本parity达标；
- reading-order adjacency-F1≥97%，地点precision/recall较同源文本下降各≤1个百分点，严重错误0；
- 三张1080×1920图片在冻结环境下P95≤12秒；
- 原图泄漏0、清理100%；
- Qwen-VL不达标保持实验；
- 图片失败不污染已成功文本来源。

## Verification

- upload/cleanup/timeout/cancel/failure；
- OCR baseline和VL panel；
- representative/synthetic/Agent证据分层；
- browser多图、刷新、断线和partial；
- secret/privacy scan；
- backend/frontend/full regression；
- H1/生产：`NOT_RUN`。

## Authority

- `AGENTS.md`、Charter、Spec、v3 API、Architecture；Program、Roadmap、Release Gates、Agent Gate Protocol、Product Mainline Execution Guide、Provider Admission、Risk Register；ADR-007、ADR-009、ADR-011、ADR-012、ADR-013、ADR-014。

## Baseline

- branch/commit/upstream、G03 subject/transition、PaddleOCR版本与候选硬件：激活时填写；
- synthetic、许可清晰的代表性截图、自动视觉复核和`MULTI_AGENT_SIMULATED_REVIEW`严格分层；真人OCR、H1/生产：`NOT_RUN`。

## Invariants

- 文本与截图共用同一语义编译与用户投影；原图不入DB/日志/Git，所有终态清理；
- OCR box/source evidence对用户隐藏；清理失败为PRIVACY_BLOCKED，不伪装成功；
- Qwen-VL不达标保持实验，不修改文本blind或弱化bbox；partial来源不抹掉成功来源。

## Budget

- 1～6张、单张≤10MB；三图P95环境冻结；OCR/VL deadline、并发、临时盘和清理重试按RunSpec记账；
- 不新增对象存储、付费OCR或账号；每切片checkpoint。

## HITL

新增真人来源或consent、Qwen-VL新账号/费用、对象存储/付费OCR、未预批准migration、H1/公网/生产/`main`需批准。现有许可数据上的Agent参考转写、裁决和Gate审查可自主执行。

## Checkpoint ledger

| 时间 | 用户结果 | Commit | Verification | Evidence level | Remaining | Risk/failure | Next autonomous action |
|---|---|---|---|---|---|---|---|
| 激活时填写 |  |  |  |  |  |  |  |

## Auto-advance

- Required gate：`Screenshot Parity Gate`；Next template：`TC-VNEXT-G05-CITY-KNOWLEDGE.md`；
- subject push/readback、耐久`AGENT_GATE_PASS`登记到仓库外Goal pass ledger、clean tree、无Stop后，最终归档，按Program稳定binding原子激活G05并创建generation 5权限锚；H1/公网/生产不自动启动。

## Completion record

- Status / Subject commits / Remote branch：激活后填写；
- Verification / Evidence / Gate result / `structurally_valid`：激活后填写；
- H1 / production / commercial：激活时固定为`NOT_RUN / NOT_RUN / NOT_RUN`；
- User-visible result / Remaining risks / Goal archived / Next activated：激活后填写；
- Promotion decision：`NOT_REQUESTED`。

## Stop conditions

- 原图无法在所有终态删除；
- 需要新增付费OCR/对象存储；
- Qwen-VL只能通过弱化bbox或来源门禁晋级；
- 截图链必须绕开统一语义编译器；
- 必须新增真人来源数据且无法以许可清晰的代表性集满足门禁。
