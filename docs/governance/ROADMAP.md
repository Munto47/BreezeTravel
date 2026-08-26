# 「行程查」V1 纵向 Roadmap

> 状态：`ACCEPTED`
>
> Program：`TC-V1-INTERVIEW-2026`
>
> 周期：24 周内测候选版 + 3 周 H1 真人实测闭环

任何时刻只执行 `CURRENT_GOAL.md` 中一个 `APPROVED/IN_PROGRESS` 切片。阶段 Gate、clean tree、远端 checkpoint 和 evidence readback 全部通过后，才可按 `PROGRAM.md` 自动生成下一 Goal。

## 1. 阶段顺序

| 阶段 | 周期 | 首要用户结果 | 核心实现 | 退出 Gate |
|---|---:|---|---|---|
| P0-G02 | 当前 | 架构、API、数据、Gate 和自动推进无冲突 | Program、ADR、API 合同、文档权威迁移 | 文档 Gate |
| P1 文本纵向闭环 | 第 3～6 周 | 文本行程完成确认、核验、修复、采纳和复验 | Brief、fixture Evidence、Audit、Advice adapter、Repair、恢复 | D1 |
| P2 可靠运行 | 第 7～10 周 | 刷新、断线、重复提交、并发和重启可恢复 | TripCheckRun、lease、CAS、幂等、SSE、Trace | Reliability Gate |
| P3 输入/Provider 完整性 | 第 11～14 周 | 截图、四种交通、天气和风险来源进入主链 | OCR 隐私、Provider adapter、RiskEvidence | Synthetic OCR Phase Gate + G2 + G3；G1/G4 为候选证据债 |
| P4 Advice/Repair | 第 15～18 周 | 每个问题有行动方式和安全候选 | AdviceBundle、CandidateSet、RepairEngine、OR-Tools bake-off | Solver Admission Gate |
| P5 评测与消融 | 第 19～22 周 | 每次变更可与稳定基线比较 | 360 数据、Legacy/Core/Solver、snapshot、Judge | Evaluation Gate |
| P6 候选与公网证据 | 第 23～24 周 | 可公开演示的 V1 内测候选版 | G0～G6、live receipts、性能、manifest、视频 | Candidate Gate |
| H1 真人实测 | 第 25～27 周 | 8～12 人完成受控/真实任务并闭环缺陷 | consent、访谈、regression、重新复验 | Human Usability Gate |

## 2. P1 必须在第 6 周前完成的链路

```text
文本 Import → TripBrief 确认 → 歧义 POI 确认 → EvidenceSnapshot
→ 一个事实/路线冲突 → Advice → Repair 预览 → 新 Revision
→ 完整 postcheck → Evidence 后杀进程并恢复
```

- 北京、上海、杭州各至少一个浏览器主链；
- Provider 使用受控 fixture；
- 18 条 pilot 同步建立，每城 6 条；
- OCR 只做隔离技术验证，不阻塞 D1；
- 重启不得重复创建 Provider 副作用、repair 或 revision。

## 3. 数据增长

| 时间点 | pilot | dev | regression | frozen blind | 规则 |
|---|---:|---:|---:|---:|---|
| P1 | 18 | 0 | 0 | 0 | 先验证合同、runner 和 oracle |
| P2/P3 | 18 | 逐步到 180 | 随缺陷追加 | 0 | 修复故障必须进入 regression |
| P4 | 18 | 180 | 72 | 0 | schema/oracle 冻结准备 |
| P5 | 18 | 180 | 72 | 90 | blind 由隔离流程生成并封存 |

最终总计 360 条，北京、上海、杭州各 120。同源和变异案例不得跨 split；blind 失败只回流为 dev/regression 复现。

## 4. OR-Tools 准入

P4 通过统一 RepairEngine 比较 BoundedRepair、RoutingModel/TSPTW 和 CP-SAT。新策略必须先满足零新增 BLOCKER/HIGH/UNKNOWN，再按完整 postcheck 成功率、P95、编辑成本和路线代价比较。未通过 Gate 只保留实验代码和报告。

## 5. 证据实验台

每个阶段持续维护：

- `RunSpec`：commit/config/model/prompt/rule/provider/dataset/snapshot/fault/budget；
- 六类 fault：Provider timeout、字段部分失败、重复提交、并发编辑、进程终止、config 漂移；
- Trace、receipt、snapshot、replay 和原始指标；
- Legacy A、Core B、Solver C 的同 RunSpec 结果；
- 同绑定 release manifest。

实验台不需要大型运营后台；提供 CLI artifact 和一个受控演示页面即可。

## 6. 分支与 checkpoint

- Program 集成分支：`codex/trip-check-v1-program`；
- 阶段分支：`codex/trip-check-p<n>-<scope>`；
- 每个可验证切片执行定向检查、显式暂存、commit 和 push；最长 60 分钟形成远端 checkpoint；
- Gate 通过后可在 Program 授权内 fast-forward 到集成分支；不得自动合并 `main`。

## 7. H1

H1 只有在 G0～G6 全部通过、公网和招募现场批准、consent/删除/退出机制完成后开始。每名参与者完成一个统一任务，并可自愿使用自己的真实行程。严重误导、主链阻断和隐私问题必须进入 regression，修复后重跑相关 Gate 和 manifest。
