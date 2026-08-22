# P8 最终真人校准协议

本协议只用于完整本地功能交付后的 P8 最终发布门禁，不允许 Agent、LLM Judge、合成标签或历史推荐评测代填真人事实。开发阶段的 5.6-sol `synthetic_proxy` 校准另见 ADR-003，二者不得混合计数。

## 样本与归属

- 北京、上海、杭州，2～5 人、2～5 天；
- 至少 30 份真实 AI/手工行程，来自 15～20 名真实旅行组织者；
- 原文、受控变体按 `source_document_id` 分组，不能跨 train/dev/blind；
- 每份原文先记录 consent，再由真人填写 `case.schema.json`；
- 每个组织者使用研究范围内的 HMAC-SHA256 假名标识；聚合器从 case 反算唯一组织者和唯一 `source_document_id`，不信任 manifest 手填计数；
- 系统运行结果和组织者的 Repair 采纳/拒绝写入 `prediction.schema.json`；
- `manifest.json` 只在真实材料与标签已存在时增加计数和 case 路径。
- 聚合器使用代码内固定下限：至少 30 份唯一真实 source document、15～20 名唯一真实组织者；manifest 只能陈述相同合同，不能下调或放宽门禁。
- manifest、case、prediction 在聚合前必须通过仓库内 Draft 2020-12 JSON Schema；城市、天数、人数、非负计数、边界字段或结构不合法时 fail closed。
- 真实原始错误与受控人工植入错误按 `is_original_error` 分组报告，不得只发布混合 recall。
- `critical_human_check` 是独立于 finding `human_verdict` 的复核记录；未执行时必须写 `UNAVAILABLE` 和原因，M1 对应门禁失败，不能用 precision 代填。

## 运行方式

```powershell
cd backend
python -m scripts.evaluate_auditor_human
```

空 manifest 必须输出 `BLOCKED_HUMAN_DATA`。脚本只做确定性聚合，不调用任何模型或 Provider。输出至少包含：

- 日期、时间、地点、固定承诺字段 F1；
- 高置信度 POI 自动匹配 precision、固定承诺 recall、静默错配数；
- BLOCKER/HIGH precision 与 recall；
- 关键 finding 人工核对准确率与 Evidence 回读率；
- 审计耗时 P80；
- Repair 采纳率和拒绝原因分布。

Repair 采纳率 40% 是产品目标，不作为篡改审计事实的技术通过条件。未满足真人样本数、任一事实门槛未通过、或 evidence 无法回读时，P8 真人验收不通过，保持本地 candidate，不能进行公网发布或真人产品表述。
