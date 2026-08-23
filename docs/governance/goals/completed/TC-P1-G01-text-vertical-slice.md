# COMPLETED GOAL：P1-G01 文本纵向闭环

## Metadata

- Goal ID：`TC-P1-G01-text-vertical-slice`
- Program ID：`TC-V1-INTERVIEW-2026`
- Phase：`P1`
- Status：`COMPLETED`
- Branch：`codex/trip-check-p1-text-vertical-slice`
- Baseline commit：`8cafa26d6b972faa54b1fc10f375eab4a32c6383`
- D1 subject commit：`dd70870a817b84f6364804a5701950c754728f4e`
- Evidence commit：`7c60f27d2168d7f2a5193bbf7f01121c9e5b560a`
- Approved by / at：用户批准完整实施方案 / 2026-08-22
- Completed at：2026-08-23
- Predecessor gate：`TC-P0-G02 PASS`

## Outcome

受控文本行程已经打通以下可恢复纵向闭环：

```text
文本 Import → TripBrief/地点确认 → EvidenceSnapshot → Audit
→ Advice → Repair 预览/采纳 → 新 Revision → 完整 postcheck
```

Run 使用 PostgreSQL 权威状态、lease/CAS、稳定副作用键和持久事件；Evidence 提交后终止并恢复不会重复 Evidence、Provider receipt 或 revision。刷新通过 GET 回读，SSE 通过 `Last-Event-ID` 续传。

## Scope result

- migration `022`～`024` 保持 append-only，未增加 migration；
- 接通 TripBrief、TripCheckRun、RunSpec、AdviceBundle 既有公共合同；
- Evidence、AuditEngine、Repair/EditCommand 继续复用唯一领域主干；
- 北京、上海、杭州受控 CandidateSet/路线 fixture 产生 Evidence 与 receipt；
- 18 条 pilot 使用固定 runner 实际执行并输出逐 Run artifact；
- Import 工作区接通 Brief 确认、地点确认、Run、Advice、Repair 与 postcheck；
- 未实现 OCR、真实/付费 Provider、Builder、Planner、RAG、LoRA、Yjs、公网部署或真人评测。

## Acceptance result

- 三城浏览器文本主链：`PASS`；BJ-02 歧义地点未经确认不能进入权威行程：`PASS`；
- 18 pilot：`18/18 PASS`，城市分布 `6/6/6`：`PASS`；
- 错城/错 POI 自动接受数 `0`：`PASS`；
- Repair 后新增 `BLOCKER/HIGH/UNKNOWN` 数 `0`：`PASS`；
- Evidence 后终止恢复，Run/Evidence/receipt/revision 数量一致且无重复副作用：`PASS`；
- 幂等重放、payload 冲突、并发 revision、缺少/stale `If-Match`、config hash 漂移：`PASS`；
- D1 artifact、逐 Run RunSpec/event/receipt/snapshot/replay/metrics 和 90 秒演示脚本：`PASS`。

## Verification result

以下结果全部绑定 D1 subject commit `dd70870a817b84f6364804a5701950c754728f4e`：

- `python -m pytest tests/ -q`：`1247 passed, 26 skipped, 38 warnings`；
- `python -m ruff check app evals scripts tests`：`PASS`；
- `npm run build`：`PASS`；
- `python backend/scripts/validate_dual_entry_testset.py`：命令 `PASS`，仅作旧资产结构回归；
- PostgreSQL fault matrix：`7 passed`；
- P1 fault contracts：`16 passed`；
- 固定 runner：`18/18 PASS`；
- Playwright：`4/4 PASS`；
- D1 manifest readback、177 项 artifact sha256 和敏感信息扫描：`PASS`、`0` 命中。

## Evidence boundary

- `controlled_fixture`：`PASS`；
- `postgresql_integration`：`PASS`；
- `controlled_browser_fixture`：`PASS`；
- `live_provider`：`NOT_RUN`；
- `public_e2e`：`NOT_RUN`；
- `human_evidence`：`NOT_RUN`。

D1 只证明 P1 受控文本纵向闭环，不替代候选版 G0～G6、真实 Provider、公网或真人证据。

## Completion record

- Commits：`507ab02`、`a509f72`、`e7597ca`、`5626519`、`b9e1d76`、`18d1575`、`6cdb42f`、`acacc94`、`eb65f7a`、`05f40bf`、`3bd93ea`、`cb5d0a8`、`47499fa`、`f79463e`、`16d9f35`、`dd70870`、`7c60f27`，以及包含本完成档案的治理提交；
- Remote branch：`origin/codex/trip-check-p1-text-vertical-slice`，upstream 在归档前已确认；
- Evidence：`backend/evidence/trip_check_v1/p1/d1_manifest.json`、`fault_matrix.json`、`browser-playwright.json`、`pilot/`、`logs/` 与 `DEMO_90_SECONDS.md`；
- Gate result：`D1 PASS`；
- Next Goal generated：`TC-P2-G01-reliable-run-and-trace`，仅为 `DRAFT`，未开始实施；
- Remaining red lights：live Provider、公网 E2E、真人证据及候选版 G0～G6 均未运行；
- Promotion decision：`AUTO_ADVANCE_ELIGIBLE`，仅允许 fast-forward 到 `codex/trip-check-v1-program`，不合并 `main`。
