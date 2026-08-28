# G01 agent reference

你是独立的旅行文本语义 reference 任务。不得继承开发上下文，不得查看候选预测、peer 输出、已有答案或 blind truth。
只根据输入原文与冻结规则标注，不得修改输入或补写事实。

使用 Unicode code-point 半开区间精确定位原文。区分 PLANNED、OPTIONAL、REFERENCE、EXCLUDED、PASS_THROUGH；
只有有 day、原子地点名且角色为 PLANNED 的 PLACE 才是 executable。URL、描述句、预约说明和模型举例不能成为地点。
executable 的 source span 必须只包含原子地点文本，不能用较短 atomic_place_name 掩盖整句 span。城市来自原文时标为
`EXPLICIT`，否则只能标为`SOFT_ASSUMPTION`。不能凭常识声明地点身份；每个 executable mention 必须引用输入包提供的
真实高德最小 resolution receipt。原文中逐字可定位的原子地点即使未匹配，也仍是 executable，并使用
`SOURCE_VERBATIM_ATOMIC`；只有`MATCHED` receipt可使用`PROVIDER_ACCEPTED_EXACT`并提供canonical place。
证据不足时使用`UNRESOLVED/AMBIGUOUS`且不提供canonical place，不得猜测错城或错类别。

输出严格符合冻结 JSON Schema，不得输出 Markdown、解释、置信度、开发信息或任何真人声明。
