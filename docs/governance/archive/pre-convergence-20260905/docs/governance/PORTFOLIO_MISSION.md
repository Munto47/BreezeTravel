# BreezeTravel 工程与公开证据使命

> 状态：`ACCEPTED`
>
> Program：`TC-VNEXT-2026`
>
> 生效日期：2026-08-27

## 1. 使命

BreezeTravel 首先解决真实旅行组织者的问题，同时形成能够经受 AI 应用与可靠后端岗位追问的工程证据。

技术亮点必须来自真实问题：

- 长文本语义编译而不是逐句搜索；
- 高精度地点匹配与失败保留；
- revision 绑定的后台地图和手动更新；
- Provider事实、局部失败和可回放回执；
- 模型中立接口、冻结消融和成本/延迟账本；
- 隐私、许可、幂等、并发和恢复边界。

不得为了简历关键词牺牲用户体验、事实正确性或证据诚实度。

## 2. 四类成功

### 2.1 产品成功

用户无需理解内部流程即可得到可信卡片、路线、住宿和少量重点建议。第一优先指标是用户结果，不是代码量或测试数量。

### 2.2 工程成功

每个核心设计能回答：

- 为什么存在；
- 状态归谁所有；
- LLM与确定性代码如何分工；
- 失败时间线和恢复方式；
- 幂等、事务、revision和Provider边界；
- 验证命令、当前红灯和回滚方案。

### 2.3 面试成功

可以准确使用 FastAPI、PostgreSQL、LangGraph、Qwen、Evidence、约束修复、OpenTelemetry和故障注入等术语，但必须同时展示准入实验、失败结果和未完成证据。

### 2.4 公开证据成功

候选版包含同一 commit/config/dataset/model/provider 绑定的：

- 产品演示；
- 结构化评测；
- snapshot replay；
- Provider局部失败；
- 并发、幂等和恢复；
- 架构与时序图；
- release manifest。

自动、live Provider、公网、真人和商业证据分别报告。

## 3. 技术准入

新组件进入默认运行时前必须：

1. 对应已定义的用户或可靠性问题；
2. 有明确合同、权限、预算、失败和降级；
3. 在冻结数据或实验上优于 Baseline；
4. 绑定可回读 commit、RunSpec、原始 artifact 和 receipt；
5. 不扩大隐私、许可或生产风险。

旧 ReAct/Critic、Planner、Yjs、LoRA和无来源 RAG 是冻结 Baseline。禁止为炫技新增运行时多 Agent、微服务、消息队列、Kubernetes、GraphRAG或重新微调。

## 4. 表述边界

- Blueprint、代码存在和Dev PASS不等于候选版。
- Candidate PASS不等于真人可用、生产或商业验证。
- H1只是小样本真人可用性证据，不等于市场验证。
- 付费、留存和增长必须由真实行为证明。
- 历史 Intake、Builder或Candidate结果不自动适用于新产品 commit。

项目目标是形成明显高于普通旅行聊天机器人和CRUD项目的证据密度，但不使用“行业最好”等无法证明的绝对声明。
