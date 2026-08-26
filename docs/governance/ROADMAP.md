# Trip Intake v2 Roadmap

> 状态：`ACCEPTED`
>
> Program：`TC-INTAKE-V2-2026`

1. I1：冻结 v2 schema、API、迁移、兼容与证据合同。
2. I2：实现 schema-constrained extraction、确定性验证、revision/confirm/materialize 和 PostgreSQL 回读。
3. I3：实现先解析后建 workspace 的文本/截图 UI 与恢复流程，重跑既有 TripCheck 主链。
4. I4：生成并独立复核 120 条数据，冻结 24 条 blind，执行严格 NLU Gate。

每个阶段使用独立可回滚 commit；下一阶段只在定向验证、clean diff、远端 checkpoint 和 evidence readback 后开始。全国 live Provider、H1、公网和 production 保持 `NOT_RUN`。
