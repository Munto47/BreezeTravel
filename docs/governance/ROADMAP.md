# Trip Intake v2 Roadmap

> 状态：`ACCEPTED`
>
> Program：`TC-INTAKE-V2-2026`

1. I1：冻结 v2 schema、API、迁移、兼容与证据合同。
2. I2：实现 schema-constrained extraction、确定性验证、revision/confirm/materialize 和 PostgreSQL 回读。
3. I3：实现先解析后建 workspace 的文本/截图 UI 与恢复流程，重跑既有 TripCheck 主链。
4. I4：生成并独立复核 120 条数据，冻结 24 条 blind，执行严格 NLU Gate。
5. O1：接入 DeepSeek V4 Flash semantic draft、确定性证据编译和 hybrid fallback。
6. O2：建立真实 prediction runner、通用 scorer、RunSpec 与预算/时延回执。
7. O3：只基于 dev/validation 完成两轮内的通用优化并冻结候选。
8. O4：执行一次 sealed blind，形成 Intake v2 候选重验输入。

每个阶段使用独立可回滚 commit；下一阶段只在定向验证、clean diff、远端 checkpoint 和 evidence readback 后开始。O4 通过后进入 G0～G6 候选重验，不自动进入 H1。全国 live Provider、H1、公网和 production 保持 `NOT_RUN`。
