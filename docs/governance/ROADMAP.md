# 「行程查」V1 24 周 Roadmap

> 状态：`ACCEPTED`
>
> 起始基线：2026-08-22
> 说明：阶段编号只表示 V1 交付顺序，不代表并行授权。

任何时刻只允许 `CURRENT_GOAL.md` 中一个 `APPROVED` 切片。阶段完成后停止，由用户决定是否进入下一阶段。

| 阶段 | 周期 | 用户可见结果 | 主要实现 | 阶段退出证据 |
|---|---:|---|---|---|
| P0 指导与基线 | 第 1～2 周 | 项目目标、能力声明和开发边界一致 | 权威迁移、旧方案归档、Builder 冻结、能力分级、基线 | 文档/链接审计、现有代码/迁移/测试/evidence 基线 |
| P1 输入与 TripBrief | 第 3～6 周 | 用户能提交文本/截图并确认完整行程条件 | PaddleOCR、临时资产清理、解析确认、TripBriefRevision、范围拒绝 | OCR/解析样本、PostgreSQL、隐私清理、关键字段门禁 |
| P2 事实核验 | 第 7～10 周 | 用户看到地点、营业、路线、酒店和时间事实 | 地点归属、四种路线、酒店往返、时间可行性、receipt、恢复 | G1/G2/G3；局部 Provider 失败、幂等、并发、恢复 |
| P3 偏好天气风险 | 第 11～14 周 | 硬事实和建议判断分开，风险有来源 | 偏好/强度规则、月份先验、天气、Brave、RiskEvidence | 来源优先级/TTL/成本测试与真实 Provider receipt |
| P4 Advice 与候选 | 第 15～18 周 | 每个问题有行动方式，可安全采纳真实候选 | AdviceBundle、CandidateSet、路线变化、Repair/EditCommand、postcheck | 建议 100% 覆盖、候选 receipt 100%、零新增高风险 |
| P5 360 数据集与连续验收 | 第 19～22 周 | 每次变化可与稳定基线比较并决定接受/拒绝 | import-only v2、runner、snapshot、独立无 API Judge、blind 隔离 | 18/180/72/90 分层完成，三城各 120，G0～G5 同绑定 |
| P6 稳定化与内部交付 | 第 23～24 周 | 形成「行程查 V1 内测候选版」 | 浏览器 E2E、性能、重启、隐私、live Provider、manifest | `RELEASE_GATES.md` 全部满足，G6 manifest 为候选 |

## 每阶段实现顺序

每个阶段均拆成可独立验证的纵向切片：合同/schema 设计（需 HITL）→ 最小后端路径 → 持久化/失败语义 → 前端主链 → 定向测试 → 同阶段完整门禁。不得先批量建设基础设施，再寻找用户路径。

## 数据集计划

P5 新建 `import-only v2`，总计 360 条：

| 分区 | 数量 | 用途 | 允许调整 |
|---|---:|---|---|
| pilot | 18 | 合同试跑与标注流程验证 | 可重建，但必须版本化 |
| dev | 180 | 规则、Prompt、排序、检索优化 | 允许开发使用，不用于最终晋级 |
| regression | 72 | 已修复问题的持续防回归 | 仅追加或经审计更正 |
| frozen blind | 90 | 独立最终评估 | 开发 Agent、运行模型和 Judge 不得查看标签 |

北京、上海、杭州各 120 条，覆盖干净/噪声文本、手工描述、截图、歧义地点、酒店、餐饮、四种交通、天气、风险新闻和复合修复。train/dev 不用于模型微调。

## 分支与交付

阶段分支使用 `codex/trip-check-p<n>-<scope>`。一个 PR 只包含一个阶段；切片验证后立即 commit/push，最长 60 分钟必须产生远端 checkpoint。合并与进入下一阶段必须人工批准。

## Roadmap 之外

真人内测在 P6 候选版后单独启动，不提前计入 V1 证据。扩城、跨城、Builder、MQ、Kubernetes、GraphRAG、新 Agent 和重新微调不在本 Roadmap 中。
