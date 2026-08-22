# 合成行程与模拟排雷诊断记录（2026-08-20）

本记录是开发期的离线合成诊断，不是真人事实标注、真实用户验证、M1 质量门禁或公网声明。所有输入与模拟选择均由 `gpt-5.6-sol` 子 Agent 生成或复核，固定标记为 `synthetic_subagent_simulation_not_human`。

## 数据与执行合同

- 范围：北京、上海、杭州各 50 条，共 150 条；2～5 人、2～5 天。
- 结构：60 条模拟 AI 原始行程、60 条同源受控变异、30 条边界/失败样本。
- 受控变异：时间重叠、重复地点、时间缺失/无效、地点无法解析；变异直接改写 parser 可读取的 stop/time，而不是追加说明文字。
- 分组：90 个 `source_document_id`，train/validation/test 按源文档隔离，配对样本不跨 split。
- 真人隔离：`human_labels=false`、`m1_eligible=false`；runner 遇到 `human_label`、`human_findings`、`consent_recorded` 或真实人类标记时 fail closed。
- 输入绑定：runner 校验 `case_count`、唯一 `case_id`、逐条证据边界及 `cases.jsonl` SHA256，并在结果中绑定 manifest、cases 与 runner 代码哈希。

当前绑定：

```text
dataset schema: auditor-simulated-v2.1
runner version: auditor-simulation-runner-v3
cases sha256: 3a03bf19493469fd3e24e1a6cff4f52377a50b1072e755c1bf3571c6dbfd8f74
manifest sha256: 4aac43b5248b6222d5e409151c2b12ec63d1c4c6696fba9369350225d554b7b9
runner code sha256: 7e9e4ecd90155cc4571830380c28ea113187b0ac40286377a7a205133caf7f3a
pipeline code sha256: 447eed28ae241c877826ce60dc77eaa936234c00cc72a2f502beb7ce5197842e
```

`pipeline code sha256` 覆盖 `app/importing`、`app/audit`、`app/constraints`、`app/operations`、`app/repairs`、`app/itineraries` 下的 Python 文件以及显式依赖 `app/schemas/task_spec.py`，当前共 78 个文件。结果中的 `generated_at` 是本次运行的真实生成时间（带时区）；确定性 Evidence/Audit 时钟另存为 `deterministic_reference_time=2026-08-20T16:00:00+08:00`，两者不再混用。

## 最终离线诊断

- 60/60 组原始行程与受控变异成功配对；60/60 个注入类别在分阶段变异差分中出现。
- 30 个原始错误全部被对应阶段捕获：Parser 12/12、Resolution 12/12、Audit 6/6。
- 30/30 个边界样本进入解析错误、实体待确认或 Audit `UNKNOWN`；12 个 parse failure 不再伪造后续 Audit。
- 已标注诊断从 detected 集合中扣除后，`additional_unlabelled_diagnostics` 为 0；138 条行程仍保留营业时间、天气或时间证据不足形成的合理 `UNKNOWN`。
- 30 个 `VIOLATED + repairable + BLOCKER/HIGH` 案例全部进入 Repair 搜索。
- 30/30 个可修复案例得到 proposal，共 45 个不可变 preview；case coverage 为 1.0。方案数量不是质量分数。
- 模拟选择为接受 30、拒绝 30、跳过 90；模拟接受率 0.5 只用于检查决策记录和拒绝原因分布，`eligible_for_m1_human_repair_adoption=false`。
- 报告强制 `diagnostic_only=true`、`quality_gate=false`、`public_claim_eligible=false`。

## 仿真驱动的实际修复

1. 第一版变异只追加自然语言尾注，parser 无法感知。数据改为直接改写可解析 stop/time，并改用同源配对差分。
2. 基线行程跨日重复景点，污染重复地点诊断。每城扩展到 10 个唯一景点，并增加真实 parser 唯一性断言。
3. runner 曾把缺酒店、餐饮、天气等未标注诊断称为误报。基线现在每天含午餐、晚餐和酒店；只有显式 absent 才计 `explicit_false_positives`，环境证据不足单列为 `honest_unknown`。
4. runner 曾只信任 manifest 声明。现在校验 cases 内容哈希、数量、逐案例边界，并拒绝真人字段混入。
5. Repair apply/reject 存在 PostgreSQL 竞态。两条路径现在对 repair 行加锁，终态更新限定 `PROPOSED` 并校验影响行数；内存仓储使用同一决策锁。
6. `DUPLICATE_PLACE` 原来只有发现、没有 Repair。现在可预览删除较早或较晚重复项，锁定和固定预约永不删除。
7. postcheck 曾因 stop ID 集合缩小，把同一地点已有 `UNKNOWN` 错算成新增风险。风险身份已改为规则、原因、日期和地点等稳定语义字段。
8. `待定` 等通用占位词曾被 fixture provider 原样回显后自动匹配。Resolver 现在在调用 Provider 前 fail closed，返回待确认而不是伪造地点事实。
9. Parser 错误曾被错误地按 Audit finding 计算召回。runner v3 分别记录 Parser、Resolution 和 Audit 阶段，并且 parse failure 不再继续构造 Audit。

## 运行方式

从仓库根目录执行：

```powershell
python backend/scripts/generate_auditor_simulated.py
python backend/scripts/run_auditor_simulation.py
```

输入位于 `backend/eval_data/auditor_simulated/`，结果位于 `backend/results/auditor_simulated/latest.json`。真人 M1 仍只读取 `backend/eval_data/auditor/manifest.json`，当前保持 0/30。
