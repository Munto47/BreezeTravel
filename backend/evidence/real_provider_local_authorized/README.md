# Local-authorized Provider evidence

`suggestion_snapshot_2026-08-21.json` 是 Suggestion Provider 的三城实网技术快照：

- 固定 Anchor：北京故宫博物院、上海外滩、杭州西湖风景名胜区；
- 每城分别执行 `NEARBY / POPULAR / FUN / FOOD` 高德 around search；
- 过滤错城、错品类、Anchor 重复和 canonical 重复后，每城冻结 6 个候选；
- 每个候选通过现有 `AmapRouteSource` 串行抓取 `Anchor -> candidate` walking route receipt；
- entity 与 route 都保留脱敏 request hash、原始 Provider response hash、observed_at、Provider、execution mode、端点和 snapshot ID；
- 高德显式返回的 current facts 单独冻结，entity receipt 不会被当作营业、预约或无障碍证据。

采集命令：

```powershell
$env:PYTHONPATH='backend'
python backend/scripts/capture_suggestion_provider_snapshot.py --strict --request-pause-seconds 0.25
```

离线校验（不会再次调用 Provider）：

```powershell
$env:PYTHONPATH='backend'
python -c "import json; from pathlib import Path; from scripts.capture_suggestion_provider_snapshot import validate_artifact; p=Path('backend/evidence/real_provider_local_authorized/suggestion_snapshot_2026-08-21.json'); print(validate_artifact(json.loads(p.read_text(encoding='utf-8'))))"
```

快照只证明 `local_real`、本机授权条件下真实 entity/route 适配器在该次观测可用。它不证明候选已经通过完整约束/排名，不证明营业时间、预约、无障碍，不是 public E2E、人验或发布门禁结果。脚本无 fixture fallback；预检或 Provider 调用失败时仍写脱敏 failure receipt，`--strict` 返回非零。
