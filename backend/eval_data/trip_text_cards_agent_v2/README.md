# G01 Text Card agent evaluation v2

该合同在不改写 `trip_text_cards_v1` 任一已绑定字节的前提下，为 G01 增加两份独立
`gpt-5.6-sol / xhigh` reference 与一份新的 `gpt-5.6-sol / ultra` adjudication。

- A/B 使用相同冻结 prompt、schema 和 input bundle，但使用不同 task 与 assignment；
- A/B 看不到候选预测、peer 输出或已有答案；
- adjudicator 只能在 A/B 冻结后启动，并精确绑定两份 artifact hash；
- 每个 executable mention 必须绑定仓库外的高德 live resolution receipt；只有`MATCHED`才可给canonical place，`UNRESOLVED/AMBIGUOUS`必须保持无canonical；
- 原始 reference、adjudication 和 Provider index 均保持仓库外；
- Git 只保存 schema、prompt、contract、hash、聚合指标与脱敏验证 receipt；
- 证据只能称 `MULTI_AGENT_SIMULATED_REVIEW`，真人/H1/生产保持 `NOT_RUN`。

普通 v2 reference 只允许 dev/validation。sealed blind 使用独立、仓库外 tranche 与 Agent Gate 的
one-shot receipt，不能通过本目录工具读取旧 `frozen_blind` truth。
