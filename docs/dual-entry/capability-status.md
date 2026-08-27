# 「行程查」V1 能力与证据状态

> `LEGACY_CAPABILITY_SNAPSHOT / NOT_VNEXT_AUTHORITY`：下表冻结2026-08-23旧Program状态，历史数值不改写；它不能成为`TC-VNEXT-2026`当前Goal、Gate或完成声明。新版状态只读[`../governance/CURRENT_GOAL.md`](../governance/CURRENT_GOAL.md)。

> 更新时间：2026-08-23
> 本表只报告当前状态，不构成产品授权或发布证明。

`TC-P1-G01`～`TC-P4-G01` 已按 Program 完成阶段 Gate；当前唯一活动 Goal 为 `TC-P5-G01-evaluation-ablation`。P5 v2 的 360 条数据合同、actual OCR materialization、外置 blind commitment 与 seal 已达到 `READY`，但 1080 条正式终态/replay、三轮 Judge 和 Evaluation Gate 仍为 `NOT_RUN`，不能据此进入 P6。

状态词固定为 `planned / implemented / unit_verified / integration_verified / snapshot_verified / live_verified / publicly_verified / user_validated`。只能使用当前 commit 对应证据达到的最高等级；更高等级不从低等级推断。

| V1 能力 | 当前最高证据 | 当前边界或缺口 |
|---|---|---|
| 文本导入、原文/source span 与解析草稿 | integration_verified | Import → TripBrief → Run 已通过 PostgreSQL 与三城受控浏览器主链；未执行真实用户文本或公网入口 |
| 截图上传、本地 PaddleOCR、原图终态清理 | snapshot_verified | P3 synthetic stress 与 P5 actual PaddleOCR materialization/清理回执已通过；候选版 60 张授权公开来源 G1 和三图 P95 仍为 `NOT_RUN`，合成截图不能替代 |
| 版本化 TripBrief 与确认档案复用 | integration_verified | migration 022、文本 Import 同事务建档、字段 provenance、`NO_PREFERENCE`、ETag/幂等修订与确认已通过真实 PostgreSQL 故障矩阵 |
| 单城市、2～5 人、2～5 天早期拒绝 | planned | 现有城市/解析逻辑不能替代统一入口边界 |
| 地点消歧与城市归属 | snapshot_verified | 冻结 CandidateSet 的 18 条 pilot 与 BJ-02 浏览器确认门禁通过；仅证明受控 fixture，不代表 live POI 数据 |
| EvidenceSnapshot 与唯一 Audit Engine | snapshot_verified | 同一 RunSpec 下的 Evidence、receipt、Audit、replay 已落盘并通过 D1；真实 Provider 仍为 `NOT_RUN` |
| 驾车路线 | snapshot_verified | P3 统一适配器与固定 snapshot 已验证；候选版 G4 live receipt 仍为 `NOT_RUN` |
| 步行、公交路线 | snapshot_verified | P3 统一适配器、receipt 与局部失败语义已通过固定 snapshot；不替代 live Provider |
| 骑行路线 | snapshot_verified | P3 统一适配器、receipt 与局部失败语义已通过固定 snapshot；不替代 live Provider |
| 营业、预约、酒店往返、用餐窗口 | implemented | 规则和局部证据可复用；缺 V1 PostgreSQL/快照/live 完整矩阵 |
| 偏好、节奏与活动强度 | implemented | 历史约束底座可复用；需区分 HARD/NO_PREFERENCE/INFERRED 和建议性证据 |
| 天气硬冲突与月份适配 | implemented | 需分离确定性和建议性输出，并重跑真实 Provider 门禁 |
| Brave 风险搜索与 RiskEvidence | planned | 未实现查询预算、来源优先级、TTL/成本和结构化合同 |
| AdviceBundle 行动建议覆盖 | snapshot_verified | D1 受控 Finding 已映射行动、预期影响和不确定性；具体地点只来自冻结 CandidateSet，live 数据未验证 |
| 受控 CandidateSet、路线变化与 receipt | snapshot_verified | D1 候选与路线 receipt 绑定并参与完整 postcheck；不宣称为 live CandidateSet |
| 采纳 → 新 revision → 完整 postcheck | snapshot_verified | Advice/Repair 采纳创建新 revision、旧报告 stale、完整 postcheck 与零新增高风险门禁已通过 18 pilot |
| TripCheckRun 阶段持久化、SSE 与重启恢复 | integration_verified | 六类 Reliability PostgreSQL 矩阵 6/6；实际 worker 终止、lease 接管、幂等、并发 revision、config drift、SSE 重连与乱序去重均通过；不代表公网或生产 SLO |
| 脱敏 OTel 与 PostgreSQL 领域 Trace | integration_verified | `run → stage → provider_attempt` 与 Run/Event/Attempt/Receipt 领域 Trace 关联率 100%，敏感属性命中 0；OTel 不替代 PostgreSQL 权威状态 |
| 18 pilot / 360 条目标数据集 | snapshot_verified | 18/180/72/90、三城 120/120/120 的 P5 v2 数据合同和 seal 为 READY；1080 条正式输出/replay、blind aggregate 与 Evaluation Gate 仍为 `NOT_RUN` |
| 无 API 独立自动 Judge | implemented | v2 bundle/round/panel 合同已实现；正式三轮独立 Judge 尚未执行，`human_calibration_performed=false` |
| V1 受控浏览器主链与 D1/Reliability manifest | integration_verified | 三城主链、BJ-02、刷新恢复、SSE 游标续传及重复/乱序事件去重通过，两个 manifest 可回读；公网、P95 与候选版 G0～G6 仍未运行 |
| 真人内测 | planned | V1 候选版之后的独立阶段，当前为 0 |
| 拖拽 Builder、Planner、RAG、多 Agent、LoRA、Yjs | frozen_legacy_asset | 保留代码和最低回归；不在当前开发范围，不作为 V1 能力声明 |

## 固定范围

- 城市：北京、上海、杭州；
- 人数：2～5 人；
- 天数：2～5 天；
- 输入：文本、手工文字、截图；
- 当前产品入口：仅「行程查」；
- 禁止扩城、跨城、新 Agent、MQ、Kubernetes、GraphRAG、重新微调和主动 Builder 开发。

历史 P0～P8 与 M1～M4 仅存在于归档方案和旧报告中，不再作为当前进度体系。
