# BreezeTravel 证据报告

此报告的作用是让简历数字可回读，而不是把历史离线结果包装成生产指标。

## 当前基线状态

`backend/evidence/latest.json` 是唯一可公开读取的摘要，并明确标记为 `historical_baseline_pending_rerun`：

- Router 离线固定集：50 条，准确率 0.88；`both` 类 0.60，是当前最重要的改进对象。
- RAG 历史评测：27 条，Faithfulness 0.9389、Answer Relevancy 0.9889、Context Recall 0.6944；其语料为历史合成资料，不能作为公开真实资料的效果宣称。
- 排线压测：50 次，`/api/optimize` P95 为 2221 ms。该数字的依赖配置见原始 JSON，不能外推到公网。

## 重新发布真实资料指标的门槛

1. 导入已审核的公开资料，记录 URL、许可、获取时间与数据版本。
2. 固定 train/validation/blind-test 划分；盲测题不得参与调参。
3. 分别运行 Router、RAG 和负载评测命令，保留原始 JSON、代码提交 SHA、模型配置和时间。
4. 更新 `backend/evidence/latest.json` 的状态、数据说明和指标后，才可在 README 或简历使用新数值。

当前 `/api/evidence/latest` 仅返回这个脱敏摘要；它不会暴露原始 prompt、密钥、用户请求或 LangSmith 私有 trace。
