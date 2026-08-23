# COMPLETED GOAL：P2-G01 可靠运行与领域 Trace

## Metadata

- Goal ID：`TC-P2-G01-reliable-run-and-trace`
- Program ID：`TC-V1-INTERVIEW-2026`
- Phase：`P2`
- Status：`COMPLETED`
- Branch：`codex/trip-check-p2-reliable-run-and-trace`
- Baseline commit：`8816975f2abf417d21c3aa7dc977576d64347502`
- Reliability subject commit：`920b165a404219b7f586296a37960920b1d17170`
- Evidence commit：`797a6a1a12d60390b4daae9a1f0ff04b4d61cda7`
- Approved by / at：用户批准 P2 完整实施方案与 OpenTelemetry 依赖 / 2026-08-23
- Completed at：2026-08-23
- Required gate：`Reliability Gate`

## Outcome

P1 的 Run、lease/CAS、幂等、SSE 和 PostgreSQL 恢复能力已收敛为六类固定故障矩阵，并形成两层 Trace：

```text
OpenTelemetry: trip_check.run → trip_check.stage → trip_check.provider_attempt
PostgreSQL: Run/Event/StageAttempt/Receipt/Snapshot → Domain Trace → Replay
```

OpenTelemetry 只承担脱敏诊断；PostgreSQL 领域 Trace 继续作为恢复和验收权威。公共 HTTP API/schema 与 migration `022`～`024` 未改变。

## Acceptance result

- `provider_timeout`：初始请求加最多 2 次重试，共 3 次；耗尽后 `PARTIAL` receipt、`UNAVAILABLE` Evidence 和 `UNKNOWN` Finding：`PASS`；
- `partial_field_failure`：1 次失败且不重试，成功 Brief/路线事实保留，Run 以 `PARTIAL · WAIT_ADOPTION` 收敛：`PASS`；
- `duplicate_submit`：Run 与 Repair 同 payload replay 返回原资源，不同 payload 复用键冲突，资源计数不增长：`PASS`；
- `concurrent_revision`：两个并发采纳仅一个成功，失败方映射 `409` 并回读胜者 revision：`PASS`；
- `terminate_after_evidence`：父进程在 Evidence/receipt 已提交后实际终止 worker，lease 过期后接管；原 snapshot、Evidence receipt 和 revision 不重复：`PASS`；
- `config_drift`：恢复返回 `RUN_CONFIG_MISMATCH`，阶段、事件和副作用计数不变：`PASS`；
- 领域 Trace 必需字段覆盖率 `100%`，Domain/OTel 关联率 `100%`，敏感属性命中 `0`：`PASS`；
- 浏览器刷新恢复、SSE 游标重连、重复/乱序事件去重：Playwright `4/4 PASS`；
- P1 pilot 回归：`18/18 PASS`，三城 `6/6/6`，错 POI 自动接受 `0`：`PASS`。

## Verification result

以下结果全部绑定 Reliability subject commit `920b165a404219b7f586296a37960920b1d17170`：

- `python -m pytest tests/ -q`：`1256 passed, 27 skipped, 38 warnings`；
- `python -m ruff check app evals scripts tests`：`PASS`；
- `npm run build`：`PASS`；
- `npm run test:e2e:trip-check-p2`：`4/4 PASS`；
- `python backend/scripts/validate_dual_entry_testset.py`：`PASS`，仅作旧资产结构回归；
- PostgreSQL integration：`9 passed`；
- P2 Reliability runner：`6/6 PASS`；
- P1 pilot regression：`18/18 PASS`；
- 顶层 manifest readback、230 项 artifact sha256、231 个 Git index blob 字节一致性与敏感扫描：`PASS`、`0` 命中。

首次完整 Gate 曾因 Windows 本地 socket `WinError 10055` 保持 `REJECT`；失败用例单独及后续全量回归均通过。该基础设施失败未被记为代码通过，也未进入最终 PASS artifact。

## Evidence boundary

- `controlled_fixture`：`PASS`；
- `postgresql_integration`：`PASS`；
- `controlled_browser_fixture`：`PASS`；
- `live_provider`：`NOT_RUN`；
- `public_e2e`：`NOT_RUN`；
- `human_evidence`：`NOT_RUN`；
- 候选版 G0～G6：`NOT_RUN`。

Reliability Gate 只证明 P2 受控可靠运行与 Trace，不替代 OCR/Provider 完整性、候选 commit G0～G6、公网或真人证据。

## Completion record

- Checkpoints：`627d477`、`067b2f1`、`1c1eea5`、`584eeee`、`869fe53`、`6a692f7`、`9a1f375`、`2bc868c`、`920b165`、`797a6a1`，以及包含本完成档案的治理提交；
- Remote branch：`origin/codex/trip-check-p2-reliable-run-and-trace`，evidence checkpoint upstream 已确认；
- Evidence：`backend/evidence/trip_check_v1/p2/reliability_gate_manifest.json`、`reliability/`、`pilot_regression/`、`browser-playwright.json` 与 `logs/`；
- Gate result：`Reliability Gate PASS`；
- Next Goal generated：`TC-P3-G01-input-provider-integrity`，仅为 `DRAFT`，未批准或实施；
- Remaining red lights：OCR 隐私闭环、真实 Provider、四种交通/天气/风险来源、候选版 G0～G6、公网和真人证据均未运行；
- Promotion decision：`AUTO_ADVANCE_ELIGIBLE`，仅允许 fast-forward 到 `codex/trip-check-v1-program`，不合并 `main`。
