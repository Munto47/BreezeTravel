# 双入口本地验证记录（2026-08-20）

本记录只证明当前工作树在受控本地环境中的结果，不等于公网 E2E、真实 Provider 可用性或真人产品校准。

## 自动门禁

- 后端默认门禁：在 `backend` 目录执行 `python -m pytest tests -q`，结果 `812 passed, 21 skipped, 38 warnings`；`external` 标记默认跳过，只有显式 `RUN_EXTERNAL_TESTS=1` 才允许 Provider/GPU 测试，避免本地 `.env` 静默加载 LoRA 或调用外部服务。从仓库根执行会因一个历史测试使用 `evidence/...` 相对路径而产生 cwd 失败，不属于代码回归。
- Python 静态检查：`python -m ruff check app tests scripts`，结果 `All checks passed!`。
- 前端生产构建：`npm run build`，Next.js 编译、类型检查、9 个页面生成成功；`/import` 为 10.5 kB，First Load JS 111 kB。
- PostgreSQL：执行 009/010/011/012/013 迁移，并验证完整 import → audit → Repair → apply → 幂等重放 → revision/report/Tips artifact 回读；同时覆盖创建型 command 租约/接管/回滚、Import state CAS、`current_import_id`、成员约束行锁、Audit 输入漂移、兄弟 Repair 并发、路线目标回读，以及单事务 workspace resume、Repair apply lineage、IDOR 同形 404 和重启回读，`6 passed in 8.86s`。测试容器随后已停止。
- 发布清单默认要求的数据库迁移已同步推进到 `013_idempotent_creation_commands.sql`，避免未来发布包漏报 012/013 前置；这只是本地发布合同修复，不代表已经公网发布。
- `git diff --check`：通过；仅有 Git 的 LF/CRLF 工作区提示。

## 本地浏览器闭环

使用本地前后端和 PostgreSQL，已重复完成：

1. 创建 room/workspace；
2. 导入两日文本，得到 NOT_FOUND；
3. 编辑查询并受控重搜 Provider 候选，其他 resolution version 不变；
4. apply 为 revision 1；
5. Audit 检出时间链冲突并回读 Evidence；
6. 生成“顺延后项”和“缩短前项”两个真实差异方案；
7. apply Repair，完整 postcheck 后推进到 revision 2；
8. 刷新页面，从 URL 引用和服务端恢复 import、revision、report、Evidence、Repair 状态。

该记录是 `local_e2e`，不是公网双入口 E2E。

## P2 规则源收敛

- `critic_v2` 不再位于 Planner 主图。
- 固定 parity 数据集覆盖 Critic 独有的无餐饮、餐饮超限和相邻二级品类重复；逐规则测试同时覆盖营业时间、用餐窗、时间链、天气暴露和每日酒店。
- canonical AuditApplicationService 为所有审计与 Repair postcheck 注入同一组系统约束；legacy verifier 仅用于 Planner 内部兼容自检。
- 持久化 Planner 在 revision 与 AuditReport 成立前不生成 Tips；Repair UI 也只在 postcheck 后请求 Tips。产物以 report 为主键绑定 revision/content hash，审计未通过时返回明确拒绝，UNKNOWN 会作为待确认输入保留。

## 移动端复用合同与成员版本底座

- Import 新增 `state_version`、ETag 和 compare-and-set；confirm、重搜和 apply 强制携带 `If-Match`，缺失返回 `428 IF_MATCH_REQUIRED`。双设备对同一状态写入时只允许一方提交，另一方得到稳定 `IMPORT_STATE_CONFLICT`。
- APPLIED/FAILED import 不再允许修改 resolution；apply 丢包后使用 `(import_id, Idempotency-Key)` 回放原 revision，相同 key 不同请求拒绝为 `IDEMPOTENCY_KEY_REUSED`。
- workspace 提供有界 import 列表与 latest/unfinished 查询，支持只保留 workspace id 的客户端恢复导入入口状态。
- `GET /trip-workspaces/{workspace_id}/resume` 在单个 `REPEATABLE READ READ ONLY` 事务中恢复 current revision/import/report/evidence/proposed 或 applied Repair/Tips；聚合 ETag 支持 304，写入仍使用独立 itinerary/import ETag。无权限与不存在同形 404，损坏 lineage 返回 `WORKSPACE_STATE_INCONSISTENT`。
- Import、Audit create/refresh、Repair propose 和 Tips generation 均使用持久化 creation command；同 key 同 payload 回放，同 key 异 payload 409，计算阶段之外的领域写入与 command success 同事务发布。浏览器在响应丢失后会保留同一逻辑命令键。
- 成员约束已具备 append-only workspace revision、历史集合重建及 Audit hash 绑定；来自 MEMORY/INFERRED 的内容只能保持 SOFT。P7 的成员 API、分享权限和界面尚未启动。
- 路线成本不是固定 0：仅当 source/candidate 所需 edge 都有唯一、一致、FRESH 的时长事实时返回真实分钟差，否则明确为 `null`。
- Audit 保存会在同一事务内复核 itinerary、task 和 member constraint 输入版本；任一漂移都返回 `AUDIT_INPUT_STALE`，旧输入报告不能重新成为 current。
- 兄弟 Repair apply 统一使用 workspace → 候选集合的确定锁顺序，避免互锁；空白拒绝原因返回稳定 `INVALID_REPAIR_REJECTION_REASON`，不会泄漏为 500。

## 合成行程与模拟排雷

- 独立数据集：北京/上海/杭州各 50 条，共 150 条；60 条模拟 AI 原始行程、60 条受控变异、30 条边界样本。
- runner 实际调用 parser、确定性 fixture resolver、EvidenceService、AuditEngine 和 BoundedRepairSearch，不访问网络或外部 LLM。
- 最终结果：原始错误按 Parser/Resolution/Audit 三阶段 30/30 捕获，60/60 注入类别在配对差分中出现，30/30 边界样本要求确认；额外未标注诊断为 0，138 条合理 UNKNOWN 保留，30/30 可修复案例得到 45 个 Repair preview。
- 输入和结果绑定 SHA256；混入真人字段、数量或哈希漂移都会 fail closed。
- 该结果是 synthetic diagnostic，`quality_gate=false`、`public_claim_eligible=false`。详细记录见 `synthetic-auditor-simulation-2026-08-20.md`。

## 尚未通过

- M1 聚合器结果：`BLOCKED_HUMAN_DATA`。
- 真人样本：0/30 份行程、0/15～20 名组织者。
- P3 的真实 blind 解析 F1、实体 precision/recall 尚无真人数据。
- Tips 的真实 LLM 文案质量没有调用 Judge，也没有真人评分；当前只验证绑定、时序、拒绝和持久化合同。
- 公网部署、新链路真实 Provider、公网双入口 E2E 均未在本记录中验证。
