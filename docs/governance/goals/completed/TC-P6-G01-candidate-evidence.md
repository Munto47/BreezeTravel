# COMPLETED GOAL：P6-G01 候选证据、受控公网与 Candidate Gate

## Metadata

- Goal ID：`TC-P6-G01-candidate-evidence`
- Program ID：`TC-V1-INTERVIEW-2026`
- Phase：`P6`
- Status：`COMPLETED`
- Branch：`codex/trip-check-p6-candidate-evidence`
- Baseline commit：`9faefae0b56cf26a90f7a95ee4ba9d8f23a6951e`
- Gate subject commit：`d282035aef338bb622dde55be2585f624fc77190`
- Completed at：2026-08-26
- P5 Gate subject：`d162694a76b3ac97e9ffed71427f62e2bad6a4ee`
- Approved by / at：User / 2026-08-25
- Predecessor gate：`Evaluation Gate=PASS`
- Required gate：`Candidate Gate`

## Outcome

在一个最终冻结的 candidate commit 上重新完成 G0～G6，交付可回滚的受控 snapshot 公网候选、真实来源 OCR 证据、固定 live Provider receipts、浏览器与性能证据、90 秒和 5 分钟演示、架构/恢复图、P5 消融表以及不可变 release manifest。`/api/evidence/latest` 只读回传与 manifest 相同的候选证据边界，最终 `Candidate Gate=PASS`。

```text
P5 PASS checkpoint
→ CandidateRunSpec / evidence schema
→ G0～G5 同 commit 执行
→ exact commit 受控公网部署与 E2E
→ G6 外置只读 manifest
→ evidence readback
→ Candidate Gate
```

P6 完成仍不等于合并 `main`、生产 release、H1 真人内测、商业验证或 `human_evidence=true`。

## Scope

- 新增内部 `CandidateRunSpec`、G0～G6 runner/receipt、release manifest 与 Candidate Gate；
- 新增公网只读候选证据 schema `/api/evidence/latest`，至少包含 `subject_commit`、固定范围、G0～G6 状态、证据等级、public E2E、已知缺口、`human_evidence=false`、manifest hash；
- manifest 使用运行时只读外置挂载，避免提交 manifest 后改变被验收 commit；
- 建立 60 张获授权真实来源 OCR 数据集，北京/上海/杭州各 20；仓库只保存 hash、授权/provenance、标注版本、OCR receipt 和聚合结果；
- 实跑隔离 PostgreSQL、冻结 Provider snapshot、固定 18 次高德/和风 live 矩阵、本地浏览器主链、性能、受控公网 E2E；
- 修正公网 `/health` 路由，使候选健康检查可验证；
- 交付 90 秒视频、5 分钟完整演示、架构图、恢复时序图、P5 消融表和复现命令。

## Non-goals

- 不扩展北京、上海、杭州以外城市；不支持跨城、超过 5 人或超过 5 天；
- 不新增 Provider、付费额度、公共业务 API、migration、生产依赖、消息队列、Kubernetes 或新基础设施；
- 不新增产品能力、评分规则、prompt 优化、运行时 Agent、旧 Planner/RAG/Builder/LoRA 功能；
- 不修改 P5 frozen blind/oracle、P5 Gate、P4 Solver admission 或历史 manifest；
- 不开展 H1、真人招募/consent、商业验证，不把自动代理或公开 E2E 记为真人证据；
- 不合并 `main`，不创建 release，不把受控候选称为生产发布。

## Authority

按以下顺序执行：

1. `AGENTS.md`；
2. `docs/product/PROJECT_CHARTER.md`；
3. `docs/product/TRIP_CHECK_SPEC.md`；
4. `docs/governance/PORTFOLIO_MISSION.md`；
5. `docs/governance/PROGRAM.md`；
6. 本文件、`docs/governance/ROADMAP.md`、`docs/governance/RELEASE_GATES.md`；
7. 已接受 ADR、schema/API 合同；
8. candidate commit 对应的不可变 evidence。

用户已批准本 Goal 所列只读 evidence schema、现有零增量费用 18 次 Provider 矩阵和受控公网候选部署。任何超出本文件的公共 API、Provider、付费、migration、依赖、基础设施或 H1 仍需新的明确授权。

## Contract versions

- Candidate evidence schema：新增版本化只读 schema；旧 evidence 响应保持可审计，不原地伪造 PASS；
- CandidateRunSpec：绑定 commit/upstream、dirty tree、config、dataset、model、rule、snapshot、live matrix、PostgreSQL、browser/public URL；
- P5 input：Gate manifest hash `9a3338a565522577f4514f628b225ad165e87085a992185bd2650b197011187a`；
- OCR dataset：`real_authorized_ocr_v1`，60 张，三城各 20；原图与敏感材料只在仓库外隔离目录；
- Provider matrix：高德 12 次路线（三城各四种交通）+ 和风 3 次预报 + 和风 3 次实时预警，共 18 次；
- Evidence output：`D:/munto/code/claudeProject/agentTravel-p6-artifacts/p6-candidate/<subject_commit>/`；
- Runtime manifest：外置、只读挂载、内容 hash 回读。

## Baseline

- P5 completion checkpoint：`9faefae0b56cf26a90f7a95ee4ba9d8f23a6951e`，已推送；
- P5 Evaluation Gate：subject `d162694a76b3ac97e9ffed71427f62e2bad6a4ee`，status `PASS`，promotion `KEEP_CORE_B`；
- backend baseline：1812 passed / 29 skipped，Ruff PASS；frontend build PASS；dual-entry PASS；
- 当前 P6 G0～G6、public E2E、release manifest、Candidate Gate：`NOT_RUN`；
- real OCR dataset：`NOT_BUILT/NOT_RUN`；live 18 matrix：`NOT_RUN`；
- 公网主页与旧 evidence endpoint 可访问的历史观察不能作为候选证据；`/health` 历史状态为 404，必须在 exact candidate 上修复并重验；
- human evidence：false；H1：`NOT_STARTED`。

## Invariants

- `TripWorkspace → ItineraryRevision → EvidenceSnapshot → AuditEngine → RepairOption/EditCommand` 权威主干不变；
- 任何语义编辑或 Advice 采纳创建新 revision，旧报告 stale，完整 postcheck 后才显示解决；
- `UNKNOWN/UNAVAILABLE` 不计 PASS；Provider 局部失败保留成功事实并标注失败字段；
- 候选地点只能来自冻结 CandidateSet，并绑定地点/路线 receipt；
- 原始截图不得进入数据库、日志或 Git，成功/失败终态均删除；
- 60 张真实 OCR 原图和敏感来源材料只在隔离外部目录，仓库只接收不可逆 hash、授权、版本和聚合 receipt；
- snapshot replay 与 live Provider 是不同证据，fixture fallback 必须显式为 0 才能判 live PASS；
- public E2E、automated Judge、live Provider 和 human evidence 分级披露，不能互相替代；
- P6 任何代码、配置、数据或部署内容变化都会使旧 candidate evidence 失效，按影响范围重跑 G0～G6并生成新 manifest；
- Candidate Gate 的 commit、upstream、config、dataset、P5 manifest、Provider receipts、PostgreSQL、browser/public URL 必须一致。

## Acceptance cases

### G0：权威与合同

- 权威文档、API/schema、能力声明、追加式 migration 政策一致；
- 旧 RAG/Planner/Builder 文案不会被误认为当前产品能力；
- CandidateRunSpec、evidence schema、manifest schema、fail-closed validator 均可执行；
- public evidence 默认 `human_evidence=false`，已知缺口不能被省略或标绿。

### G1：离线质量与真实 OCR

- 完整 backend、Ruff、frontend build、dual-entry、隐私/状态机/幂等/失败语义全部 PASS；
- 60 张获授权真实来源 OCR 数据集完整，三城各 20，无跨 split 泄漏；
- 关键字段 micro-F1 ≥95%，低置信关键字段确认召回率 100%；
- 原图终态删除 100%，数据库/日志/Git 泄漏 0；
- 仓库内没有原图、敏感来源正文或未脱敏 Provider 原始凭据。

### G2：PostgreSQL

- 隔离 PostgreSQL 实跑现有 migration、事务、并发、lease 接管、进程重启回读、旧数据兼容和截图清理；
- 幂等 replay 不产生第二个 run/revision/side effect；CAS 冲突返回当前 revision；
- 测试服务自动启停，receipt 绑定 exact candidate config 与数据库版本。

### G3：冻结 snapshot

- Provider snapshot 在无网络模式重放；
- receipt、事实、终态和 replay hash 100% 一致；
- 网络请求为 0，snapshot/version/config hash 全部回读。

### G4：固定 live Provider 矩阵

- 只运行批准的 18 次：三城各四种高德路线、各一次和风天气预报、各一次实时预警；
- fixture fallback=0；请求/响应/时间/config/失败分类 receipt 完整且脱敏；
- 凭据、配额或网络失败记为外部阻断，不更换 Provider、不扩大调用范围、不把 snapshot 当 live PASS。

### G5：本地候选、性能与受控公网

- 本地浏览器覆盖文本、截图、TripBrief 确认、地点消歧、刷新、SSE 断线、进程重启、局部 Provider 失败、Advice 采纳、新 revision、完整 postcheck；
- 首次反馈 ≤1 秒、解析确认 P95 ≤3 秒、三图 OCR P95 ≤12 秒、基础报告 P95 ≤30 秒、含风险报告 P95 ≤45 秒；
- 部署前只读审计阿里云环境，形成数据库备份、旧版本保留、回滚和验证清单；
- 部署 exact candidate commit，公网只用受控 snapshot，CORS/Secret/限流/截图清理全部通过；
- `/health` 返回可验证健康状态；public E2E 全链 PASS 后才允许更新候选 evidence；
- 部署后主链、数据或隐私异常立即回滚，旧公开 evidence 保持不变。

### G6：不可变 manifest 与交付物

- 外置 manifest 绑定同一 candidate commit、config、OCR 数据、P5 manifest、模型、规则、snapshot、live receipts、PostgreSQL、浏览器性能和公网 URL；
- manifest 以运行时只读方式挂载，发布后内容 hash 不再变化；
- `/api/evidence/latest` 回读 schema、manifest hash、disclosure、`human_evidence=false` 和已知缺口准确；
- 90 秒演示、5 分钟完整演示、架构图、恢复时序图、P5 消融表和复现命令全部可回读；
- secret/privacy scan 0 命中，artifact index 全部 hash 可回读。

### Candidate Gate

- G0～G6 与 public E2E 全部在同一 candidate commit 上实际 `PASS`；
- release manifest、evidence endpoint 和部署 readback 绑定一致；
- 任一 Gate 为 `NOT_RUN/BLOCKED/FAIL` 时 Candidate Gate 为 `REJECT`；
- 最终状态只能标记候选就绪，不得晋级为 H1、真人、生产或商业证据。

## Execution plan

1. 冻结 P6 合同并实现 CandidateRunSpec、evidence/manifest schema、validator 与 fail-closed tests；
2. 完成 G0/G1 和 60 张真实来源 OCR 数据集；
3. 依次完成 G2 PostgreSQL、G3 snapshot、G4 固定 live matrix；
4. 完成本地 G5 浏览器/恢复/性能；
5. 只读审计服务器、备份并部署 exact candidate，完成公网健康与 E2E；
6. 生成并只读挂载 G6 manifest，回读 evidence endpoint；
7. 生成演示交付物与 Candidate Gate，全部 PASS 后归档 P6。

每个可验证切片执行“定向验证 → diff → 显式暂存 → staged diff/check → commit → push”，最长 60 分钟形成远端 checkpoint。

## Verification

基础命令：

```powershell
cd backend
python -m pytest tests/ -q
python -m ruff check app evals scripts tests

cd ../frontend
npm run build

cd ..
python backend/scripts/validate_dual_entry_testset.py
```

P6 还必须由版本化 runner 实际执行并回读：G0 文档/schema、G1 real OCR、G2 PostgreSQL、G3 no-network snapshot、G4 18 live receipts、G5 local/public browser/performance、G6 manifest/evidence、Candidate Gate。仅存在测试文件或历史 PASS 不计当前证据。

## Budget and checkpoints

- 预计窗口：8～12 个专注日，以 Gate 结果为准；
- 外部调用：固定 18 次，禁止隐式 retry 扩大矩阵；
- 增量费用：0；任何付费/绑卡/新增额度停止；
- 数据：60 张真实来源 OCR，三城各 20，不扩城；
- 正式 candidate evidence 只在冻结 commit 上运行；任何修复产生新 subject 与新 manifest；
- 最长 60 分钟一个可恢复本地/远端 checkpoint。

## Pre-approved actions

- 在本 P6 分支实现内部 CandidateRunSpec、只读 evidence schema、manifest、G0～G6 runner/test/receipt 和 `/health` 修复；
- 生成/复核 60 张真实来源 OCR 的仓库外数据与仓库内脱敏合同；
- 自动启停隔离 PostgreSQL 和本地候选服务；
- 运行现有零增量费用凭据的固定 18 次 Provider 矩阵；
- 对现有阿里云环境先只读审计、备份/保留旧版本，然后部署 exact candidate 并在失败时回滚；
- 使用开发子代理做独立复核、故障诊断与无 API 自动 Judge；
- 在开发分支 commit/push；不需要逐文件重复批准。

## HITL and stop conditions

以下任一情况立即停止当前推进并使受影响证据失效：

- blind 泄漏、OCR/source 隐私泄漏、secret 泄漏、目标外数据差分或 evidence 绑定矛盾；
- 需要新增 Provider、付费、公共业务 API、migration、生产依赖或新基础设施；
- 无法证明 60 张 OCR 来源授权，或原图进入数据库/日志/Git；
- live matrix 需要超过 18 次、fixture fallback 非 0，或只能用 snapshot 冒充 live PASS；
- 服务器 exact commit、config、manifest 或回读不一致；
- 部署后主链、数据或隐私异常；必须回滚且不更新 evidence；
- 连续两个切片无法改善同一 Gate，独立诊断后仍需扩大范围或降低门槛；
- 请求进入 H1、真人测试、合并 `main`、production release 或对外商业声明。

凭据/配额/网络等外部故障记为 `BLOCKED_EXTERNAL`，不得通过新增 Provider、扩大调用或降低 Gate 追绿。

## Auto-advance

- Required gate：`Candidate Gate`；
- Next Goal template：无；P6 完成后只能归档并保持 H1 `NOT_STARTED`；
- Candidate Gate PASS 不自动授权 H1、公网长期运行、生产 release、main 合并或真人招募。

## Completion record

- Candidate subject：`d282035aef338bb622dde55be2585f624fc77190`，candidate tree clean，subject/upstream 在 Gate 决策时一致；
- Remote branch / upstream：`origin/codex/trip-check-p6-candidate-evidence`，候选分支保持指向 subject，不承载 Gate 后治理归档提交；
- Verification results：G0～G6 全部 `PASS`；G0 12/12，G1 18/18，G2 13/13，G3 12/12，G4 14/14，G5 16/16，最终公网 disclosure readback `PASS`；
- G1：60 张真实授权来源，三城各 20，关键字段 micro-F1 `0.971718`，低置信确认召回 `1.0`，三图 OCR P95 `4087.537ms`，原图终态清理 60/60，Git/日志/数据库泄漏 0；
- G4：高德路线 12 次、和风预报 3 次、实时预警 3 次，共 18 次；Provider failure、hidden retry、fixture fallback、secret leak 均为 0；
- G5：本地浏览器主链、恢复、局部 Provider 失败、Advice、新 revision、完整 postcheck 与受控公网 E2E 全部 `PASS`；首次反馈 P95 `78.188ms`；
- Evidence root：`D:/munto/code/claudeProject/agentTravel-p6-artifacts/p6-candidate/d282035aef338bb622dde55be2585f624fc77190/`；
- Release manifest hash：`ce1bb33425388ce84defa730157befb234d3af05454ec8d18998201160748c2d`；Candidate Gate receipt hash：`cb7e2cab918aedcb22cb97bfbc7c62d61b37b1b74f3416ee78712c1c6793e8ce`；final disclosure readback hash：`3ec5a872406c484e6e03192e2bb3e6824e8d9f32c3d0c3f2c386c87acb486788`；
- Public evidence：`https://www.breezetravel.cn/api/evidence/latest`，只读回传 `Candidate Gate=PASS`、G0～G6=`PASS`、`human_evidence=false`，响应体 SHA 与 final evidence 文件一致；
- Gate result：`Candidate Gate=PASS`；
- Next Goal generated：无；H1 保持 `NOT_STARTED`，必须另行取得真人招募、consent 与 evidence 晋级批准；
- Remaining red lights：`HUMAN_EVIDENCE_NOT_RUN`、`CONTROLLED_SNAPSHOT_PUBLIC_ONLY`、`PUBLIC_CPU_OCR_12S_PERFORMANCE_NOT_PROVEN`、`NO_MAIN_MERGE`、`NO_PRODUCTION_RELEASE`、`NO_H1_HUMAN_TESTING`；
- Promotion decision：`NOT_REQUESTED`；P6 归档，不自动进入 H1、合并 `main`、创建 release 或生产发布。
