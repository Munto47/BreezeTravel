# 「行程查」V1 Release Gates

> 状态：`ACCEPTED`
> 适用目标：进入真人内测前的 V1 内测候选版

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

- OCR/解析关键字段 F1 ≥95%，低置信关键字段 100% 进入确认；
- 路线问题 precision 和 recall 均 ≥90%；
- 备选地点与路线 receipt 绑定率 100%；
- 非 PASS Finding 的行动建议覆盖率 100%；
- 返回具体地点的最终建议 100% 来自冻结真实 CandidateSet；
- 固定 snapshot 重放 hash 一致率 100%；
- 浏览器关键链、刷新、断线、进程重启、并发与幂等场景全部通过。

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
| G4 真实 Provider | 高德、和风天气、Brave 的请求/响应/时间/配置回执与局部失败 |
| G5 浏览器与性能 | 主链、确认、刷新、断线、重启、采纳/postcheck 和 P95 |
| G6 Release manifest | 同一 commit/config/dataset/model/rule/provider receipt 的不可变汇总 |

G0～G6 必须在候选 commit 上重新运行。旧 manifest、历史报告或不同 dirty tree 的结果不能拼接。

## 6. Judge 与人工证据

自动语义 Judge 使用独立、无 API 的模型评审流程；运行时 DeepSeek 不得评价自己的输出。结果只标记为 `automated_proxy_judge`，不等于真人校准、public E2E 或发布批准。

`frozen_blind` 标签与开发 Agent、运行模型和 Judge 隔离；blind 失败只进入 `dev/regression` 的复现形式，禁止修改 blind/oracle 以消除失败。

真人内测不属于本 V1 候选版 Gate。候选版通过后，由用户另行批准和设计 consent、样本与反馈流程。

## 7. 禁止替代

Builder、旧推荐指标、历史 RAGAS、synthetic proxy、source prior、旧 release manifest、测试数量或单个演示不得替代上述任何门禁。
