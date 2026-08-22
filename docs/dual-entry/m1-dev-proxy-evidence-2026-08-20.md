# M1-dev 5.6-sol 代理校准证据包

本证据包记录本地开发门禁，不是真人验收、真实用户测试或公网质量证明。

## 边界

- 数据集：北京、上海、杭州各 50 条，共 150 条；`synthetic_proxy`。
- 三个相互盲隔离的 GPT-5.6-sol 代理评审角色。
- 产物禁止包含 `human_label`、真实组织者、consent 或真人采纳率。
- 通过只允许推进本地 P5～P8 开发；不能满足 P8 真人验收或发布门禁。

## 可复核产物

| 产物 | SHA-256 |
|---|---|
| `backend/results/auditor_simulated/proxy_role_1.json` | `be6857a1636f17bc6d2312f14f253752088d0cbaa2196a70fa6b6d8ad93570f4` |
| `backend/results/auditor_simulated/proxy_role_2.json` | `7bb042c64c262f960d16940e1d80b1ec6e467e7f41a285c766d5bf057b392764` |
| `backend/results/auditor_simulated/proxy_role_3.json` | `ed6aaee250b2fcc6e5ae65a6239651aeaecb82d7ceba731d8db8fa6ae41d9c85` |
| `backend/results/auditor_simulated/m1_dev_proxy_gate.json` | `f7010ab66039ede1e0b323663ad0ed104183f2bd4196c3772225c6aba7b739db` |

在 `backend/` 中复验：

```powershell
python -m scripts.run_m1_dev_proxy_gate `
  --artifact results/auditor_simulated/proxy_role_1.json `
  --artifact results/auditor_simulated/proxy_role_2.json `
  --artifact results/auditor_simulated/proxy_role_3.json
```

当前结果为 `M1_DEV_PROXY_PASSED`；报告仍固定声明
`human_validated=false`、`public_claim_eligible=false` 与
`release_eligible=false`。

## 交付后真人门禁

P8 的 30 份真实原始行程、15～20 名真实组织者、consent、真人 finding 和真人 Repair 行为，继续由
[`m1-human-calibration-protocol.md`](m1-human-calibration-protocol.md) 单独收集和聚合。不得以本包替代或混计。
