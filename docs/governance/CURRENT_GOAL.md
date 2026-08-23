# CURRENT GOAL：P2-G01 可靠运行与领域 Trace

## Metadata

- Goal ID：`TC-P2-G01-reliable-run-and-trace`
- Program ID：`TC-V1-INTERVIEW-2026`
- Phase：`P2`
- Status：`DRAFT`
- Planned branch：`codex/trip-check-p2-reliable-run-and-trace`
- Baseline commit：待 Program fast-forward 完成后绑定
- Approved by / at：`NOT_APPROVED`
- Predecessor gate：`TC-P1-G01 D1 PASS`

## Outcome

在不扩大产品范围的前提下，让受控「行程查」Run 对六类固定故障产生可恢复、可回读、可重放的机器可读结果，并以领域 Trace 解释每个阶段、权威 revision、Evidence、receipt 和失败分类。

```text
RunSpec + Fault Profile → lease/CAS → stage event/receipt/Trace
→ restart or reconnect → PostgreSQL readback → replay → Reliability manifest
```

## Scope

- 闭合 Provider timeout、字段部分失败、重复提交、并发编辑、进程终止和 config 漂移六类固定故障；
- 在现有 TripCheckRun、RunSpec、lease/CAS、幂等、SSE 和 receipt 上补齐统一 Trace，不创建第二套运行状态；
- Trace 至少绑定 `bt.run_id`、workspace/revision/brief/evidence、config/rule/provider、execution mode、阶段和失败类别；
- Provider timeout/部分失败只使用受控 fixture，保留成功事实并将受影响字段标记为 `UNKNOWN`；
- 验证刷新恢复、SSE 断线续传、过期 lease 接管、并发 revision 和稳定 replay；
- 固定六类 canonical fault case；每个本阶段修复出的真实故障追加到 regression；
- 输出绑定同一 commit/config/dataset 的 Reliability manifest、原始 Trace/receipt/event/replay 和指标。

## Non-goals

- 不实现 OCR、截图输入、真实高德/天气/Brave、付费模型或公网部署；
- 不新增 migration、公共 API/schema、消息队列、Kubernetes、新 Agent 或权威 Redis 状态；
- 不扩城、不跨城，不扩人数或天数；
- 不主动开发 Builder、Planner、RAG、LoRA、Yjs、OR-Tools 或 360 数据集；
- 不晋级 live/public/user evidence，不进入 P3。

## Authority

`AGENTS.md`、`../product/PROJECT_CHARTER.md`、`../product/TRIP_CHECK_SPEC.md`、`../product/TRIP_CHECK_API_CONTRACT.md`、`PROGRAM.md`、`ROADMAP.md`、`RELEASE_GATES.md`、ADR-005 与本 Goal。

## Contract versions

- API/schema：沿用 `trip-check-api-v1` 与 migration `022`～`024`，不变更公共合同；
- Dataset increment：6 个固定 canonical fault case，加本阶段真实缺陷 regression；
- Fault profiles：`provider_timeout`、`partial_field_failure`、`duplicate_submit`、`concurrent_revision`、`terminate_after_evidence`、`config_drift`；
- Evidence output：`backend/evidence/trip_check_v1/p2/`。

## Baseline

- P1 D1 subject：`dd70870a817b84f6364804a5701950c754728f4e`；evidence commit：`7c60f27d2168d7f2a5193bbf7f01121c9e5b560a`；
- P1 已证明受控三城文本闭环、PostgreSQL 终止恢复、基础幂等/CAS/lease/SSE 和 18 pilot；
- 六类 Reliability Gate 的 timeout/partial 语义、统一 Trace 与同绑定 manifest 尚未作为完整矩阵验收；
- live Provider、公网和真人证据保持 `NOT_RUN`。

## Invariants

- PostgreSQL 是 Run、revision、event、receipt、幂等和 lineage 的权威状态；
- checkpoint/SSE/进程内状态不替代稳定副作用键、事务边界和 receipt；
- `UNKNOWN/UNAVAILABLE` 不得成为 PASS，部分失败不得丢弃成功事实；
- config hash 漂移必须 fail closed，不允许拼接不同配置的恢复结果；
- 任何语义采纳创建新 revision，旧报告 stale，完整 postcheck 后才可显示解决；
- Trace 与 artifact 不包含密钥、Authorization、原图、完整 Prompt 或未脱敏真实用户文本。

## Acceptance cases

- Provider timeout 有界结束，受影响字段为 `UNKNOWN`，其他成功事实保留；
- 字段部分失败产生 `PARTIAL` Run，成功 Evidence 可回读；
- 重复提交返回同一资源，不新增 Run/repair/revision/Provider 副作用；
- 并发编辑仅一方成功，失败方 `409` 并可回读当前 revision；
- 进程终止后由过期 lease 接管，阶段、receipt 和 replay 一致；
- config 漂移返回 `RUN_CONFIG_MISMATCH`，不继续旧 Run；
- SSE 使用 `Last-Event-ID` 无重复/丢失地续传持久事件；
- 每个 canonical case 均有 RunSpec、Trace、receipt、event、replay、metrics 和机器可读判定；
- 敏感字段扫描为 0 命中。

## Verification

- 定向：状态机、timeout/partial、幂等、CAS、lease、SSE、Trace schema、receipt 与 replay；
- PostgreSQL：事务回滚、并发、lease 接管、进程重启回读与副作用计数；
- 浏览器：刷新、SSE 断线重连和终态恢复；
- Reliability runner：六类 fault matrix 与同绑定 manifest readback；
- 回归：后端全量 pytest、Ruff、前端 build、旧双入口结构验证；
- `NOT_RUN`：真实 Provider、公网 E2E、真人证据、候选版 G0～G6。

## Budget

- 外部 Provider/模型成本：0；
- 新增 migration/依赖/基础设施：0；
- 重试必须有界并由 RunSpec 固定；
- 每个可验证切片立即 commit/push，最长 60 分钟形成远端 checkpoint。

## Pre-approved actions

- 在独立 P2 开发分支修改现有 Run/Trace/fixture/测试与证据 runner；
- 运行离线、真实 PostgreSQL 和本地受控浏览器验证；
- Gate 通过后按 Program 规则提交、推送并 fast-forward Program 集成分支。

## HITL

- 本文件当前仅为 `DRAFT`；开始 P2 前必须由用户现场批准并将状态改为 `APPROVED/IN_PROGRESS`；
- 公共 API/schema、migration、新依赖/基础设施、真实 Provider、修改 oracle、证据晋级、进入 P3、合并 `main` 或部署均需现场批准。

## Auto-advance

- Required gate：`Reliability Gate`；
- Next Goal template：`TC-P3-G01-input-provider-integrity`；
- 只有 Acceptance 全 PASS、clean tree、commit/upstream 确认、evidence 同绑定可回读且无 Stop condition 后，才可归档 P2 并生成 P3 `DRAFT`；不得自动实施 P3 或合并 `main`。

## Stop conditions

- 连续两个切片不能改善 Reliability Gate 同一门禁；
- 需要扩大城市/人数/天数或新增基础设施；
- 需要修改公共合同、历史 migration、blind/oracle 或降低 Gate；
- 出现证据矛盾、隐私问题、成本超预算或只能靠不受控重试通过。

## Completion record

- Commits：`NOT_STARTED`；
- Remote branch / upstream：`NOT_CREATED`；
- Verification results：`NOT_RUN`；
- Evidence paths：`NOT_CREATED`；
- Gate result：`NOT_RUN`；
- Next Goal generated：`NO`；
- Remaining red lights：六类 Reliability matrix 与领域 Trace 尚未完整执行；
- Promotion decision：`NOT_REQUESTED`。
