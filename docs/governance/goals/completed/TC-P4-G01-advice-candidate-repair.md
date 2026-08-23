# COMPLETED GOAL：P4-G01 Advice、CandidateSet 与安全 Repair

## Metadata

- Goal ID：`TC-P4-G01-advice-candidate-repair`
- Program ID：`TC-V1-INTERVIEW-2026`
- Phase：`P4`
- Status：`COMPLETED`
- Branch：`codex/trip-check-p4-advice-repair`
- Baseline commit：`3ea92a4dcf58d029ddd06d115fe682ed6b986524`
- Gate subject commit：`85368777ca8d2d4e77cf053fc9a74018f9f9fc9a`
- Completed at：2026-08-23
- Required gate：`Solver Admission Gate`

## Outcome

每个非 PASS Finding 现在都能生成有依据且不越过证据边界的 Advice；具体地点建议必须来自冻结 CandidateSet，并同时绑定地点与路线 receipt。Repair preview 保持零写入，apply 在幂等、CAS 和 base revision 约束下只创建一个新 revision；完整 postcheck 不安全时在 revision 写入前拒绝采纳。

```text
AuditFinding → AdviceBundle → 冻结 CandidateSet
→ Repair preview → apply/CAS/idempotency
→ 新 ItineraryRevision → 完整 postcheck → 新报告与 lineage
```

## Acceptance result

- Advice/CandidateSet：每个 `MUST_ADJUST / SHOULD_OPTIMIZE / NEEDS_CONFIRMATION` Finding 有确定性 Advice；越界或 receipt 不完整的具体地点返回 `UNVERIFIED_CANDIDATE_REJECTED`；
- Repair 事务：preview 零写入；apply 校验 `If-Match`、`Idempotency-Key` 和 base revision；重复请求 replay，冲突/stale 请求拒绝；
- postcheck：未解决目标 Finding 或新增 `BLOCKER/HIGH/UNKNOWN` 时返回 `REPAIR_POSTCHECK_UNSAFE`，不会留下孤立 revision；
- 数据工厂：18 pilot、180 dev、72 regression、0 frozen blind；三城配额、故障分布、来源隔离、重复、隐私和冻结 hash 全部通过；
- Solver bakeoff：36 条冻结案例使用相同 RunSpec、oracle、seed、成本和 2 秒硬截止；
- Solver decision：BoundedRepair 主策略成功率 `66.7%`，CP-SAT `50.0%`，额外解决类型 `0`；CP-SAT 准入 `REJECT`，默认运行时保持 `bounded_repair_v1`；
- CP-SAT 作为本地实验在隔离子进程运行；`UNSAT/TIMEOUT/ERROR` 明确分类并回退，未污染主后端进程。

## Verification result

以下结果全部绑定 Gate subject `85368777ca8d2d4e77cf053fc9a74018f9f9fc9a`：

- `python -m pytest tests/ -q`：`1313 passed, 28 skipped, 38 warnings`；
- `python -m ruff check app evals scripts tests`：`PASS`；
- `npm run build`：`PASS`；
- 浏览器 repair 闭环：北京、上海、杭州及歧义 fail-closed，`4/4 PASS`；
- PostgreSQL migration/repair 并发/恢复：`PASS`，Gate 自动拉起并停止隔离 PostgreSQL；
- P1 pilot：`18/18 PASS`；P2 reliability 与 P3 snapshot regression：`PASS`；
- 36 条三策略 bakeoff、25-stop 性能和 replay：`PASS`；
- P4 phase manifest：`PASS`，274 项 artifact index 可回读，敏感信息扫描 0 命中。

## Evidence boundary

- controlled fixture、PostgreSQL integration、controlled browser fixture：`PASS`；
- live Provider、public E2E、human evidence、frozen blind：`NOT_RUN`；
- G1/G4/G5/G6 候选证据债未晋级；
- candidate readiness：`REJECT`。

P4 PASS 只证明本地候选修复闭环成立，不表示候选版、公开或真人证据已经通过。

## Completion record

- Checkpoints：`2e37088`、`26c2a6c`、`08d8cc7`、`142fa30`、`a8b6701`、`bf9d3ab`、`8536877`，以及包含 Gate evidence 的最终 checkpoint；
- Remote branch：`origin/codex/trip-check-p4-advice-repair`；
- Evidence：`backend/evidence/trip_check_v1/p4/p4_gate_manifest.json` 及其 artifact index；
- Gate result：P4 phase `PASS`；candidate readiness `REJECT`；
- Promotion decision：`P5_NOT_AUTHORIZED`，未生成/激活 P5，未合并 `main`、release 或 deploy。
