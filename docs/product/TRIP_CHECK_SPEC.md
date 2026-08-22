# 「行程查」V1 产品与行为规格

> 状态：`ACCEPTED`
>
> 版本：1.0
> 日期：2026-08-22

## 1. 完整用户流程

```text
文本/截图输入
→ OCR 与结构解析
→ 用户确认城市、日期、人数、到离、酒店、交通方式和偏好
→ 地点消歧与城市归属核验
→ 营业、交通、酒店衔接、强度、天气与风险分析
→ 必须调整 / 建议优化 / 待确认
→ 行动建议 + 真实备选地点
→ 预览并采纳
→ 新 Revision
→ 完整复验
```

任何步骤失败都应保留已经成功且配置一致的阶段结果，并给出可恢复状态；不得把不同配置的阶段输出拼成一次完成的 Run。

## 2. 输入合同

### 2.1 文本

支持粘贴文本和手工输入。系统保存原文、解析版本、source span、字段置信度和确认状态。Prompt injection 或无法解析的内容只能成为待确认文本，不能成为执行指令或权威事实。

### 2.2 截图

- 格式：PNG、JPEG、WebP；
- 数量：单次最多 6 张；
- 大小：每张不超过 10MB；
- OCR：本地 PaddleOCR 3.x 中文模型；
- 留存：保存图片 hash、OCR/模型版本、文本框、置信度和处理回执；
- 清理：原图在任务成功或失败终止后删除，不写数据库、应用日志或 Git。

上传格式、数量或大小不符合时，整批拒绝并指出具体文件。原图清理失败属于隐私阻断项，Run 不得标记成功。

### 2.3 TripBriefRevision

每次确认生成版本化 `TripBriefRevision`，至少包括城市、日期、人数、到离信息、酒店或住宿区域、四种交通偏好及限制、预算、餐饮与住宿风格、饮食限制、每日节奏和活动强度。

必填字段未确认时不得进入核验。没有特殊偏好的字段显式保存 `NO_PREFERENCE`。已确认档案可复用，但本次行程设置优先；`INFERRED` 信息不得成为 `HARD`。

### 2.4 早期拒绝

城市不受支持、检测到跨城、人数不在 2～5、行程不在 2～5 天时，在事实采集前返回结构化拒绝原因。

## 3. 核验合同

### 3.1 确定性核验

Audit Engine 对有足够证据的字段判断：地点真实性、城市/区县归属、营业与预约、时间冲突、四种交通路线耗时、酒店往返、用餐窗口、步行限制和天气硬冲突。

高德路线适配器统一支持步行、公交、骑行和驾车。每次采集保存规范化请求 hash、响应 hash、请求/观测时间、交通模式、状态和必要回执。缺少或过期证据时保持 `UNKNOWN/UNAVAILABLE`。

### 3.2 建议性判断

餐厅/酒店偏好、活动强度、月份适配、热门程度和拥挤风险属于建议性判断。输出必须包含依据、适用条件和不确定性，不得使用硬事实语气。

### 3.3 风险搜索

仅对热门、节假日、天气异常或潜在风险地点触发 Brave Web/News：每个行程最多 3 个查询，每个查询最多 5 个来源；官方、政府、交通或运营方优先，正规媒体其次，社交内容只能触发「待确认」。搜索或模型失败不阻断其他核验。

DeepSeek 只把检索结果整理为结构化 `RiskEvidence`，必须包含 URL、标题、发布日期、检索时间、短原文片段和风险类型。它不能补造来源、日期或事实。

## 4. 报告与 Advice 合同

Finding 分为 `MUST_ADJUST`、`SHOULD_OPTIMIZE`、`NEEDS_CONFIRMATION`。每个非 PASS Finding 必须有一个 `AdviceBundle`：至少一条具体行动建议、预期影响和不确定性；能查到真实候选时附冻结 CandidateSet、地点/路线 receipt、路线变化、偏好匹配和 Evidence；无可靠候选时只给具体搜索区域与筛选条件。

用户采纳必须通过现有 Repair/EditCommand 创建新 `ItineraryRevision`。只有针对新 revision 完成全量 postcheck，Finding 才能变为已解决。

## 5. Run 与恢复合同

`TripCheckRun` 绑定 itinerary revision、TripBrief revision、Prompt、模型、Provider、搜索、规则和配置 hash。阶段固定为 OCR、解析、地点消歧、事实采集、Audit、风险补充、Advice 生成和采纳后的 postcheck。

长运行通过数据库状态和 SSE 展示进度，不引入消息队列。断线或重启后从同一配置下最后完成阶段恢复；阶段使用稳定幂等键。Provider 局部失败保留成功事实并标记受影响字段；Brave 不可用不得影响其他核验。

## 6. 接口与持久化边界

V1 需要截图/OCR、TripBriefRevision、TripCheckRun/进度、Advice 查询/应用和 postcheck 资源；具体路径和 schema 在对应 Goal 中经 HITL 后确定。所有写接口使用 revision 前置条件和幂等键，客户端不能提交 canonical POI、Provider 事实或「已解决」状态作为权威值。

- 新 migration 从 `022` 开始，只追加，不重写历史；
- 旧文本导入和现有 revision 保持可读；
- 保留输入、确认版本、事实、Finding、Advice、命令和 postcheck lineage；
- 原图是临时资产，不属于持久化业务记录。

## 7. 性能与失败体验

- 标准文本输入首次进度反馈 ≤1 秒；解析与确认页 P95 ≤3 秒；
- 三张截图 OCR P95 ≤12 秒；基础报告 P95 ≤30 秒；
- 包含风险搜索的完整报告 P95 ≤45 秒。

超时、断线和 Provider 失败必须显示已完成阶段、不可用字段、可重试动作和稳定 Run ID，禁止以空白页或通用「生成失败」结束。

## 8. 外部技术依据

- [PaddleOCR 3.x Quick Start](https://www.paddleocr.ai/main/quick_start.html)
- [高德路径规划 2.0](https://lbs.amap.com/api/webservice/guide/api/newroute)
- [Brave News Search](https://api-dashboard.search.brave.com/app/documentation/news-search/get-started)
