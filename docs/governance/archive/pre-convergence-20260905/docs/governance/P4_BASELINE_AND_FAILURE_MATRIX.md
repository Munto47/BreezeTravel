# P4 Advice / CandidateSet / Repair 基线与失败矩阵

## 绑定基线

- Goal：`TC-P4-G01-advice-candidate-repair`
- 开发起点：`3ea92a4dcf58d029ddd06d115fe682ed6b986524`
- P4 Program baseline：`acb1e990d01f2e8c68688a841b7023028ab90427`
- 2026-08-23 定向基线：Advice、Repair、API、并发、路线目标共 `24 passed`。

该结果只证明现有本地受控骨架，不证明 PostgreSQL、浏览器、公网 Provider、真人或候选发布。

## 现有能力与 P4 缺口

| 组件 | 已有行为 | P4 必须补齐 |
|---|---|---|
| AdviceBundle | 非 PASS Finding 可生成确定性 Advice | 字段完整性、Finding 覆盖、CandidateSet/receipt 强绑定 |
| BoundedRepair | 生成预览并运行 postcheck | 统一策略契约、策略回执、与 TSPTW/CP-SAT 同集比较 |
| Repair apply | If-Match、幂等键、CAS、单 revision | 失败恢复与 postcheck 严重问题门禁的完整回归 |
| Candidate evidence | Suggestion 主链已有地点和路线 receipt | Advice/Repair 侧冻结 CandidateSet，越界候选 fail closed |
| Evaluation | 18 pilot 与 P1-P3 回归存在 | 36 bakeoff、180 dev、72 regression 与 P4 manifest |

## 失败矩阵

| 失败 | 权威结果 | 禁止行为 |
|---|---|---|
| 无合格候选 | 返回区域/筛选条件，保留 Finding | 编造具体地点 |
| 候选不在冻结集合或 receipt 不完整 | `UNVERIFIED_CANDIDATE_REJECTED` | 进入 RepairOption |
| 路线事实缺失、过期或冲突 | `UNKNOWN/UNAVAILABLE`，Repair 不晋级 | 把缺失路线成本计为 0 |
| Solver 无解 | `UNSAT` 并回退 | 记录为 solver success |
| Solver 超时 | `TIMEOUT` 并回退 | 延长门禁或静默成功 |
| Solver 异常 | `ERROR` 并回退 | 丢失失败原因 |
| 预览 | 零权威写入 | 创建 revision 或采纳记录 |
| 重复 apply | 返回首次结果 | 创建第二个 revision |
| stale/CAS 竞争 | 409 并回读胜者 | 覆盖胜者 |
| postcheck 新增 BLOCKER/HIGH/UNKNOWN | 不得标为成功解决 | 仅凭 edit 成功声明解决 |

## 冻结比较合同

`backend/evals/trip_check_v1/p4/solver_bakeoff_v1.jsonl` 固定 36 条、三城各 12 条，包含可修复、无解、性能、超时、异常回退和确定性并列场景。每条绑定 CandidateSet、fixture receipt、oracle、2 秒超时、固定 seed、目标函数版本和 case hash；正式比较后不得原地修改。
