# 行程文本内容识别答案合同（尚未生成）

当前目录只保存合同说明，不包含结构化答案 JSON、`manifest.json` 或独立审查结果，状态为 `NOT_RUN`。后续任务获批并完成识别时，每个 `.txt` 应对应一个同名 `.json`；`manifest.json` 保存路径、输入哈希和审查状态。

## 证据边界

- 未来答案必须由开发子 Agent 根据匿名正文生成，并由未参与首次识别的子 Agent 独立复核。
- 识别阶段不使用源文件名判断地点、天数或人数；文件名只在主 Agent 写回时用于一一对应。
- 这些答案如生成，只能标记为 `automated_agent`，不是真人 ground truth、正式 oracle、frozen blind、H1 或发布证据。
- 本任务不核验景点事实、营业时间、价格、交通可行性或外部 Provider 信息。
- 正文没有明确答案时保留范围、推断或 `unknown`，不为了产品的 2～5 人、2～5 天边界改写原意。

## 文件合同

每份答案的 `schema_version` 固定为 `itinerary-content-answer-v1`，主要字段如下：

- `source_file`、`source_sha256`：重命名后的源文件及其 SHA-256。
- `recognition_basis`：固定为 `content_only_anonymous_case`。
- `destination`、`trip_days`、`traveler_count`：值或范围、识别方式、置信度与 1-based 原文行号证据。
- `days[].branches[]`：逐日主方案、备选、条件分支和整日替换。
- `events[]`：事件、地点、原文时间粒度、顺序、关系、内部子事件与行号证据。
- `unassigned_events`：正文没有 Day 结构时，只保存可识别的候选事件，不人为编排日期。
- `excluded_mentions`：住宿/交通建议、必吃清单、图片/参考链接、票务或营业事实等非行程事件。
- `ambiguities`：正文冲突、未指明地点、范围或无法唯一判断的逻辑。
- `review`：首次识别与独立审查的来源、状态、问题和修正摘要。

## 时间规则

- `exact`：正文给出具体时刻或时间区间，例如 `14:00`、`18:30`。
- `period`：正文只给出上午、中午、下午、傍晚、晚上等时段。
- `relative`：正文只给出随后、返回后、最后等相对时间。
- `duration`：正文只给出 `3～4 小时` 等时长。
- `all_day`：正文明确写全天或一整天。
- `unknown`：正文未说明时间。

不会把“上午”补成具体钟点，也不会把“提前 7 天 20:00 放票”“19:00～22:00 亮灯”等参考事实写成旅客事件时间。

## 关系规则

- 事件数组中的 `order` 表示同一分支内的正文顺序。
- `relations` 只记录正文可证明的先后、互斥、条件、插入或替换关系。
- `二选一`、`A/B`、`备选`、`换成` 保持独立分支，不与主路线扁平合并。
- 景区内部参观顺序优先放在事件的 `subevents` 中。
- `/` 和 `+` 按具体上下文判断；无法唯一判断时进入 `ambiguities`。

## 审查状态

- `PASS`：独立审查未发现内容级错误。
- `PASS_WITH_AMBIGUITIES`：识别准确，但正文自身仍存在不能消解的歧义或冲突。
- 最终交付不允许保留 `NEEDS_CORRECTION`。
