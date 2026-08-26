# COMPLETED GOAL：P3-G01 输入与 Provider 完整性

## Metadata

- Goal ID：`TC-P3-G01-input-provider-integrity`
- Program ID：`TC-V1-INTERVIEW-2026`
- Phase：`P3`
- Status：`COMPLETED`
- Branch：`codex/trip-check-p3-input-provider-integrity`
- Baseline commit：`cd533cf81034cfdd11e9a3f3ea15d953202bb1a5`
- Gate subject commit：`e13053ecce6d2e5aa6d2d5ecd57184a6a2d200aa`
- Evidence commit：`acb1e990d01f2e8c68688a841b7023028ab90427`
- Completed at：2026-08-23
- Required gate：`P3_SYNTHETIC_OCR_PHASE_GATE + G2 + G3`

## Outcome

截图 OCR 隐私闭环、四种交通、天气预报与实时天气预警均已进入固定主链；受控 fixture、synthetic OCR、PostgreSQL、snapshot 和 live Provider 被明确拆成不同证明等级。Brave News 因标准计划的数据留存条款与持久化 EvidenceSnapshot 不兼容，退出 P3 必需 Provider；第六次固定调用改为既有和风天气预警。

```text
截图/文本 → OCR/Parser → TripBrief 确认 → Provider Adapters
→ EvidenceSnapshot/Receipt → Audit/Advice → Repair → 完整 postcheck
```

## Acceptance result

- synthetic OCR v2：12 例、三城各 4 例，PaddleOCR `3.7.0` 文本 micro-F1 `0.999149`，关键字段精确召回 `1.0`，低置信确认召回 `1.0`：`PASS`；
- deterministic render integrity：解码、格式、尺寸、非空、重复渲染 hash 和 12 个唯一图片 hash：`PASS`；人工视觉布尔值已移除；
- 原始合成图片终态目录删除，Git/证据泄漏命中 `0`：`PASS`；
- G2 PostgreSQL：migration、截图持久化、事务、恢复和 P2 六类 reliability 回归：`PASS`；Gate 自动拉起并停止隔离 PostgreSQL；
- G3 snapshot：6/6，网络调用 `0`，hash 可回读：`PASS`；
- P1 pilot：18/18，三城 6/6/6：`PASS`；
- 受控浏览器截图流：2/2：`PASS`；
- 高德四种路线、和风天气预报/预警 adapter 的字段级失败、局部成功、零预警非全局安全语义：`PASS`；
- sensitive scan：`PASS`，0 命中；扫描器已排除 `risk-provider` 等 slug 对 `sk-` 模式的误报，同时保留真实 key 形状检测。

## Verification result

以下结果全部绑定 Gate subject `e13053ecce6d2e5aa6d2d5ecd57184a6a2d200aa`：

- `python -m pytest tests/ -q`：`1300 passed, 28 skipped, 38 warnings`；
- `python -m ruff check app evals scripts tests`：`PASS`；
- `npm run build`：`PASS`；
- `npm run test:e2e:trip-check-p3`：`2/2 PASS`；
- `python backend/scripts/validate_dual_entry_testset.py`：`PASS`；
- PostgreSQL integration：`7 passed`；
- P2 Reliability regression：`6/6 PASS`；
- P3 phase manifest：`PASS`，271 项 artifact index 可回读。

首次重跑因扫描器把 ADR slug 中的 `risk-provider` 误判为 OpenAI key 而保持 `REJECT`；修复后重新完整运行，不复用旧测试结果，最终 sensitive scan 为 0 命中。

## Evidence boundary

- `synthetic_ocr_stress`：`PASS`；
- `postgresql_integration`：`PASS`；
- `provider_snapshot`：`PASS`；
- `controlled_browser_fixture`：`PASS`；
- `G1_REAL_OCR_CANDIDATE_GATE`：`NOT_RUN`；
- `G4_LIVE_PROVIDER`：`NOT_RUN`；
- `G5_PUBLIC_BROWSER_PERFORMANCE`：`NOT_RUN`；
- `G6_RELEASE_MANIFEST`：`NOT_RUN`；
- candidate readiness：`REJECT`；public E2E 与 human evidence：`NOT_RUN`。

P3 phase PASS 只证明开发阶段输入/Provider 完整性，不表示候选版、公开或真人证据已经通过。零天气预警只表示本次查询未返回活动预警，不表示没有其他旅行风险。

## Completion record

- Checkpoints：`8a430a2`、`6d108e5`、`a5ab396`、`e13053e`、`acb1e99`，以及包含本完成档案的治理提交；
- Remote branch：`origin/codex/trip-check-p3-input-provider-integrity`，evidence checkpoint upstream 已确认；
- Program integration：`origin/codex/trip-check-v1-program` 已 fast-forward 到 `acb1e99`；
- Evidence：`backend/evidence/trip_check_v1/p3/integrity_gate_manifest.json` 及其 artifact index；
- Gate result：`P3 phase PASS`；candidate readiness `REJECT`；
- Remaining red lights：G1 真实 OCR、G4 live Provider、G5 公网浏览器/性能、G6 release manifest、真人证据；
- Promotion decision：`AUTO_ADVANCED_TO_P4`，不合并 `main`、不部署、不进入 H1。
