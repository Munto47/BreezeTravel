# TC-VNEXT-2026 Program

> 状态：`APPROVED`
>
> Program 版本：`Blueprint 1.0`
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

| 顺序 | 版本 | Goal ID | 用户结果 | 依赖 | 退出 Gate |
|---:|---|---|---|---|---|
| 0 | Blueprint 1.0 | `TC-BP-G00-BLUEPRINT` | 项目方向、架构、接口、版本和门禁可执行 | 当前远端基线 | Blueprint Gate |
| 1 | V0.1 可信卡片 | `TC-VNEXT-G01-TEXT-CARDS` | 长文本直接生成可编辑逐日卡片 | G00 | Text Card Gate |
| 2 | V0.2 地图与住宿 | `TC-VNEXT-G02-MAP-STAY` | 打开地图即看步行/公交；修改后手动更新；选择整程酒店 | G01 | Map & Stay Gate |
| 3 | V0.3 核心行程查 | `TC-VNEXT-G03-TOP3-AUDIT` | 只看到最重要三个问题并可修复 | G02 | Top-3 Audit Gate |
| 4 | V0.4 截图一致性 | `TC-VNEXT-G04-SCREENSHOT` | 截图得到与文本同等级卡片 | G03 | Screenshot Parity Gate |
| 5 | V0.5 三城知识层 | `TC-VNEXT-G05-CITY-KNOWLEDGE` | 更可靠的时长、时段、夜景、季节和预约建议 | G04 | Knowledge Admission Gate |
| 6 | V0.6 个性化与分享 | `TC-VNEXT-G06-MEMORY-SHARE` | 显式记忆偏好并分享用户友好行程 | G05 | Consent & Share Gate |
| 7 | V0.9 候选版 | `TC-VNEXT-G07-CANDIDATE` | 稳定、快速、隐私合规、可演示 | G06 | Candidate G0～G6 |
| 8 | V1.0 真人版 | `TC-H1-G01-HUMAN-USABILITY` | 证明真实用户能独立完成主链 | G07 + 人工批准 | H1 |
| 9 | V1.1 商业探索 | 新 Program | 验证单次深核验付费与创作者工具 | H1 + 人工批准 | Commercial Evidence |

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

## 4. 预批准的公共合同与 migration

这些授权只在对应 Goal 激活后生效。

### G01

- 新增 `/api/v3/trip-understandings` create/result/events/commands、source/行程删除和账号旅行数据级联删除合同；
- 新增 `028_trip_understanding_v3.sql`，包含持久job/lease/event、资源所有权、source TTL和删除回执；
- 新增 `029_map_render_snapshots.sql`、最小地图worker、walking/transit计算、逻辑去重和迟到写保护，使首批卡片后真正开始后台地图准备；
- 接入模型中立 `StructuredInferenceProvider`；G01置为`APPROVED`后，首个preflight必须现场readback Qwen账号、区域、exact model ID、endpoint、价格和隐私条款，不假定已有配置；
- 修改 Web 首页/登录顺序，新增匿名体验；
- 冻结现有未版本化API的OpenAPI兼容snapshot；
- 复用现有高德POI与walking/transit开发能力，但只有许可readback允许的最小字段可以持久化。

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

- 复用现有截图、PaddleOCR 和清理合同；
- Qwen-VL只作消融实验，不因模型存在自动晋级；
- 不预批准新对象存储或付费 OCR。

### G05

- 仅在 Provider/Data Admission Gate 通过后执行 `032_knowledge_claims.sql`；
- 允许官方/政府/运营方、授权创作者和用户主动上传内容；
- 不授权抓取小红书或其他未授权社交内容。

### G06

- 仅在 consent/delete 合同通过后执行 `033_user_memory_and_feedback.sql`；
- 记忆只保存结构化偏好；
- 分享使用不可枚举、可撤销 token，不恢复房间号作为入口。

### G07

- 不预批准新产品功能；
- 只做性能、可靠性、隐私、可访问性、Provider证据和候选材料收口；
- 更新release manifest生成器，使其读取TC-VNEXT Goal/Gate、v3 OpenAPI、新数据集和同绑定receipts；旧360/三城manifest测试只保留历史兼容，不是新版Blueprint证明；
- 不自动部署或进入 H1。

所有 migration 只追加，不修改 001～027。应用启动不得自动执行 DDL。

## 5. 模型与外部调用预算

- G00：不得调用模型、POI、路线、天气或其他真实 Provider。
- G01：Qwen开发调用不设总费用硬上限，但每任务最多一次初始调用和一次schema修复，并记录exact model、token、延迟、修复、失败和估算费用；不得新增付费账号、绑卡或生产调用。
- Qwen Max为质量上限和初始开发benchmark候选，Plus为生产候选，Flash为低延迟候选；只在dev/validation选唯一候选，冻结后sealed blind一次性验证。
- DeepSeek保留冻结 Baseline，不作静默 fallback。
- G01地图预计算及以后只允许当前已有、许可readback通过且无增量费用的高德/天气开发矩阵；扩大范围或产生新费用需人工批准。
- 同一失败策略最多两次；第三次尝试前必须改变假设、实现、工具、数据或验证方法。

## 6. 数据与评测

G01新建语义/地点数据集：

- 90 条 family-isolated 长文本；
- 60 条北京/上海/杭州；
- 15 条其他城市；
- 15 条多城市、URL、描述、备选、否定和经过地点对抗样本；
- 54 dev / 18 validation / 18 sealed blind；
- 当前 19 条用户文本只作 regression，不进入 blind；
- 双人独立标注，冲突裁决；
- blind schema/oracle冻结后禁止修改；标签由独立custodian保管，开发代理和运行模型不可读；
- Max/Plus/Flash只在dev/validation选择，唯一候选、prompt、schema、threshold和最小预测分母冻结后才运行sealed blind一次；
- blind失败只生成独立dev/regression故障族，不回看标签调参；输入分布或schema实质变化时必须经独立批准创建新版blind，旧版只读。

每个真实修复故障追加 regression。模型不能评价自己的输出；自动 Judge只作辅助，确定性 scorer和可执行行为是门禁权威。

### 可选形成性用户学习

G01、G02、G03通过工程Gate后分别预留 `FUX-01卡片理解`、`FUX-02地图更新/住宿信任`、`FUX-03 Top-3行动理解`。每次都必须由用户另行批准招募、consent、数据范围和脚本；不自动启动、不阻断后续工程Goal、不得冒充H1或Candidate证据。其唯一作用是尽早发现产品语言和交互假设错误，结论只进入后续Goal的显式变更提案。

## 7. 自动推进

Goal完成后只有同时满足以下条件才能激活下一 Goal：

- 当前 Goal 的用户 Outcome 已实现；
- 对应 Gate 全 PASS；
- 没有未披露的 required `NOT_RUN`；
- working tree clean；
- checkpoint commit 已推送；
- 远端文件/evidence可回读；
- 所有 Stop condition 为 false；
- 从current Goal完整内容生成最终completed归档，不删除字段或保留PENDING；
- 下一 Goal合同与 Program模板一致。

subject checkpoint先push/readback；归档与下一Goal激活在同一个治理过渡commit中完成，该commit也必须push/readback。transition commit不要求把自身未知hash写入自身。

自动推进只到 G07。H1、生产、公网、商业、合并 `main` 始终需要人工批准。

## 8. Stop conditions

出现以下情况停止并请求最小必要决策：

- 需要改变产品北极星或跳过 Goal；
- 需要未预批准 migration/API/生产依赖；
- 需要新账号、绑卡、付费 Provider或扩大数据来源；
- 高德/模型/知识数据留存或许可无法满足；
- 需要读取或修改 sealed blind/oracle；
- 发现隐私泄漏、事实与证据无法消解；
- 连续两个不同切片无法改善同一硬门禁；
- 需要真人、发布、部署或 `main` 合并。

普通代码错误、测试失败、构建失败、环境问题和可调查的不确定性不是用户阻塞。

## 9. 历史证据边界

旧 Trip Intake、Builder、P5/P6 Candidate、旧公网、旧模型和旧 manifest 保持历史状态。它们可以证明可复用资产曾经工作，但不能宣称新版 `TC-VNEXT-2026` 已通过任何产品、候选、真人或商业 Gate。
