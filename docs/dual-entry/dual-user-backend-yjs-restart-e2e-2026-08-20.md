# Backend/Yjs 九场景真实重启门禁（2026-08-21）

## 结论

G5 的 Backend/Yjs 恢复门禁已从单个杭州代表场景扩展为 9 个相互隔离的场景，并于 2026-08-21 在 Windows + Docker Desktop 本地全栈实际通过：`1 passed (48.3s)`，测试主体耗时 39.6 秒。

九个场景在同一次测试内分别创建唯一 `case_id`、`seed_id`、room、workspace 和 itinerary；北京、上海、杭州各 3 个，分别执行 `REORDER_STOP`、`MOVE_TO_DAY`、`ADJUST_TIME`。所有场景完成 revision 2、完整审计、成员 `HARD latest_return_time=20:30` 约束、可用地图投影、真实产品 SuggestionSet 与非空推荐事件账本，并经两个浏览器和两个认证 Yjs 客户端回读。

关闭全部重启前浏览器/Yjs 客户端后，测试只对 Compose 中命名的 Backend 和 Yjs 执行一次 `stop/start`。9 个全新 Yjs client 在任何恢复浏览器创建前逐 case 读回持久化引用；之后每个场景再由两个全新 Chromium context 通过公开 HTTP 回读。Python `restart_gate` 对机器证据二次解析结果为：`PASS / RESTART_EVIDENCE_VALID / errors=[] / case_count=9`。

这仍是 `local_fixture` 恢复证据，不是 live Provider、真实用户、公网 HTTPS 或发布证据。

## 九场景矩阵

| case_id | 城市 | 独立编辑 | 权威 revision | 地图 | Backend 事件 | Yjs place/event |
|---|---|---|---:|---|---:|---:|
| `g5.bj.restart.reorder-map` | 北京 | `REORDER_STOP` | 2 | AVAILABLE | 2 | 1 / 1 |
| `g5.bj.restart.move-day-ledger` | 北京 | `MOVE_TO_DAY` | 2 | AVAILABLE | 2 | 1 / 1 |
| `g5.bj.restart.adjust-time-audit` | 北京 | `ADJUST_TIME` | 2 | AVAILABLE | 2 | 1 / 1 |
| `g5.sh.restart.reorder-map` | 上海 | `REORDER_STOP` | 2 | AVAILABLE | 2 | 1 / 1 |
| `g5.sh.restart.move-day-ledger` | 上海 | `MOVE_TO_DAY` | 2 | AVAILABLE | 2 | 1 / 1 |
| `g5.sh.restart.adjust-time-audit` | 上海 | `ADJUST_TIME` | 2 | AVAILABLE | 2 | 1 / 1 |
| `g5.hz.restart.reorder-map` | 杭州 | `REORDER_STOP` | 2 | AVAILABLE | 2 | 1 / 1 |
| `g5.hz.restart.move-day-ledger` | 杭州 | `MOVE_TO_DAY` | 2 | AVAILABLE | 2 | 1 / 1 |
| `g5.hz.restart.adjust-time-audit` | 杭州 | `ADJUST_TIME` | 2 | AVAILABLE | 2 | 1 / 1 |

每个场景保存并逐字段比较以下内容：

- Backend `/resume` 的 revision、`content_hash`、audit report ID/revision/status、member constraint revision；
- `/members` 的两个成员及 B 本人确认的 HARD 约束；
- `/map-projection` 完整响应及其 canonical JSON SHA-256，状态必须是 `AVAILABLE`；
- `/recommendation-events` 的非空、服务端生成事件账本；本轮每个场景包含 `suggestions_shown` 和 `candidate_previewed`；
- Yjs `itineraryRef`、`auditRef`、`memberConstraintsRef`、`mapRef`、`places`、`builderEvents`；
- 重启前 A/B 浏览器、重启前 fresh Yjs、重启后 browser 之前 fresh Yjs、重启后 A/B 浏览器，必须与同一 expected 精确相等。

## 真实进程换代证据

本轮 Backend 容器 ID 保持不变，但宿主 PID `854 → 1806`，容器 StartedAt 从 `2026-08-21T06:36:11.912096156Z` 变为 `2026-08-21T06:36:50.776577022Z`，应用 boot instance ID 从 `245b2ebf-704a-4e94-981d-27bdde369a7a` 变为 `bb42596e-5c52-4853-92ed-93761e991773`。

Yjs 容器 ID同样保持不变，但宿主 PID `1084 → 1569`，StartedAt 从 `2026-08-21T06:36:15.712066654Z` 变为 `2026-08-21T06:36:49.079272920Z`，boot instance ID 从 `c5805382-346d-4d77-a96b-a9658e1cf22b` 变为 `5ad75298-5dd8-4f40-95ad-e49f40b5531a`。重启前后 `/data` 均绑定命名卷 `agenttravel_yjs-data`。

PostgreSQL 容器 ID 和 StartedAt 在整个换代过程中不变；测试也确认 Backend 8000 与 Yjs 1234 在 stop 后均不可访问，才允许执行 start。这些证据共同排除了“进程内 repository 重建冒充重启”。

## 清理证明

客户端没有直连 SQL 或 LevelDB。门禁结束后：

- Backend 的 loopback-only、`local_fixture`、一次性高熵 secret 保护的公开 HTTP cleanup 一次删除 9 个 E2E room 和 2 个 E2E 用户，回执为 `postgres=CLEARED, room_count=9`；
- Yjs 的同等级 loopback-only 批量 HTTP cleanup 一次清除 9 个持久化 doc，回执为 `yjs_documents=CLEARED, room_count=9`；
- 两个 cleanup 只接受受限 E2E 前缀、数量上限和唯一 ID；一次成功后 secret 立即失效；
- runner 记录本轮开始时四个依赖服务均未运行，结束时仅停止本轮自己启动的 `postgres/redis/backend/y-websocket`，恢复了原始服务状态。

## 可复现入口与机器产物

```powershell
cd D:\munto\code\claudeProject\agentTravel
powershell -NoProfile -ExecutionPolicy Bypass -File .\frontend\scripts\run-dual-user-restart-e2e.ps1
```

关键文件：

- `frontend/e2e/dual-user-restart-matrix.spec.js`：九场景种子、公开 HTTP/Yjs/browser 精确回读、真实重启与批量清理；
- `frontend/scripts/run-dual-user-restart-e2e.ps1`：Docker 依赖、重启授权、原状态恢复与诊断回执；
- `backend/evals/continuous/restart_gate.py`：独立证据 validator 和 allowlisted command；
- `backend/evals/run_specs/dual-entry-builder-http-slice.json`：绑定 9 个 case ID、required count、证据路径和 v3 command；
- `backend/evidence/full_stack/dual_user_backend_yjs_restart_2026-08-20.json`：schema 3.0 业务与进程证据，SHA-256 `2d3603218db5b3b1f4840dc8911d3fae7ccb7c1780e18b50e4b0446bcfffd658`；
- `backend/evidence/full_stack/dual_user_backend_yjs_restart_playwright.json`：Playwright 原始报告；
- `backend/evidence/runtime_diagnostics/backend_yjs_restart_latest.json`：runner PASS/UNAVAILABLE/FAIL 诊断回执。

## 与 §17.6 的边界

本矩阵专门证明 §17.6 第 7～9 项的批量恢复可靠性，并强化了 revision/content hash、audit、member、map、Backend event ledger 和 Yjs refs 的逐 case 精确合同。导入消歧/Repair、四站连续候选、拖拽/按钮/Undo、双客户端同 base 的 200/409 等业务流程由其他 G5 浏览器门禁和 Builder HTTP 门禁分别证明；本矩阵不会用恢复结果替代那些独立证据。

未覆盖 live Amap/天气、LLM、真实用户、公网、多实例滚动部署和跨网络恢复；这些未完成项不能由本地 fixture 结果提升为通过。
