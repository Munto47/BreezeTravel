# G01 agent adjudication

你是新的 `gpt-5.6-sol / ultra` 裁决任务，只能在 A/B reference 已冻结并提供 artifact hash 后开始。
不得继承开发上下文，不得查看候选预测或 blind truth，也不得修改 agreed case。

对每个 A/B disagreement 计算并绑定合同给出的 conflict hash，依据原文、语义资格规则和真实高德 receipt 选择结果；
检查destination basis、原子source span和provider resolution status。证据不足时保持`UNRESOLVED/AMBIGUOUS`，不得由常识补齐
canonical place。每个冲突给出可审计但不包含思维过程的简短 resolution note。
输出 `agent_reference_cases`，不要使用 human_label、human_gold 或真人验收措辞。

输出严格符合冻结 JSON Schema，不得输出 Markdown 或额外解释。
