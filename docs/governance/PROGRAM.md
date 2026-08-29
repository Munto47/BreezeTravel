# TC-VNEXT-2026 Program

> 状态：`APPROVED`
>
> Program 版本：`Blueprint 1.3 / Unified Mainline and Parallel Delivery`
>
> 授权日期：2026-08-27
>
> 授权来源：用户批准的「行程查」长期产品蓝图与治理重构计划

## 1. Program Outcome

按固定顺序把 BreezeTravel 从旧房间/表单/逐句 POI 搜索体验迁移为：

```text
随便粘贴文本或上传截图
→ 高准确率逐日卡片
→ 后台准备、手动更新的真实路线地图
→ 整程住宿与用餐补全
→ Top-3 真实性和可行性核验
→ 最小修改、版本化采纳和完整复检
```

北京、上海、杭州达到深度能力；其他国内城市提供基础整理并明确边界。Program 不自动授权 H1、生产、公网、商业化、抓取第三方内容或合并 `main`。

## 2. 固定版本和 Goal 顺序

| 顺序 | 阶段 | 版本 | Goal ID | 用户结果 | 依赖 | 退出 Gate |
|---:|---|---|---|---|---|---|
| 0 | Blueprint | Blueprint 1.0 | `TC-BP-G00-BLUEPRINT` | 项目方向、架构、接口、版本和门禁可执行 | 当前远端基线 | Blueprint Gate |
| 1 | `CORE_MVP` | V0.1 可信卡片 | `TC-VNEXT-G01-TEXT-CARDS` | 长文本直接生成可编辑逐日卡片 | G00 | Text Card Gate |
| 2 | `CORE_MVP` | V0.2 地图与住宿 | `TC-VNEXT-G02-MAP-STAY` | 打开地图即看步行/公交；修改后手动更新；选择整程酒店 | G01 | Map & Stay Gate |
| 3 | `CORE_MVP` | V0.3 核心行程查 | `TC-VNEXT-G03-TOP3-AUDIT` | 只看到最重要三个问题并可修复 | G02 | Top-3 Audit Gate |
| 4 | `PRODUCT_ENHANCEMENT` | V0.4 截图一致性 | `TC-VNEXT-G04-SCREENSHOT` | 截图得到与文本同等级卡片 | G03 | Screenshot Parity Gate |
| 5 | `PRODUCT_ENHANCEMENT` | V0.5 三城知识层 | `TC-VNEXT-G05-CITY-KNOWLEDGE` | 更可靠的时长、时段、夜景、季节和预约建议 | G04 | Knowledge Admission Gate |
| 6 | `PRODUCT_ENHANCEMENT` | V0.6 个性化与分享 | `TC-VNEXT-G06-MEMORY-SHARE` | 显式记忆偏好并分享用户友好行程 | G05 | Consent & Share Gate |
| 7 | `CANDIDATE_HARDENING` | V0.9 候选版 | `TC-VNEXT-G07-CANDIDATE` | 稳定、快速、隐私合规、可演示 | G06 | Candidate G0～G7 |
| 8 | Human | V1.0 真人版 | `TC-H1-G01-HUMAN-USABILITY` | 证明真实用户能独立完成主链 | G07 + 人工批准 | H1 |
| 9 | Commercial | V1.1 商业探索 | 新 Program | 验证单次深核验付费与创作者工具 | H1 + 人工批准 | Commercial Evidence |

任何时刻 `CURRENT_GOAL.md` 只允许一个 `APPROVED` 或 `IN_PROGRESS` Goal。不得并行激活两个产品 Goal，也不得跳过依赖。

## 3. Goal 内切片规则

每个 Goal 按最小端到端切片推进：

1. 读取当前 Goal、权威合同和基线；
2. 复现用户问题或当前缺口；
3. 交付一个用户可观察结果；
4. 运行定向验证；
5. 检查公共用户投影；
6. 更新 checkpoint；
7. commit、push、远端 readback；
8. 继续下一切片。

每个 checkpoint 写入：

- 用户现在能做什么；
- commit 和 branch；
- 实际验证与 `NOT_RUN`；
- 当前 evidence 等级；
- 剩余工作；
- 新风险或失败；
- 下一自主动作。

### 3.1 产品主线比例

每个切片必须推进用户可见主链或当前Goal硬指标。纯治理切片最多连续一个checkpoint；连续两个checkpoint均为`Product progress = NONE`时，必须按`PRODUCT_MAINLINE_EXECUTION_GUIDE.md`停止治理扩展并回到产品实现。

版本证据采用比例原则：G01～G06只要求`CORE_AGENT_GATE`；G07才要求候选级`HARDENED_CANDIDATE_GATE`。任何后续版本或生产加固不得前置阻断早期产品Goal。

### 3.2 G01～G07 统一 Agent Gate

G01～G07 固定执行 `AGENT_GATE_PROTOCOL.md`，通过状态为 `AGENT_GATE_PASS`。证据等级只允许：

- `AUTOMATED_TEST`；
- `LIVE_PROVIDER_EVIDENCE`；
- `MULTI_AGENT_SIMULATED_REVIEW`；
- `SEALED_AGENT_BLIND`；
- `HUMAN_USABILITY`；
- `PRODUCTION_EVIDENCE`。

Agent 参考答案、裁决和审查不属于真人证据。每个候选必须绑定同一 commit/config/data，执行三个隔离审查角色和新的 ultra 裁决；可复现且属于当前Goal的P0/P1必须修复。P2只有在破坏当前用户Outcome、硬Gate指标、隐私/安全不变量，或在Goal激活时被显式列为blocking时才阻断，其余必须记录后续归属。需要blind的Gate必须由独立Codex任务执行。候选绑定改变时只让受影响结论失效。历史真人schema与历史证据只读保留。

`CORE_AGENT_GATE`固定候选commit/config/data、prompt/schema、确定性scorer、三角色审查、ultra裁决、Goal要求的sealed blind和clean checkout readback。仓库外保存原始Agent输出和blind truth，Git只保存hash、脱敏回执和聚合指标。G01～G06不要求角色私钥、外部broker、authority generation、activation-readiness或隔离OCI供应链证明。

现有G01 `BOOTSTRAP`、authority verifier、purpose-specific broker设计和相关schema保留为`DEFERRED_CANDIDATE_HARDENING`历史资产，不删除也不冒充PASS；在G07前不继续实现、不切`ACTIVE`、不阻断Goal。G07固定使用候选收口档位`HARDENED_CANDIDATE_GATE`，但必须先产生`HardeningDecision`：可选择`NOT_REQUIRED_WITH_RATIONALE`并记录残余风险，或在明确威胁模型证明价值时选择`REQUIRED`并只启用被点名的外部authority/OCI控制。任何签名只能证明对应执行事实，不能替代Provider事实、真人、生产或商业证据。

### 3.3 并行工作包机器合同

`docs/governance/current_work_packages.json`是当前并行开发清单，字段和状态由生成schema约束。固定规则：

- 一个active Goal、一个非终态`INTEGRATOR`；所有可写包合计最多三个，集成者处于写入状态时也计入上限；`READY_TO_MERGE`必须冻结；
- 所有可写包使用同一exact baseline，`owned_paths`不得按目录前缀重叠；指导文件或Goal binding不一致的checkout只能只读；
- 最多提前一个Goal且最多两个`PREPARED_NOT_INTEGRATED`贡献包；下一Goal不得登记集成者，当前Goal不得依赖下一Goal。准备不等于激活，不得提前合并、创建migration或改变公共API；
- 普通贡献包必须把AGENTS、CURRENT_GOAL、Goal/work-package binding、migration目录、共享OpenAPI生成物和锁文件列入`forbidden_paths`；
- 工作包并行实现、独立定向测试和独立commit；贡献包冻结为`READY_TO_MERGE`后，唯一集成者按领域模型→持久化/API→前端→E2E串行合并并接纳已登记路径；
- 每个包最多两轮修复复审。未阻断当前用户Outcome的剩余P2/P3进入风险登记和明确后续Goal，不延长主线。

## 4. 预批准的公共合同与 migration

这些授权只在对应 Goal 激活后生效。

### G01

- 新增 `/api/v3/trip-understandings` create/result/events/commands、source/行程删除和账号旅行数据级联删除合同；
- 新增 `028_trip_understanding_v3.sql`，包含持久job/lease/event、资源所有权、source TTL和删除回执；
- 新增 `029_map_render_snapshots.sql`、最小地图worker、walking/transit计算、逻辑去重和迟到写保护，使首批卡片后真正开始后台地图准备；
- 接入模型中立 `StructuredInferenceProvider`；从当前环境安全加载已有凭据并通过官方目录自动readback Qwen区域、exact model ID、endpoint、上下文和Provider可返回的价格字段；未暴露字段写`NOT_EXPOSED_BY_PROVIDER`，不向用户索要；
- 修改 Web 首页/登录顺序，新增匿名体验；
- 冻结现有未版本化API的OpenAPI兼容snapshot；
- 在`OWNER_ATTESTED_EXISTING_AUTHORIZATION`范围使用高德POI与walking/transit开发能力，只持久化最小脱敏字段；生产、公开展示和长期缓存许可仍在对应人工审批点处理。

### G02

- 新增 v3 map-renders、stay-suggestions、stay-selection 合同；
- 新增 `030_stay_recommendation_snapshots.sql`；
- 在G01地图后端上交付地图剧场、walking/transit切换、`NEEDS_UPDATE`和手动重绘；
- 复用现有高德开发账号的POI与步行/公交能力，在许可确认且无增量费用的开发范围内执行；
- 新增版本化 HotelBrandRegistry；
- 路线几何只按条款进入短期缓存。

### G03

- 新增 Top-3 用户投影与 materialize 入口；
- 复用现有 EvidenceSnapshot、AuditEngine、Advice、EditCommand 和 postcheck；
- 执行必需的 `031_day_index_trip_bridge.sql`，建立 `PlanRevisionRef` materialization lineage、`calendar_basis=ABSOLUTE|DAY_INDEX_ONLY`、nullable calendar range和软人数来源；不得虚构日期或伪装用户确认。

### G04

- 追加登录态multipart `POST /api/v3/screenshot-batches`和`source.type=SCREENSHOT_BATCH`；返回短期owner-bound不透明引用，禁止Base64 JSON；
- 复用PaddleOCR基线和统一语义编译器，原图所有终态清理；OCR文本/bbox映射继承SourceDocument TTL和主动删除；
- Qwen-VL只作消融实验，不因模型存在自动晋级；
- 不预批准新对象存储或付费 OCR。

### G05

- 仅在 Provider/Data Admission Gate 通过后执行 `032_knowledge_claims.sql`；
- 允许官方/政府/运营方、授权创作者和用户主动上传内容；
- 不授权抓取小红书或其他未授权社交内容。

### G06

- 仅在 consent/delete 合同通过后执行 `033_user_memory_and_feedback.sql`；
- 记忆只保存结构化偏好；
- 预批准偏好查看/更新/清空、最小反馈事件、分享创建/撤销/只读投影v3合同；
- 分享使用摘要存储、不可枚举、可撤销、可过期的秘密；可点击链接只把secret放在fragment，前端以body换取HttpOnly capability后立即清除，secret不进入服务端可见URL/日志/Referer/分析，不恢复房间号作为入口。
- 反馈不隐含训练/评测授权；独立数据用途consent默认关闭、可撤销。清空全部旅行数据必须清除偏好和反馈并撤销分享。

### G07

- 不预批准新产品功能；
- 只做性能、可靠性、隐私、可访问性、Provider证据和候选材料收口；
- 基于明确威胁模型决定是否启用外部authority、purpose-specific broker、角色签名、不可变远端ref和隔离OCI；若不启用，必须记录替代控制与残余风险；
- 更新release manifest生成器，使其读取TC-VNEXT Goal/Gate、v3 OpenAPI、新数据集和同绑定receipts；旧360/三城manifest测试只保留历史兼容，不是新版Blueprint证明；
- 不自动部署或进入 H1。

所有 migration 只追加，不修改 001～027。应用启动不得自动执行 DDL。

## 5. 模型与外部调用预算

- G00：不得调用模型、POI、路线、天气或其他真实 Provider。
- G01：Qwen开发调用不设总费用硬上限，但每任务最多一次初始调用和一次schema修复，并记录exact model、token、延迟、修复、失败和估算费用；不得新增付费账号、绑卡或生产调用。
- Qwen Max为质量上限和初始开发benchmark候选，Plus为生产候选，Flash为低延迟候选；只在dev/validation选唯一候选，冻结后sealed blind一次性验证。
- DeepSeek保留冻结 Baseline，不作静默 fallback。
- G01地图预计算及以后只允许当前已有、所有者声明授权且无增量费用的高德/天气开发矩阵；扩大范围或产生新费用需人工批准。
- 同一失败策略最多两次；第三次尝试前必须改变假设、实现、工具、数据或验证方法。

## 6. 数据与评测

G01新建语义/地点数据集：

- 90 条 family-isolated 长文本；
- 60 条北京/上海/杭州；
- 15 条其他城市；
- 15 条多城市、URL、描述、备选、否定和经过地点对抗样本；
- 54 dev / 18 validation / 18 sealed blind；
- 旧根目录`tests/`中的19条未完成旅行文本已按项目所有者要求删除，不进入regression、oracle或blind；G01的90条数据必须按本Program重新生成、双Agent独立标注并冻结；
- 两个`gpt-5.6-sol / xhigh`隔离任务独立标注，新的`gpt-5.6-sol / ultra`任务在A/B输出冻结并计算hash后裁决；字段使用`agent_reference/agent_adjudication`，不得伪装真人；
- blind schema/oracle冻结后禁止修改；答案由独立Codex任务在仓库外保管，开发任务和运行模型不可读；
- Max/Plus/Flash只在dev/validation选择，唯一候选、prompt、schema、threshold和最小预测分母冻结后才运行sealed blind一次；
- blind失败只生成独立dev/regression故障族，不回看标签调参；输入分布或schema实质变化时必须经独立批准创建新版blind，旧版只读。

每个真实修复故障追加 regression。候选模型不能评价自己的输出；Agent裁决只接收可复现证据，确定性 scorer、live Provider回执和可执行行为共同构成门禁权威。

### 可选形成性用户学习

G01、G02、G03通过工程Gate后分别预留 `FUX-01卡片理解`、`FUX-02地图更新/住宿信任`、`FUX-03 Top-3行动理解`。每次都必须由用户另行批准招募、consent、数据范围和脚本；不自动启动、不阻断后续工程Goal、不得冒充H1或Candidate证据。其唯一作用是尽早发现产品语言和交互假设错误，结论只进入后续Goal的显式变更提案。

## 7. 自动推进

Goal完成后只有同时满足以下条件才能激活下一 Goal：

- 当前 Goal 的用户 Outcome 已实现；
- 对应 Gate 已绑定同一候选并取得 `AGENT_GATE_PASS`；
- 没有未披露的 required `NOT_RUN`；
- working tree clean；
- checkpoint commit 已推送；
- 远端文件/evidence可回读；
- 所有 Stop condition 为 false；
- 从current Goal完整内容生成最终completed归档，不删除字段或保留PENDING；
- 下一 Goal合同与 Program模板一致。

subject checkpoint先push/readback，并把Gate回执耐久物化；归档、下一Goal激活、`current_goal_binding.json`和`current_work_packages.json`在同一个治理过渡commit中原子更新。下一Goal的合同路径/hash、阶段和Gate profile必须与Program表对应项完全一致，该commit也必须push/readback。G01～G06不登记外部Goal ledger、不推进authority generation；G07只有在`HardeningDecision=REQUIRED`时才处理被点名的外部anchor。transition commit不要求把自身未知hash写入自身。

G03完成后保存可体验里程碑并自动进入G04，不新增HITL。自动推进只到 G07；G07最高状态为`VNEXT_CANDIDATE_READY_AGENT_VERIFIED`。H1、生产、公网、商业、合并 `main` 始终需要人工批准。

## 8. Stop conditions

出现以下情况停止并请求最小必要决策：

- 需要改变产品北极星或跳过 Goal；
- 需要未预批准 migration/API/生产依赖；
- 需要新账号、绑卡、付费 Provider或扩大数据来源；
- 需要读取或修改 sealed blind/oracle；
- 发现隐私泄漏、事实与证据无法消解；
- 需要真人、发布、部署或 `main` 合并。

普通代码错误、测试失败、构建失败、Agent Gate或blind失败、环境问题、当前已有Provider的可诊断配置问题和可调查的不确定性不是用户阻塞；它们留在当前Goal继续诊断。只有解决方案需要改变产品目标/Gate、扩大付费/数据权限或进入上述人工阶段时才停止请求决定。

## 9. 历史证据边界

旧 Trip Intake、Builder、P5/P6 Candidate、旧公网、旧模型和旧 manifest 保持历史状态。它们可以证明可复用资产曾经工作，但不能宣称新版 `TC-VNEXT-2026` 已通过任何产品、候选、真人或商业 Gate。
