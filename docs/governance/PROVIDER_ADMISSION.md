# 模型与 Provider 准入表

> 状态：`ACTIVE`
>
> Program：`TC-VNEXT-2026`
>
> 日期：2026-08-28

## 1. 准入状态

| 能力 | Provider/实现 | 开发状态 | 默认运行时 | 持久化边界 | 生产前要求 |
|---|---|---|---|---|---|
| 文本语义质量上限 | Qwen Max exact binding | G01预批准；从现有凭据自动目录readback | 开发benchmark候选 | 只存receipt/hash/指标，不存密钥与完整原文 | 区域、endpoint、exact ID、隐私与模型Gate |
| 文本语义生产候选 | Qwen Plus exact binding | G01预批准；从现有凭据自动目录readback | Validation达标后可成为唯一候选 | 同上 | 与Max同dev/validation比较，不能用于blind选模 |
| 文本低延迟候选 | Qwen Flash exact binding | G01预批准；从现有凭据自动目录readback | 否 | 同上 | dev/validation的schema、质量和P95均达标 |
| 历史语义Baseline | DeepSeek固定版本 | 只读/消融 | 否 | 复用历史receipt，不作fallback | 不得晋级新版证据 |
| OCR基线 | 本地PaddleOCR | 已有资产，G04适配 | G04候选 | OCR box/hash；原图终态删除 | 真实来源OCR Gate |
| 视觉模型 | Qwen-VL固定snapshot | G04实验 | 否 | 不保存未脱敏模型响应 | bbox/顺序/卡片/性能消融胜出 |
| POI搜索 | 高德Web Service | `OWNER_ATTESTED_EXISTING_AUTHORIZATION`，G01可开发 | G01候选 | 只保留最小规范化地点事实与脱敏回执 | 生产/公开展示前核对数据留存范围 |
| 路线 | 高德步行/公交 | `OWNER_ATTESTED_EXISTING_AUTHORIZATION`，G01可开发 | G01后台/G02展示候选 | 规范化事实最小留存；geometry默认短期缓存 | 商业展示、长期缓存和域名许可 |
| 天气/预警 | 和风天气 | 历史已有，G03使用 | G03候选 | observed/effective/expires和receipt | 当前套餐、归因和使用范围核对 |
| 风险发现 | 未指定 | 未准入 | 否 | 未取得存储权不得入Evidence | 原始官方来源、成本和许可 |
| 官方知识 | 景区/政府/运营方 | G05待准入 | 否 | KnowledgeClaim短证据与时效 | robots/条款/许可/更新策略 |
| 授权创作者内容 | 待合作 | G05待准入 | 否 | 只保存授权范围 | 书面授权和撤回流程 |
| 小红书笔记 | 无授权接口 | BLOCKED | 否 | 禁止抓取/持久化 | 官方授权能力和明确许可 |
| 运营商本机号 | 阿里云号码认证 | 独立实验待批准 | 否 | 最小认证回执 | 账号、审核、费用、consent和短信fallback |

## 2. StructuredInferenceProvider 合同

运行时业务层只依赖：

- task；
- schema_version；
- `redacted_input_payload`；
- provider、region、endpoint、exact model ID、structured-schema能力；
- deadline；
- failure policy。

结果必须包含：

- validated proposal；
- schema/model/prompt/input/output hash；
- token、latency、repair call、fallback；
- error category；
- estimated cost。

binding还必须记录非思考模式、温度、prompt/schema/config hash、pricing version与币种。G01使用官方模型目录自动发现区域、endpoint、当前账户可用exact ID和上下文；Provider未暴露的workspace、价格或币种字段写`NOT_EXPOSED_BY_PROVIDER`，不得猜测，也不得继续向用户索要。不存在可回读exact ID或区域时，该候选`NOT_READY`并继续自动诊断。

禁止：

- 模型直接调用地图或数据库；
- 模型返回的POI/路线成为权威；
- 隐藏重试或跨模型静默fallback；
- 在日志/receipt保存密钥、完整原文或未脱敏响应。

## 3. 模型晋级规则

1. Max建立质量上限。
2. Plus和Flash使用完全相同的冻结prompt/schema与dev/validation。
3. 所有零容忍和Validation硬门禁先通过，并冻结最小预测分母。
4. 质量相对Validation最佳下降不超过0.5个百分点。
5. P95至少改善20%才因性能替换。
6. 只选一个候选并冻结全部binding后运行sealed blind一次；答案由不继承开发上下文的独立Codex任务保管，模型和开发任务不可读。
7. blind失败进入新dev/regression故障族，不能回看标签选另一个模型；只有输入分布/schema变化并经独立批准才创建新blind版本。
8. 失败模型保持实验或Baseline，不通过文案掩盖。

## 4. 高德边界

G01/G02只在现有开发授权和无增量费用范围调用，授权依据记录为`OWNER_ATTESTED_EXISTING_AUTHORIZATION`；无需用户重复上传书面证明。

开发Gate只允许以下最小持久化集：

- canonical place ID与最小展示字段；
- normalized route duration/distance/transfer；
- query/config/response hash；
- observed_at、failure和receipt。

路线geometry和完整响应默认只进入受限短期缓存。生产、公开演示或商业化前必须核对高德条款并确认缓存、拆分展示、持久化和商业使用范围。未解决时状态为`BLOCKED_PRODUCTION`，不是G01～G07开发代码失败。

## 5. 知识与社交内容

知识进入运行时前必须证明：

- 来源身份；
- 许可和保存权；
- 抓取/访问方式；
- 更新与过期；
- 引用和删除；
- 是否只能建议、不能作为HARD。

小红书当前不进入数据管线。用户复制或上传的内容只服务该用户的行程，不能因此进入公共知识库或训练集。

## 6. 隐私与区域

模型启用前固定账号方案、访问区域和数据使用条款。发送模型前本地遮蔽手机号、证件号、订单号等高风险字段。登录用户原始文本与可还原SourceClaim加密保存，默认最长30天或直到删除行程/账号；到期只保留不可逆hash、结构化结果、版本和删除回执。它们不进入应用日志、trace、分析事件或模型receipt。DEMO固定示例的匿名编辑24小时清理。

任何Provider条款变化、区域迁移、新账号/套餐或付费升级都需要更新本表和对应Goal。
