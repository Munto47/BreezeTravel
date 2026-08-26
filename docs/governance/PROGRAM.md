# TC-V1-INTERVIEW-2026 Program

> 状态：`APPROVED`
>
> 授权日期：2026-08-22
>
> 交付周期：24 周内测候选版 + 3 周真人实测闭环

## 1. Program Outcome

交付一条可公开演示、可重放、可故障恢复的「行程查」主链，并形成 Legacy Agent、确定性 Core 和通过准入的 Solver 对比证据；候选版通过 G0～G6 后，经现场批准完成 8～12 人真人实测。

## 2. 固定架构

```text
文本/截图 → OCR/Parser → TripBriefRevision 确认 → ItineraryRevision
→ Provider Adapters → EvidenceSnapshot → AuditEngine
→ Advice/Constraint Repair → 预览采纳 → 新 Revision → 完整 postcheck
```

旁路证据链：

```text
Versioned RunSpec + Fault Profile → Trace/Receipt/Snapshot
→ Replay → Legacy/Core/Solver 消融 → Release Manifest
```

采用模块化单体。PostgreSQL 保存权威状态；Redis 只保存可丢失状态；LangGraph 只编排固定工作流、HITL、SSE 和恢复。

## 3. Program 阶段

| 阶段 | 目标 | 必须退出证据 |
|---|---|---|
| P0-G02 | 权威、架构、API、Roadmap 与 Gate 收口 | 文档/链接/冲突审计与基线检查 |
| P1 | 第 6 周前文本纵向闭环、18 pilot、杀进程恢复 | D1 |
| P2 | TripCheckRun、lease/CAS/幂等、SSE、领域 Trace | Reliability Gate |
| P3 | OCR 隐私闭环、四种交通、天气与风险来源 | Synthetic OCR Phase Gate + G2 + G3；G1/G4 保留为候选证据债 |
| P4 | Advice、CandidateSet、Repair 与 OR-Tools bake-off | Solver Admission Gate |
| P5 | 360 数据、Legacy/Core/Solver 消融、独立 Judge | Evaluation Gate |
| P6 | 同 commit G0～G6、公网候选与 release manifest | Candidate Gate |
| H1 | 8～12 人实测、修复与重新复验 | Human Usability Gate |

## 4. 预批准事项

在不改变本 Program 合同的前提下，以下动作已获开发授权：

- 追加 migration `022`～`024` 的设计、实现和本地/PostgreSQL Gate 执行；
- 增加 PaddleOCR、OpenTelemetry 开发/运行依赖；
- 以实验依赖引入 OR-Tools；只有通过 Solver Admission Gate 后才可进入默认运行时；
- 新增 TripBriefRevision、TripCheckRun、RunSpec、AdviceBundle 和约定的 API；
- 在开发分支提交、推送，并在 Gate 通过后 fast-forward 到 `codex/trip-check-v1-program`；
- 当前 Goal 完成后，按本文件固定顺序归档并生成下一 Goal。
- 在 P1～P6 开发期使用子代理生成 synthetic/dev/regression 数据和截图、执行独立复核与故障诊断；这些产物不得标记为真实用户、人工或公开证据。
- 自动启停隔离的本地 PostgreSQL 等既有依赖服务，并执行既有凭据、固定 18 次上限、零增量费用的高德/和风开发矩阵。

## 5. 自动推进

任何时刻仍只允许一个 `CURRENT_GOAL.md` 为 `APPROVED/IN_PROGRESS`。满足以下全部条件时才自动推进：

- Acceptance cases 和对应 Gate 全部 PASS；
- 工作树干净，commit 已推送且 upstream 可确认；
- evidence 可回读，未使用旧 evidence 拼接；
- 没有 Stop condition；
- 下一 Goal 与本 Program 的阶段模板完全一致。

自动推进只允许开发分支和 Program 集成分支。不得自动合并 `main`。
P3 阶段通过与候选就绪分开计算：P3 不因 G1/G4/G5/G6 尚未运行而失败；这些项必须在 P6 候选 commit 上实际重跑并通过，不能被阶段证据替代。

## 6. 仍需现场批准

- 改变产品范围、降低 Gate、修改 frozen blind/oracle；
- 新增未列入本 Program 的生产依赖或基础设施；
- 使用真实付费 Provider、扩大外部数据范围；
- 公网部署、真人招募/consent、对外能力声明；
- 合并主分支、release、生产部署或改变仓库可见性。

## 7. 停止条件

连续两个切片无法改善同一 Gate、需要扩大范围、新基础设施、修改 blind/oracle、证据矛盾、成本超限、隐私事故，或只能通过降低 Gate 让 LLM/OR-Tools 晋级时立即停止。
