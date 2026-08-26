# 双入口外部证据补齐结果（2026-08-20）

## 结论

本轮完成了两项可真实执行的阻断工作：

1. 北京、上海、杭州的真实高德实体、真实高德驾车路线和真实和风天气 Provider 采样与性能统计；
2. 两个隔离 Chromium 用户在同一 workspace 协作、revision 冲突，并在 Backend 与 Yjs 真实重启后的全栈恢复 E2E。

另完成三名独立 `gpt-5.6-sol` 子 Agent 对 150 条合成审计样本的 M1-dev 代理校准。该结果严格标记为 `synthetic_proxy`，不能替代 30 份真实行程、15～20 名真实组织者、consent 与真人判断，也不能宣传为真人结果或发布批准。

因此当前状态是：**真实 Provider 本地授权证据通过；自动化双用户重启 E2E 通过；M1-dev 合成代理门禁通过；最终真人门禁仍为 `BLOCKED_HUMAN_DATA`。**

## 1. 真实 Provider 样本和性能

### 1.1 固定样本

| 城市 | 高德实体 | 高德驾车路线 | 和风天气 |
|---|---|---|---|
| 北京 | 故宫博物院 | 故宫博物院 → 天坛公园 | 北京 3 日预报 |
| 上海 | 上海博物馆 | 上海博物馆 → 东方明珠 | 上海 3 日预报 |
| 杭州 | 西湖风景名胜区 | 西湖断桥 → 灵隐寺 | 杭州 3 日预报 |

运行条件为 `RUNTIME_PROFILE=local_real`、`AMAP_MOCK=false`、`DEMO_MODE=false`，每个城市、每种 Provider 连续执行 3 次，共 27 次真实外部调用。证据只保存脱敏 request hash、Provider response hash、时间、状态、延迟、结果数和错误分类，不保存认证材料或完整响应。

### 1.2 实际性能

| 类型 | 成功 | 成功率 | P50 | P95 | 最大值 |
|---|---:|---:|---:|---:|---:|
| 高德实体 | 9/9 | 100% | 189.35 ms | 215.26 ms | 219.00 ms |
| 高德路线 | 9/9 | 100% | 66.69 ms | 79.40 ms | 80.21 ms |
| 和风天气 | 9/9 | 100% | 103.24 ms | 187.92 ms | 243.86 ms |
| 合计 | 27/27 | 100% | 103.24 ms | 216.19 ms | 243.86 ms |

三城精确实体预期命中为 9/9，`errors_by_category={}`。原始证据位于 `backend/evidence/real_provider_local_authorized/summary.json`，SHA-256 为 `39c33ab7226aece4d6e9db3e6080c0e045788424aba29fe395459558d3089601`。

### 1.3 新链路应用适配器 smoke

直接 REST 成功不能证明应用适配器已接通，因此又通过实际代码路径执行北京样本：

| 应用路径 | 结果 | 延迟 | 回读样本 |
|---|---|---:|---|
| `AmapEntityCandidateProvider.search` | 1/1 live | 238.78 ms | 故宫博物院，Provider place ID 可回读 |
| `AmapRouteEvidenceProvider.fetch` | 1/1 live | 74.81 ms | 故宫→天坛，20 分钟、6.092 km |
| `weather_fetcher.run` | 1/1 live | 232.32 ms | 当日天气、降水与 trace 可回读 |

适配器 smoke 发现 planner 天气节点此前只识别 `qweather_api_key`，且硬编码 `devapi.qweather.com` 与 query key，无法使用项目当前 JWT/custom-host 配置。现已改为：

- JWT/API-key 完整凭据判断；
- 使用 `settings.qweather_api_host`；
- 复用 Authorization / `X-QW-Api-Key` 请求头构造。

适配器证据位于 `backend/evidence/real_provider_local_authorized/adapter_smoke.json`，SHA-256 为 `73fcfc28bfd8e4f827a6469fb43eb3f201420aa085a6bdcd6fae7647e9306b22`，并已由总证据绑定。相关回归 49 passed，Ruff 通过，凭据泄漏扫描全部为 false。

复现：

```powershell
$env:PYTHONPATH='backend'
$env:RUNTIME_PROFILE='local_real'
$env:AMAP_MOCK='false'
$env:DEMO_MODE='false'
python backend/scripts/verify_real_providers.py --iterations 3 --timeout-seconds 10 --strict
python backend/scripts/verify_provider_adapters.py --strict
```

## 2. 双浏览器用户 + Backend/Yjs 重启 E2E

### 2.1 样本

- 两个独立 Chromium browser context；
- 两个独立测试账号 A/B；
- 同一杭州两日 room/workspace；
- revision 1：Day 1 为“西湖 09:00–10:30 → 灵隐寺 11:30–13:00”；
- B 写入本人 `HARD latest_return_time=20:30`，A 可回读；
- A/B 同时基于 revision 1 执行第一站下移，各自使用不同 command ID，但 `If-Match` 都是 revision 1。

### 2.2 实际事件链

1. 两个隔离 context 登录并进入同一工作台；
2. B 写入成员 HARD 约束，A 刷新后读取同一 member constraint revision 1；
3. A/B 并发编辑，HTTP 状态严格为 `[200, 409]`；
4. 服务端只存在 revision `[1, 2]`，败方乐观预览回滚；
5. 胜方执行完整审计，report 绑定 revision 2；
6. 关闭两个 context，执行 `docker compose restart backend y-websocket`；
7. PostgreSQL/Redis 不重启，Yjs 继续挂载 `agenttravel_yjs-data`；
8. 浏览器重连前，一个全新 Yjs client 先从持久卷回读 r2/content hash/report/member-r1；
9. 两个全新 context 再分别回读，五项权威引用逐字段一致；
10. 重启后 edit POST 总数仍为 2，revision 仍为 `[1, 2]`，失败旧命令未重放；
11. 测试 room、workspace、账号和 Yjs document 全部清理。

该单场景证据已在 2026-08-21 被九场景矩阵取代。最新实际复跑为 `1 passed (48.3s)`：北京、上海、杭州各 3 个独立 room/workspace/seed，一次真实 Backend/Yjs `stop/start` 后，9 个 fresh Yjs client 先于任何浏览器重连恢复精确引用，再由每 case 两个全新浏览器回读相同 revision 2/content hash/report/member/map/event ledger。Backend/Yjs 的 boot UUID、StartedAt 和宿主 PID 均换代，PostgreSQL StartedAt 不变，Yjs named volume 保持 `agenttravel_yjs-data`。

业务证据：`backend/evidence/full_stack/dual_user_backend_yjs_restart_2026-08-20.json`，schema 3.0，SHA-256 为 `2d3603218db5b3b1f4840dc8911d3fae7ccb7c1780e18b50e4b0446bcfffd658`。Playwright 原始报告 SHA-256 为 `9d89d8a0fa3d8bd3ee3fc3ba4639c979fa9db14497404d94131408c3f9720e73`。

复现：

```powershell
powershell -ExecutionPolicy Bypass -File .\frontend\scripts\run-dual-user-restart-e2e.ps1
```

该 runner 会 fail closed 检查 `AMAP_MOCK=true`、`FT_ROUTER_ENABLED=false`，只有显式设置一次性重启授权后才允许 spec 重启 Backend/Yjs；它不会执行 `docker compose down` 或删除持久卷。

## 3. 5.6-sol 合成代理校准

### 3.1 样本与独立性

- 北京、上海、杭州各 50 条，共 150 条；
- 60 条合成原始行程、60 条受控变异、30 条边界样本；
- 三个独立 `gpt-5.6-sol` 子 Agent 分别扮演 `proxy-evaluator-1/2/3`；
- 每个角色 150/150 覆盖，`blind=true`、`independent=true`、`may_read_other_evaluator_outputs=false`；
- 任何 artifact 都不含 `human_label`、`human_validated`、真人身份或发布批准字段。

### 3.2 Fail-closed gate 结果

| 指标 | 结果 |
|---|---:|
| Critical precision | 1.00 |
| Critical recall | 1.00 |
| 三角色关键一致率 | 1.00 |
| Critical Evidence 回读率 | 1.00 |
| 合成审计 artifact 在 180 秒内完成率 | 1.00 |
| 合成 Repair 采纳率 | 1.00 |

最终状态为 `M1_DEV_PROXY_PASSED`，`validation_errors=[]`。Gate 文件位于 `backend/results/auditor_simulated/current_20260820/m1_dev_proxy_gate.json`，SHA-256 为 `4cfe091cd46160907469af69c7446fc24ea5818d2e4b18f53bd074232efbba2e`。

这里的“180 秒内完成”是合成 artifact 的合同字段，不是 15～20 名真实组织者的实际操作耗时；Repair 采纳率也是合成决策，不是用户采纳率。

复现聚合：

```powershell
$env:PYTHONPATH='backend'
python backend/scripts/run_m1_dev_proxy_gate.py `
  --bundle backend/results/auditor_simulated/current_20260820/proxy_blind_bundle.json `
  --report backend/results/auditor_simulated/current_20260820/m1_dev_proxy_gate.json `
  --artifact backend/results/auditor_simulated/current_20260820/proxy_role_1.json `
  --artifact backend/results/auditor_simulated/current_20260820/proxy_role_2.json `
  --artifact backend/results/auditor_simulated/current_20260820/proxy_role_3.json
```

## 4. 发布边界

本轮不能把合成代理输出改写成真人结果。这样做会破坏证据 provenance，并与仓库的人类聚合器 fail-closed 合同冲突。当前仍然成立：

- `human_validated=false`；
- `public_claim_eligible=false`；
- `release_eligible=false`；
- 真人聚合器仍为 `BLOCKED_HUMAN_DATA`，真实行程 0/30、真实组织者 0/15～20；
- 已授权公网 HTTPS 双入口 E2E 尚未执行。

当前可对外准确表述为：

> BreezeTravel 已完成北京、上海、杭州真实高德/天气 Provider 的本地授权样本与性能验证，并完成两个隔离浏览器账号在 Backend/Yjs 重启后的本地全栈恢复 E2E；三名 GPT-5.6-sol 子 Agent 的 150 条合成代理校准通过 M1-dev 门禁。该代理校准不是真人研究，最终真人校准与公网发布证据仍未完成。
