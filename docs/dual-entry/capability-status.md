# 「行程查」V1 能力与证据状态

> 更新时间：2026-08-23
> 本表只报告当前状态，不构成产品授权或发布证明。

`TC-P1-G01-text-vertical-slice` 已完成，D1 在受控 fixture、真实 PostgreSQL 和本地受控浏览器三个独立证明层级上为 `PASS`。18 条 pilot 已实际执行为 18/18，三城各 6 条，错城/错 POI 自动接受为 0，Repair 后新增 `BLOCKER/HIGH/UNKNOWN` 为 0。当前只生成了 `TC-P2-G01-reliable-run-and-trace` 的 `DRAFT` 合同，尚未批准或开始 P2。

状态词固定为 `planned / implemented / unit_verified / integration_verified / snapshot_verified / live_verified / publicly_verified / user_validated`。只能使用当前 commit 对应证据达到的最高等级；更高等级不从低等级推断。

| V1 能力 | 当前最高证据 | 当前边界或缺口 |
|---|---|---|
| 文本导入、原文/source span 与解析草稿 | integration_verified | Import → TripBrief → Run 已通过 PostgreSQL 与三城受控浏览器主链；未执行真实用户文本或公网入口 |
| 截图上传、本地 PaddleOCR、原图终态清理 | planned | 尚无目标接口、清理回执和三图 P95 证据 |
| 版本化 TripBrief 与确认档案复用 | integration_verified | migration 022、文本 Import 同事务建档、字段 provenance、`NO_PREFERENCE`、ETag/幂等修订与确认已通过真实 PostgreSQL 故障矩阵 |
| 单城市、2～5 人、2～5 天早期拒绝 | planned | 现有城市/解析逻辑不能替代统一入口边界 |
| 地点消歧与城市归属 | snapshot_verified | 冻结 CandidateSet 的 18 条 pilot 与 BJ-02 浏览器确认门禁通过；仅证明受控 fixture，不代表 live POI 数据 |
| EvidenceSnapshot 与唯一 Audit Engine | snapshot_verified | 同一 RunSpec 下的 Evidence、receipt、Audit、replay 已落盘并通过 D1；真实 Provider 仍为 `NOT_RUN` |
| 驾车路线 | integration_verified | 已有 Provider/fixture 路径；不代表 V1 live gate 已通过 |
| 步行、公交路线 | implemented | 存在历史能力或适配代码，尚未按 V1 receipt 和门禁完整验证 |
| 骑行路线 | planned | 需要统一适配器、receipt 与失败语义 |
| 营业、预约、酒店往返、用餐窗口 | implemented | 规则和局部证据可复用；缺 V1 PostgreSQL/快照/live 完整矩阵 |
| 偏好、节奏与活动强度 | implemented | 历史约束底座可复用；需区分 HARD/NO_PREFERENCE/INFERRED 和建议性证据 |
| 天气硬冲突与月份适配 | implemented | 需分离确定性和建议性输出，并重跑真实 Provider 门禁 |
| Brave 风险搜索与 RiskEvidence | planned | 未实现查询预算、来源优先级、TTL/成本和结构化合同 |
| AdviceBundle 行动建议覆盖 | snapshot_verified | D1 受控 Finding 已映射行动、预期影响和不确定性；具体地点只来自冻结 CandidateSet，live 数据未验证 |
| 受控 CandidateSet、路线变化与 receipt | snapshot_verified | D1 候选与路线 receipt 绑定并参与完整 postcheck；不宣称为 live CandidateSet |
| 采纳 → 新 revision → 完整 postcheck | snapshot_verified | Advice/Repair 采纳创建新 revision、旧报告 stale、完整 postcheck 与零新增高风险门禁已通过 18 pilot |
| TripCheckRun 阶段持久化、SSE 与重启恢复 | integration_verified | migration 023、RunSpec/config hash、lease/CAS、幂等、SSE 续传和副作用 receipt 已通过真实 PostgreSQL、终止恢复与受控浏览器 D1；六类 Reliability Gate 尚未完整闭合 |
| 18 pilot / 360 条目标数据集 | integration_verified | 18 条已实际执行为 18/18、6/6/6；180 dev / 72 regression / 90 frozen blind 尚未建设 |
| 无 API 独立自动 Judge | planned | 历史 synthetic proxy/LLM Judge 不等于目标 `automated_proxy_judge` |
| V1 受控浏览器主链与 D1 manifest | integration_verified | 北京、上海、杭州主链及 BJ-02 歧义确认共 4 条通过，D1 manifest 可回读；公网、P95 与候选版 G0～G6 仍未运行 |
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
