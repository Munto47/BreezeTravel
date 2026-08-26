# 「行程查」V1 Release Gates

> 状态：`ACCEPTED`
> 适用目标：进入真人内测前的 V1 内测候选版

## 0. Ready 状态分层

经用户于 2026-08-26 批准，`TC-I1-I4-trip-intake-v2` 使用独立的开发完成态，避免历史双入口候选门禁反向阻断当前已批准的 Intake 纵向切片：

- `INTAKE_V2_DEVELOPMENT_READY`：候选 commit 上 backend 全套 pytest、Ruff、frontend build、dual-entry 结构 validator 和 120 条 NLU Gate 全部通过；025 migration、revision/CAS/idempotency/materialization 的离线合同与回归通过；
- dual-entry 的旧统计规模、Builder G2/G5 seeds、P5 历史 blind/Judge、real OCR、live Provider、公网和真人证据不属于 Intake v2 文本抽取开发完成态，保持各自原状态；
- `V1_CANDIDATE_READY`：仍必须满足下文 G0～G6、零容忍项及候选证据要求。`INTAKE_V2_DEVELOPMENT_READY` 不得改写或替代该结论；
- validator 存在结构错误、哈希错误、blind 泄漏、默认事实注入、关键字段反转或新增测试失败时，两个状态都必须 fail closed。

这是一项范围调整，不是质量阈值下调：历史能力不再作为当前 Goal 的完成前置，但其缺失证据不得被标记为 PASS。

## 1. 评分门槛

| 分桶 | 权重 | 最低要求 |
|---|---:|---:|
| OCR/解析 | 15% | 建议性分桶通用下限 80；关键字段另有硬门槛 |
| 地点与城市事实 | 20% | 90 |
| 时间、路线与酒店衔接 | 20% | 90 |
| 偏好与活动强度 | 10% | 80 |
| 天气、月份与风险 | 10% | 80 |
| 行动建议与备选地点 | 20% | 80 |
| 稳定性与性能 | 5% | 80 |

综合分必须 ≥88；所有建议性分桶 ≥80；地点和路线事实分桶 ≥90。加权总分不能抵消任一硬门槛失败。

## 2. 零容忍阻断项

以下任一非零即 `REJECT`：

- 错城或错误 POI 被自动接受；
- HARD 冲突漏检；
- 虚构事实或模型举例被写成真实候选；
- 最终地点候选缺少地点或路线 receipt；
- 修复后新增 BLOCKER/HIGH；
- 原始截图未按终态策略删除。

## 3. 功能质量硬门槛

- OCR/解析关键字段 F1 ≥95%，低置信关键字段 100% 进入确认；候选版 G1 必须由真实来源 OCR 数据集证明，synthetic stress 只能用于开发阶段回归；
- 路线问题 precision 和 recall 均 ≥90%；
- 备选地点与路线 receipt 绑定率 100%；
- 非 PASS Finding 的行动建议覆盖率 100%；
- 返回具体地点的最终建议 100% 来自冻结真实 CandidateSet；
- 固定 snapshot 重放 hash 一致率 100%；
- 浏览器关键链、刷新、断线、进程重启、并发与幂等场景全部通过。

### Trip Intake v2 NLU Gate

- 固定 120 条：72 dev / 24 validation / 24 frozen blind；easy/medium/hard 为 30/54/36；
- JSON/schema、证据子串和评分覆盖率 100%；generator/template/mutation family 不跨 split；
- 编造、出发地/目的地反转、否定反转、旧计划反转、数字串类和 `UNKNOWN → EXACT` 均为 0；
- locations、party size、duration 结构化 micro-F1 各 ≥95%，preferences/requirements ≥90%，hard 子集关键字段 ≥90%；
- 本 Gate 只证明文本需求抽取，不能替代 OCR、live Provider、公网或真人证据。

## 4. 性能硬门槛

- 标准文本首次进度 ≤1 秒；
- 解析与确认页 P95 ≤3 秒；
- 三张截图 OCR P95 ≤12 秒；
- 基础报告 P95 ≤30 秒；
- 含风险搜索的完整报告 P95 ≤45 秒。

## 5. Evidence Gate

| Gate | 必须实际证明的内容 |
|---|---|
| G0 文档/schema | 权威文件一致、schema/API 合同通过审核、migration 只追加 |
| G1 离线单测 | 规则、解析、隐私清理、状态机、幂等和失败语义 |
| G2 PostgreSQL 集成 | migration、事务、并发、租约接管、重启回读、旧数据兼容 |
| G3 固定快照 | 冻结 Provider snapshot 重放、hash 一致、无网络依赖 |
| G4 真实 Provider | 高德路线、和风天气预报与实时预警的请求/响应/时间/配置回执与局部失败；其他风险发现 Provider 必须先通过数据留存与来源准入 |
| G5 浏览器与性能 | 主链、确认、刷新、断线、重启、采纳/postcheck 和 P95 |
| G6 Release manifest | 同一 commit/config/dataset/model/rule/provider receipt 的不可变汇总 |

G0～G6 必须在候选 commit 上重新运行。旧 manifest、历史报告或不同 dirty tree 的结果不能拼接。

## 5.1 开发阶段 Gate

### D1：第 6 周文本纵向闭环

- 北京、上海、杭州各至少一个浏览器主链通过；
- 18 条 pilot 可由固定 runner 执行；
- 错 POI/错城自动接受为 0；
- Repair 后新增 BLOCKER/HIGH/UNKNOWN 为 0；
- Evidence 后终止进程，恢复后 Run、receipt、revision 和 postcheck 一致；
- 同一幂等键不产生第二个 Run、repair 或 revision。

### Reliability Gate

六类固定故障必须产生预期机器可读结果：

| 故障 | 预期 |
|---|---|
| Provider timeout | 有界重试，受影响字段 UNKNOWN，其他事实保留 |
| 字段部分失败 | Run 为 PARTIAL，成功事实不丢失 |
| 重复提交 | 返回同一资源并标记 idempotent replay |
| 并发编辑 | 一个成功，失败方 409 并回读当前 revision |
| 进程终止 | 过期 lease 接管，不重复副作用 |
| config 漂移 | `RUN_CONFIG_MISMATCH`，禁止拼接恢复 |

Trace 必须包含 `bt.run_id`、revision、brief、evidence、config、rule、provider、execution mode 和失败类别，且敏感字段扫描为 0 命中。

### P3 Synthetic OCR Phase Gate

本条是经现场批准的 P3 开发阶段例外，不改变候选版 G1：

- 冻结 12 例 `synthetic_stress`，北京、上海、杭州各 4 例；
- PNG/JPEG/WebP、聊天/备忘录/攻略或 AI 回复、clean/medium/hard 各 4 例，明暗主题各 6 例；
- spec、oracle、render 参数和 seed 必须在首次 OCR 前冻结，看到结果后不得修改标签或样本来消除失败；
- 关键字段 micro-F1 ≥95%，预标记低置信关键字段确认召回率 100%，原图泄漏命中 0；
- 阶段通过只能标记 `synthetic_ocr_stress=PASS`；`real_ocr_dataset` 与候选版 G1 继续为 `NOT_RUN`。
- P3 phase status 只硬要求本 Gate、G2、G3 与对应离线回归；G4 live receipt、G5、G6 是候选证据债，保持 `NOT_RUN/REJECT` 不得反向把离线 P3 判为失败。
- 合成图片必须有可回读的确定性 render-integrity receipt（解码、格式、尺寸、内容 hash、可复现性、非空/无溢出、字体与终态清理）；开发代理视觉复核只能标记 `automated_agent`，不得冒充人工证据。

### Solver Admission Gate

在固定 36 条 bake-off 上按以下顺序判定：

1. 新增 BLOCKER/HIGH/UNKNOWN 为 0；
2. 完整 postcheck 成功率优先；
3. 相比 BoundedRepair 成功率提高至少 10 个百分点，或稳定解决至少 3 类其无法解决的问题；
4. 5 天 25 站 P95 ≤2 秒；
5. 安全条件相同时比较编辑成本和路线代价。

未通过时 OR-Tools 保持实验资产，不得进入默认运行时。

### Evaluation Gate

- 数据为 18 pilot / 180 dev / 72 regression / 90 frozen blind，三城各 120；
- 同源/变异案例不跨 split；
- Legacy A、Core B、Solver C 使用相同 RunSpec 和 oracle；
- blind 标签对开发 Agent、运行模型和 Judge 隔离；
- 结果包含任务成功、错 POI、HARD 漏检、UNKNOWN 保留、postcheck、unsupported claim、P95、token、成本和 replay hash；
- 消融结果只决定默认运行时，不替代 G0～G6。

### Candidate Gate

候选版除 G0～G6 外还必须交付受控公网演示、90 秒视频、5 分钟完整演示、架构图、恢复时序图、消融表和可回读 manifest。公网演示默认使用受控 snapshot；live Provider Gate 单独执行。

## 6. Judge 与人工证据

自动语义 Judge 使用独立、无 API 的模型评审流程；运行时 DeepSeek 不得评价自己的输出。结果只标记为 `automated_proxy_judge`，不等于真人校准、public E2E 或发布批准。

`frozen_blind` 标签与开发 Agent、运行模型和 Judge 隔离；blind 失败只进入 `dev/regression` 的复现形式，禁止修改 blind/oracle 以消除失败。

真人内测不属于本 V1 候选版 Gate。候选版通过后进入 H1，并由用户现场批准公网、招募与 consent。

### Human Usability Gate（H1）

- 8～12 人；每人完成一个统一受控任务，并可自愿使用真实行程；
- ≥80% 无需开发者代操作完成输入、确认、报告理解和采纳；
- 关键 Finding 理解率 ≥80%；
- 虚构事实/错误地点被当作可靠建议为 0；
- 原图留存和隐私事故为 0；
- 严重误导或主链阻断全部进入 regression，修复后重跑相关 G1～G6 并生成新 manifest。

H1 只能描述为小样本真人可用性证据，不得宣称统计显著、市场验证或生产 SLO。

## 7. 禁止替代

Builder、旧推荐指标、历史 RAGAS、synthetic proxy、source prior、旧 release manifest、测试数量或单个演示不得替代上述任何门禁。
