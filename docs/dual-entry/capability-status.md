# 「行程查」V1 能力与证据状态

> 更新时间：2026-08-22
> 本表只报告当前状态，不构成产品授权或发布证明。

当前 Goal 为 `TC-P1-G01-text-vertical-slice`。P1 已完成 TripBrief、Run 持久化合同/API 和 18 条 pilot 结构门禁，但 Audit → Advice → Repair → postcheck、PostgreSQL 实迁和浏览器主链尚未闭合，因此 D1 仍为 `NOT_RUN`。

状态词固定为 `planned / implemented / unit_verified / integration_verified / snapshot_verified / live_verified / publicly_verified / user_validated`。只能使用当前 commit 对应证据达到的最高等级；更高等级不从低等级推断。

| V1 能力 | 当前最高证据 | 当前边界或缺口 |
|---|---|---|
| 文本导入、原文/source span 与解析草稿 | integration_verified | 现有 Import 主链已接通 TripBrief 草稿和字段 provenance；真实 PostgreSQL migration/事务 Gate 尚未运行 |
| 截图上传、本地 PaddleOCR、原图终态清理 | planned | 尚无目标接口、清理回执和三图 P95 证据 |
| 版本化 TripBrief 与确认档案复用 | integration_verified | migration 022、文本 Import 同事务建档、字段 provenance、`NO_PREFERENCE`、ETag/幂等修订与确认已通过受控 API 集成测试；PostgreSQL Gate 未运行 |
| 单城市、2～5 人、2～5 天早期拒绝 | planned | 现有城市/解析逻辑不能替代统一入口边界 |
| 地点消歧与城市归属 | integration_verified | 现有受控链路可复用；V1 错城/错 POI 零接受门禁尚未重跑 |
| EvidenceSnapshot 与唯一 Audit Engine | integration_verified | 已有不可变快照/规则底座；需按 V1 事实字段与同一配置 Run 收口 |
| 驾车路线 | integration_verified | 已有 Provider/fixture 路径；不代表 V1 live gate 已通过 |
| 步行、公交路线 | implemented | 存在历史能力或适配代码，尚未按 V1 receipt 和门禁完整验证 |
| 骑行路线 | planned | 需要统一适配器、receipt 与失败语义 |
| 营业、预约、酒店往返、用餐窗口 | implemented | 规则和局部证据可复用；缺 V1 PostgreSQL/快照/live 完整矩阵 |
| 偏好、节奏与活动强度 | implemented | 历史约束底座可复用；需区分 HARD/NO_PREFERENCE/INFERRED 和建议性证据 |
| 天气硬冲突与月份适配 | implemented | 需分离确定性和建议性输出，并重跑真实 Provider 门禁 |
| Brave 风险搜索与 RiskEvidence | planned | 未实现查询预算、来源优先级、TTL/成本和结构化合同 |
| AdviceBundle 行动建议覆盖 | planned | 现有 Repair 可复用；尚无每个非 PASS 100% 覆盖合同 |
| 真实 CandidateSet、路线变化与 receipt | implemented | 旧 SuggestionSet 资产存在；需冻结为 V1 Advice 候选并禁止无证据地点 |
| 采纳 → 新 revision → 完整 postcheck | integration_verified | 现有 Repair/EditCommand 主干可复用；需接入 AdviceBundle 并按 V1 零回归门禁重跑 |
| TripCheckRun 阶段持久化、SSE 与重启恢复 | implemented | migration 023、不可变 RunSpec/config hash、创建/恢复幂等、lease、ETag、SSE 续传与 side-effect receipt 已实现并通过内存仓储 API 测试；实际进程终止和 PostgreSQL 接管未运行 |
| 360 条 import-only v2 数据集 | implemented | 18 pilot 合同已建立并通过 6/6/6 结构校验，但 `execution_status=NOT_RUN`；180 dev / 72 regression / 90 frozen blind 尚未建设 |
| 无 API 独立自动 Judge | planned | 历史 synthetic proxy/LLM Judge 不等于目标 `automated_proxy_judge` |
| V1 浏览器恢复、性能与 release manifest | planned | 旧 Builder/G5/release manifest 不可替代 V1 G0～G6 |
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
