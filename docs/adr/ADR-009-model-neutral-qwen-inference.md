# ADR-009：模型中立结构化推理与 Qwen 主模型族

- 状态：Accepted；人工配置readback部分由ADR-013取代
- 日期：2026-08-27
- Program：`TC-VNEXT-2026`

## 背景

项目已经固化DeepSeek hybrid抽取、Unicode证据编译、RunSpec和失败fallback，但历史Validation曾暴露角色反转和幻觉。直接把业务绑定到某个模型或依赖模型confidence，会让能力随版本漂移。

用户决定后续开发和测试使用Qwen，同时要求能力固化在项目中而不是模型中。

## 决策

业务层只依赖 `StructuredInferenceProvider`：

- task和schema version；
- 经本地遮蔽后的 `redacted_input_payload`；
- fixed model binding；
- prompt/config hash；
- deadline和failure policy。

输出为validated proposal与 `InferenceReceipt`。服务端负责证据编译、语义枚举、冲突降级、地点资格和确定性fallback。

模型面板：

- Qwen Max：质量上限和初始开发benchmark候选；
- Qwen Plus：主要生产候选；
- Qwen Flash：低延迟候选；
- DeepSeek：冻结Baseline，不作静默fallback。

fixed model binding必须冻结provider、region、endpoint、不可漂移exact model ID、structured-schema能力、非思考模式、温度、prompt/schema/config hash、pricing version与币种。首次schema失败最多一次带错误反馈的修复调用，之后确定性PARTIAL。所有调用记录token、延迟、失败、修复、fallback和估算费用。

G01从当前环境安全读取既有凭据，并通过官方模型目录自动读回账号可用性、区域、exact model ID、endpoint、上下文和Provider可暴露的价格字段；未暴露字段写`NOT_EXPOSED_BY_PROVIDER`。开发benchmark不等于产品默认，未通过Validation前不得写入候选release配置。该自动readback与H1前Agent Gate边界由ADR-013补充。

## 晋级

挑战模型必须：

1. Max/Plus/Flash只用dev调试、用validation选择；prompt/schema/threshold和最小预测分母保持一致；
2. 全部零容忍和Validation硬Gate通过；
3. 质量相对Validation最佳下降≤0.5个百分点；
4. P95改善≥20%才因性能替换；
5. 选择唯一候选并冻结model/prompt/schema/config/threshold后，sealed blind只正式运行一次；答案由不继承开发上下文的独立Codex任务在仓库外保管，开发任务与运行模型不可读；
6. blind失败后只从独立dev/regression故障族修复；只有输入分布或产品schema实质变化并经独立批准才新建blind版本，旧blind永不重新成为选择集。

## 后果

- 可以更换模型而不改业务合同。
- 模型失败仍返回部分可编辑结果。
- DeepSeek历史能力被保留但不会污染新版证据。
- 需要维护adapter、receipt和模型消融。

## 不采用

- 使用Qwen-Agent作为运行时多Agent：当前是固定工作流，不需要自主工具规划。
- 隐藏跨模型fallback：破坏可复现性和成本回读。
- 让模型决定最终POI或Finding：越过Provider/Audit权威。
- 仅凭公开榜单选模型：必须在本项目冻结数据上比较。

## 隐私

发送模型前遮蔽手机号、证件号、订单号等高风险信息。日志和receipt不保存密钥、完整原文、完整prompt或未脱敏响应。生产前固定账号方案、区域和数据使用条款。
