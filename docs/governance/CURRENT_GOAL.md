# CURRENT GOAL：P1-G01 文本纵向闭环

## Metadata

- Goal ID：`TC-P1-G01-text-vertical-slice`
- Program ID：`TC-V1-INTERVIEW-2026`
- Phase：`P1`
- Status：`IN_PROGRESS`
- Branch：`codex/trip-check-p1-text-vertical-slice`
- Baseline commit：`8cafa26d6b972faa54b1fc10f375eab4a32c6383`
- Approved by / at：用户批准完整实施方案 / 2026-08-22
- Predecessor gate：`TC-P0-G02 PASS`

## Outcome

用受控 fixture 交付第一条真实可操作的文本纵向闭环，并证明在 Evidence 阶段终止进程后可以恢复且不重复 Provider 副作用或 revision：

```text
文本 Import → TripBrief 确认 → 歧义 POI 确认
→ EvidenceSnapshot → 事实/路线冲突 → Advice
→ Repair 预览/采纳 → 新 Revision → 完整 postcheck
```

## Scope

- 按 API 合同实现 `TripBriefRevision`、字段来源、确认状态和必要的 `TripCheckRun/RunSpec/AdviceBundle` 最小纵向字段；
- 追加且只追加 migration `022`～`024`，不修改历史 migration；
- 保留现有文本 Import，并接通 Brief 读取、条件更新、确认、Run 创建/读取/恢复、SSE 和 Advice 读取；
- 所有写操作实现稳定 `Idempotency-Key`，revision 更新实现 `If-Match`、`409` 与 `428`；
- 复用现有 EvidenceSnapshot、AuditEngine、Repair/EditCommand，不创建第二套编辑协议；
- 支持一例歧义 POI、一个确定性事实或路线冲突、Advice、采纳后新 revision 和完整 postcheck；
- 建立北京、上海、杭州各 6 条，共 18 条可执行 pilot；缺陷修复同步进入 regression；
- 浏览器覆盖三城各至少一条文本主链；加入 Evidence 后进程终止/恢复测试。

## Non-goals

- OCR 与截图 Import 只允许隔离技术验证，不进入本 Goal 主链；
- 不接真实高德、天气、Brave 或付费模型；Provider 只用受控 fixture；
- 不引入 OpenTelemetry、OR-Tools、Redis 权威状态、新 Agent 或新基础设施；
- 不扩城、不跨城，不主动修改 Builder、Planner、RAG、LoRA 或 Yjs；
- 不晋级 live/public/user evidence，不部署公网或招募真人。

## Authority

`AGENTS.md`、`../product/PROJECT_CHARTER.md`、`../product/TRIP_CHECK_SPEC.md`、`../product/TRIP_CHECK_API_CONTRACT.md`、`PROGRAM.md`、`RELEASE_GATES.md`、ADR-005 与本 Goal。

## API and schema contract

- API version：`trip-check-api-v1`；
- schema additions：`022_trip_brief_revisions.sql`、`023_trip_check_runs.sql`、`024_advice_bundles.sql`；
- compatibility：现有 Import、revision、evidence、repair 与 suggestion 读取能力保持兼容；
- stable errors：`PRECONDITION_REQUIRED`、`REVISION_CONFLICT`、`IDEMPOTENCY_CONFLICT`、`RUN_CONFIG_MISMATCH`；
- dataset increment：`18 pilot = 北京 6 + 上海 6 + 杭州 6`；
- fault profile：`terminate_after_evidence`、`duplicate_submit`、`concurrent_revision`；
- evidence path：`backend/evidence/trip_check_v1/p1/`。

## Invariants

- PostgreSQL/持久仓储是权威状态；进程内状态和 SSE 连接不是事实来源；
- Checkpoint 只表示阶段可恢复，不替代副作用幂等键、事务和 receipt；
- `UNKNOWN/UNAVAILABLE` 不得转成 PASS；Provider 局部失败保留成功事实；
- 任何有语义的采纳创建新 revision，旧报告 stale，完整 postcheck 后才能显示解决；
- 候选地点来自冻结 CandidateSet 并绑定 receipt；模型文本不得变成已验证事实；
- P1 不处理原始截图，日志和测试 artifact 不得包含密钥或未脱敏真实用户文本。

## Acceptance cases

- 北京、上海、杭州各一条浏览器文本主链完整通过；
- 18 条 pilot 全部可执行，城市分布严格为 6/6/6；
- 错误 POI 自动接受数为 0；
- Repair 后新增 `HIGH/UNKNOWN` 数为 0；
- Evidence 后终止再恢复，Run、Evidence、Provider receipt 和 revision 数量一致且无重复副作用；
- 重复写请求返回同一业务结果；不同 payload 复用同一幂等键稳定失败；
- stale `If-Match` 返回 409，缺少前置条件返回 428；
- P0 离线回归、Ruff 和前端 build 不退化；
- 形成绑定 commit/config/dataset 的 D1 artifact 和 90 秒演示脚本。

## Verification

- 新模型、仓储、API、Audit/Repair/postcheck 的定向单元与集成测试；
- migration append-only/顺序检查和 PostgreSQL 持久恢复测试；
- 18 pilot schema、分布、oracle 与执行结果验证；
- 三城浏览器 E2E；
- `terminate_after_evidence`、重复提交、并发编辑故障测试；
- `python -m pytest tests/ -q`；
- `python -m ruff check app evals scripts tests`；
- `npm run build`；
- `python backend/scripts/validate_dual_entry_testset.py` 继续只作旧资产结构回归，不作为 D1 放行。

## Budget

- 外部 Provider/模型成本：0；
- 仅使用受控 fixture 和本地 PostgreSQL；
- 新增依赖：0；
- 每个可验证功能切片立即 commit/push，最长 60 分钟形成远端 checkpoint。

## Pre-approved actions

- 实现 migration `022`～`024` 和固定 API/schema；
- 在本分支运行离线、fixture、PostgreSQL 和本地浏览器测试；
- D1 通过后 fast-forward 到 `codex/trip-check-v1-program` 并生成 P2 Goal。

## HITL

本 Goal 无额外 HITL。真实/付费 Provider、公网部署、真人招募、降低 Gate、修改 blind/oracle、合并 `main` 仍需现场批准。

## Stop conditions

- 需要修改历史 migration、公共合同或 Program 固定范围；
- 连续两个实现切片不能改善 D1 同一门禁；
- 需要新增未批准依赖/基础设施、修改 blind/oracle 或使用真实付费 Provider；
- Evidence 矛盾、隐私事故、成本超预算，或必须降低 Gate 才能通过。

## Auto-advance

D1 全部 PASS、工作树干净、commit 已推送、evidence 可回读且无 Stop condition 后，归档本 Goal 并生成 P2；不得自动合并 `main`。

## Completion record

- Commits：待填写；
- Remote branch：待填写；
- Verification results：待填写；
- Evidence paths：待填写；
- Next Goal：`TC-P2-G01-reliable-run-and-trace`；
- Promotion decision：`PENDING`。
