# G01 Text Card 数据集 v1

这是 `TC-VNEXT-G01-TEXT-CARDS` 的独立 90 条输入合同，不复用已删除的旧根目录旅行文本，也不读取或修改历史 Trip NLU Candidate、manifest、blind、oracle 或证据。

## 当前证据边界

- 90 条高保真合成长文本输入：`54 dev / 18 validation / 18 frozen_blind`；
- 30 个 family，每个 family 只有 A/B/C 三个变体，且不会跨 split；
- `60 DEEP_CITY / 15 OTHER_CITY / 15 ADVERSARIAL`；
- 仓库内 human label、gold 和 oracle 数量为 0；
- dev/validation 需要两名被授权真人独立标注，再由第三名被授权真人裁决冲突；
- frozen-blind truth 只能由独立 custodian 在仓库外保管，唯一候选冻结前不得评分或读取；
- 合成输入、结构校验、fixture 预测和自动测试都不是 human annotation，也不能使 Text Card Gate 通过。

`dataset_contract.json` 固定输入、schema、生成回执和生成器的字节 hash。`frozen_blind.inputs.jsonl` 只有输入与非真值 lineage；仓库内没有 blind label commitment 或占位 truth，避免把尚未完成的 custodian 工作伪装成已经 seal。

## 校验

在 `backend` 目录运行：

```powershell
$env:PYTHONPATH = "."
python -m evals.trip_text_cards_v1.validator
python -m pytest -q tests/test_g01_text_card_dataset.py
```

结构校验成功只表示输入合同完整。只要双人标注、裁决、Provider binding 或 sealed-blind 一次性运行任一缺失，Gate 必须保持 `HITL_PENDING` 或 `NOT_RUN`。

## 真人标注交接

空白工作包必须写到仓库外；工具不会预填模型建议或伪造标签：

```powershell
python -m scripts.prepare_g01_text_card_annotation_packet `
  --split dev `
  --assignment-id <独立任务ID> `
  --output <仓库外绝对路径>
```

同一 split 由两名不同真人分别提交 `annotation-bundle-v1` 后，第三名真人提交绑定两份源文件 SHA-256 的 `adjudication-bundle-v1`。`score_g01_text_card_dev_validation` 会验证三人身份互异、独立声明、case 全覆盖、逐字 span、所有冲突指纹与 Provider receipt，再计算确定性指标。通用开发 scorer 故意不接受 `frozen_blind`；blind 只能在候选完全冻结后由独立 custodian 的一次性流程执行。
