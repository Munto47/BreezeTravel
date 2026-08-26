# 三城真实游客需求评测集

`cases.json` 固定包含 150 条端到端测试数据：北京、上海、杭州各 50 条。

覆盖分布：

- 景点 45 条、餐饮 39 条、住宿 30 条、景点+餐饮混合 27 条、景/餐/住综合 9 条。
- 游客画像包括独行、情侣、亲子、老人、轮椅使用者、学生、商务、转机和深夜到达旅客。
- 约束包括行政区、预算、饮食与过敏、少步行、无障碍、营业时间、公共交通、天气、显式地标顺序和负向排除项。

每条数据含 `persona`、`dimensions`、自然语言 `query` 和可执行的 `expected`。运行器启动时会强制校验三城是否各 50 条、ID 是否唯一、验收语义是否完整。

真实链路运行：

```powershell
cd backend
$env:PYTHONPATH='.'
python -m scripts.run_daily_query_eval --workers 4 --output results/three_city_daily_query_eval_latest.json
```

中途遇到外部配额或网络错误后，可保留检查点；正式基线必须从空文件重新完整运行，不能把不同模型链路的结果拼成同一批次。

调试阶段若模型链路未变化，可只补跑未完成部分：

```powershell
python -m scripts.run_daily_query_eval --workers 4 `
  --resume-from results/three_city_daily_query_eval_latest.json `
  --output results/three_city_daily_query_eval_latest.json
```

该命令会调用真实 `/api/chat` SSE、高德 POI、系统使用的生成链路，并强制裁判复用同一 provider/model，不再跨 provider 回退。确定性规则负责品类、城市、行政区、指定地点、顺序和排除项；模型裁判负责约束遵循、画像适配、实用性和事实支撑。正式输出会记录 `model_chain`、裁判 provider/model 分布，以及确定性失败与 LLM 语义失败的独立/交叉统计。

## 冻结候选与三轮复现

先从一轮无工具失败的 live 报告冻结 provider 候选、检索审计和已解析的字段级证据：

```powershell
python -m scripts.run_daily_query_eval --skip-judge --workers 5 `
  --output results/live.json `
  --snapshot-output results/candidates.json
```

快照完整性门禁要求 150 条都有 live Amap 快照、没有失败 receipt，也没有 fixture/fallback。重放不会再访问 Amap、实时路线或生成模型，可重复执行三轮：

```powershell
1..3 | ForEach-Object {
  python -m scripts.run_daily_query_eval `
    --replay-snapshot results/candidates.json --skip-judge --workers 5 `
    --output "results/snapshot_round_$_.json"
}
```

DeepSeek 可用时，对三份报告分别运行 Judge。`judge-existing` 会记录源报告哈希、执行树哈希和裁判模型链，Judge error 不计为通过：

```powershell
1..3 | ForEach-Object {
  python -m scripts.run_daily_query_eval `
    --judge-existing "results/snapshot_round_$_.json" --workers 5 `
    --output "results/snapshot_judged_round_$_.json"
}
```

## 人工校准

从冻结报告分层抽取 40 条盲标样本；城市数量差不超过 1，五类意图各 8 条，并优先纳入高风险约束：

```powershell
python -m scripts.calibrate_daily_query_judge prepare `
  --report results/snapshot_round_1.json `
  --output results/human_labels_40.json `
  --sample-size 40
```

人工只填写 `human_label=pass|fail` 和可选 `human_notes`，不能查看 Judge 输出。标签完整且 Judge 无 error 后计算一致率；少于 30 条、缺标签或 Judge error 都会 fail-closed：

```powershell
python -m scripts.calibrate_daily_query_judge score `
  --labels results/human_labels_40.json `
  --judge-report results/snapshot_judged_round_1.json `
  --output results/judge_human_agreement.json
```
