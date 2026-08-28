# Agent Gate v1

本目录冻结 G01～G07 在 H1 之前使用的多 Agent 模拟审查协议。它只产生
`MULTI_AGENT_SIMULATED_REVIEW` 或 `SEALED_AGENT_BLIND` 证据，永远不产生真人、生产或商业证据。

普通 Gate 由三个互不可见输出的 `gpt-5.6-sol / xhigh` 只读任务完成，再由一个新的
`gpt-5.6-sol / ultra` 任务裁决。候选 commit 变化后旧结论失效。所有必选检查的并集必须实际运行；
`NOT_RUN`、未处理 P0/P1、属于当前Goal的未处理P2或不可回读证据均不能 PASS。

sealed blind 在候选冻结后由独立 Codex 任务运行。开发任务只能得到聚合指标、错误类别、receipt hash
和 PASS/FAIL；原始输入、答案与逐例结果保持仓库外。同一 nonce 和 tranche 只能消费一次。

本目录中的 schema、prompt 与 `protocol_contract.json` 由
`python -m scripts.generate_agent_gate_contracts` 确定性生成或校验。
