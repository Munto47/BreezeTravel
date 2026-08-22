# ADR-004：「行程查」V1 单入口与 Builder 冻结

- 状态：Accepted
- 日期：2026-08-22
- 取代范围：ADR-001 中“双入口并行推进”和 P0～P8/M1～M4 阶段顺序
- 适用范围：北京、上海、杭州；2～5 人；2～5 天单城市行程

## 背景

双入口方案建立了 revision、Evidence、Audit、Repair、SuggestionSet 和恢复等可复用底座，但同时推进 Import、Builder、成员协同和多层评测，使产品目标、阶段进度和证据晋级混在一起。代码与测试数量增长没有稳定对应到一个可由用户完成的闭环。

## 决策

BreezeTravel V1 只建设「行程查」：输入已有文本或截图，确认 TripBrief，完成事实核验、风险与建议，采纳后创建新 revision 并完整 postcheck。

现有 Builder、路线模板、拖拽、Planner、RAG、多 Agent、LoRA 和 Yjs 作为冻结资产保留。只维护防止「行程查」改动破坏现有代码的最低回归，不新增 Builder 产品能力，不把其历史 evidence 用于 V1 晋级。

实施只按 `docs/governance/ROADMAP.md` 的 P0～P6 阶段和 `CURRENT_GOAL.md` 的单一切片推进。任何阶段晋级均需人工批准。

## 后果

- 团队可以围绕一条可验证用户链集中开发与测试；
- 历史底座通过 adapter 渐进复用，不进行一次性重写；
- 旧双入口 Final 2.0 归档，不再具有权威效力；
- ADR-001 对 revision、Evidence、Audit 和 Repair 主干的决定继续有效，但双入口优先级与旧阶段顺序被本 ADR 取代；
- 恢复 Builder、扩城或跨城必须新建产品 Goal 和 ADR，经用户批准。

## 验证

文档索引、根指导、README、能力状态和 release manifest 必须统一指向当前权威文件；源码中保留的 Builder 不得出现在 `CURRENT_GOAL` 的 Scope 中。
