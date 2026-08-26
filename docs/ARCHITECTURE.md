# BreezeTravel「行程查」V1 架构

> 状态：`ACCEPTED`
>
> 决策依据：`adr/ADR-005-modular-trip-check-and-evidence-lab.md`

## 1. 模块化单体

```text
文本/截图
→ OCR / Parser
→ TripBriefRevision 确认
→ ItineraryRevision
→ Provider Adapters
→ EvidenceSnapshot
→ AuditEngine
→ Advice / Constraint Repair
→ 用户预览采纳
→ 新 ItineraryRevision
→ 完整 postcheck
```

HTTP API、领域服务、工作流、Provider adapter、评测 runner 和后台任务仍部署在同一个 FastAPI 应用中。模块通过 Pydantic 合同和 repository 边界协作，不通过网络拆成服务。

## 2. 权威对象

```text
TripWorkspace
  ├─ TripBriefRevision
  ├─ ItineraryRevision
  ├─ TripCheckRun ─ RunSpec
  ├─ EvidenceSnapshot ─ EvidenceFact / ProviderFailure
  ├─ AuditReport ─ AuditFinding
  └─ AdviceBundle ─ CandidateSet / RepairOption / EditCommand
```

- TripBrief 保存本次已确认输入；推断字段不能成为 HARD。
- ItineraryRevision 是不可变行程版本；所有语义编辑产生新 revision。
- EvidenceSnapshot 保存特定 revision/config 下的事实和局部 Provider 失败。
- AuditEngine 是三态 Finding 唯一来源。
- Advice 只能引用 Finding、Evidence 和冻结候选；采纳后必须完整 postcheck。

## 3. TripCheckRun

```text
PARSE
→ WAIT_BRIEF_CONFIRMATION
→ RESOLVE_PLACES
→ COLLECT_EVIDENCE
→ AUDIT
→ BUILD_ADVICE
→ WAIT_ADOPTION
→ POSTCHECK
```

LangGraph 负责编排阶段、HITL、SSE 和 checkpoint。PostgreSQL `TripCheckRun` 记录业务阶段、lease、attempt、config hash 和每阶段回执；checkpoint 只表示可恢复计算进度。

Provider 请求和写操作使用由 `run_id + stage + normalized_input_hash` 派生的稳定幂等键。数据库 mutation 在事务内写业务状态和命令回执；外部 Provider 不能宣称 exactly-once，重复执行必须通过幂等键、缓存 receipt 或安全重放约束其影响。

进程重启后，worker 只接管过期 lease，并校验 RunSpec/config hash。配置漂移时拒绝恢复，创建新 Run；不同配置的阶段结果不得拼接。

## 4. 状态所有权

- PostgreSQL：brief、revision、run、lease、幂等命令、receipt、evidence、finding、advice 和 lineage。
- Redis：缓存、限流和可丢失协调；Redis 丢失不改变权威结果。
- 临时文件：原始截图；成功、失败、取消和超时终态均删除。
- SSE：进度投影；断线不取消后台 Run，重连按稳定事件 ID 续传。

## 5. Evidence 与失败

Provider adapter 返回字段级事实、规范化请求/响应 hash、observed_at、有效期和失败类别。部分字段失败时保留成功事实，缺失字段保持 `UNKNOWN/UNAVAILABLE`。

```text
AuditFinding
→ rule_id/rule_version
→ EvidenceFact
→ EvidenceSnapshot
→ Provider receipt
→ RunSpec/config hash
```

模型生成的解释不能创建 EvidenceFact；无来源、过期或冲突证据不能转成 PASS。

## 6. 约束修复

默认先使用现有 BoundedRepairSearch。P4 通过统一 RepairEngine adapter 比较：

- RoutingModel/TSPTW：固定候选集合、路线顺序、等待和时间窗；
- CP-SAT：地点选择、锁定事件、软约束和最小修改成本。

所有候选必须生成 preview revision、刷新受影响 Evidence，并执行完整 postcheck。只有通过 `RELEASE_GATES.md` 的 Solver Admission Gate 才能成为默认策略。

## 7. 证据实验台

```text
Versioned RunSpec + Fault Profile
→ Trace / Receipt / Snapshot
→ Deterministic Replay
→ Legacy A / Core B / Solver C
→ Release Manifest
```

固定故障包括 Provider 超时、字段级失败、重复提交、并发编辑、进程终止和 config 漂移。每次实验输出原始结果、指标、hash 和不可变 manifest，不能只保存摘要。

## 8. OpenTelemetry

领域 Span 覆盖 Run stage、Provider、模型、Audit、Repair 和 postcheck，固定属性为：

```text
bt.run_id
bt.itinerary_revision
bt.brief_revision
bt.evidence_snapshot_id
bt.config_hash
bt.rule_set_version
bt.provider
bt.execution_mode
bt.failure_category
```

禁止记录原图、完整 Prompt、原始用户文本、密钥、Authorization、未脱敏 Provider 响应或可还原个人身份的字段。

## 9. Legacy 边界

`backend/app/agents/graph.py` 的 ReAct/Critic 图、旧 Planner、RAG、LoRA Router 和 Yjs 只保留最低回归，并在 P5 作为 Legacy A 运行。它们不得写入 V1 Finding、修改权威 revision，或替代 V1 Gate。
