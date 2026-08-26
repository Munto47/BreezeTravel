# BreezeTravel 双入口：完整本地测试、样本与端到端体验（2026-08-20）

## 1. 结论

当前工作树可以称为 **“双入口本地代理验收 + 本地授权真实 Provider + 双用户重启 E2E candidate”**：代码、受控 PostgreSQL、双浏览器、Backend/Yjs 重启恢复、固定样本评测、故障注入和证据哈希均通过；真实页面上的模板入口也完成了 `DRAFT → workspace revision → 编辑 → 审计 → 临行复检 → 刷新恢复`。

不能称为“受控三城 Beta 已发布”或“生产可用”。尚缺的证据是：

1. 已授权 HTTPS 公网环境上的双入口重复验证；
2. 30 份真实行程、15～20 名真实组织者的最终真人校准与发布证据。

本轮新增通过项：真实高德/天气 Provider 共 27/27 次成功，并以应用实际适配器补跑实体、路线、天气各 1 次；两个隔离 Chromium 账号完成同 revision 并发冲突，并在 Backend/Yjs 真实重启后逐字段恢复同一权威状态。三名 `gpt-5.6-sol` 的 150 条结果只属于 `synthetic_proxy`，没有写入真人字段。

本轮还发现一个符合该边界的真实运行结果：本地 Docker 的 `AMAP_MOCK=true`，但新文本导入的实体 Provider 未启用；创建 workspace 后，`POST /api/trip-workspaces/{id}/imports` 返回 `503 EVIDENCE_PROVIDER_UNAVAILABLE`。系统没有伪造候选，但文本导入口无法在当前运行配置下完成闭环。

## 2. 本轮执行基线

- 分支：`codex/dual-entry-itinerary-refactor`
- HEAD：`2238c5304e3c17fae8162cbfca345d2fbdf5f076`
- 工作树：包含尚未提交的双入口重构；本轮以精确工作树为测试对象，不把 `main` 的旧结果当作当前结果。
- Provider 凭据：默认 CLI 自动门禁继续清空；本轮另以显式授权的 `local_real / AMAP_MOCK=false / DEMO_MODE=false` 隔离进程执行真实高德 REST 与真实和风天气，未调用 DeepSeek/OpenAI。
- 浏览器地图：真实页面加载了高德 JS SDK/地图瓦片；这只证明地图前端资源可达，不是实体搜索、路线或天气 Evidence Provider 样本。

## 3. 实际执行结果

| 层级 | 命令/入口 | 样本量 | 本轮结果 | 能证明什么 | 不能证明什么 |
|---|---|---:|---|---|---|
| 后端默认门禁 | `cd backend; python -m pytest tests -q` | 902 collected | `878 passed, 24 skipped, 38 warnings` | 非外部逻辑和受控应用合同 | 24 个跳过项 |
| 受控服务集成 | `RUN_SERVICE_INTEGRATION=1 python -m pytest -m integration -q` | 11 | `11 passed` | PostgreSQL/Redis、迁移、并发、重启式 repository 回读 | 真实公网或 Provider |
| Python 静态检查 | `python -m ruff check app evals scripts tests` | 全目录 | 通过 | 静态规则 | 运行时业务质量 |
| 前端构建 | `npm run build` | 12 条页面路由 | 通过 | Next.js 编译、类型、生产构建 | 浏览器业务闭环 |
| 受控浏览器 | `playwright.local.config.js` | 2 | `2 passed` | 缓存报告失效、模板入口与幂等键 | 真实后端/Provider |
| 冲突恢复浏览器 | `playwright.workspace.config.js` | 2 | `2 passed` | revision 409 回滚、陈旧报告拒绝、显式权威重载 | 两名真实用户并发 |
| 浏览器 + 真后端/Yjs | `playwright.persistence.config.js` | 1 | `1 passed` | 一对消息在浏览器刷新后只恢复一次 | 服务进程重启、真实 LLM |
| Backend/Yjs 九场景重启全栈 E2E | `run-dual-user-restart-e2e.ps1` | 9 个独立 case/1 次换代 | `1 passed (48.3s)` | 三城、9 room/workspace/seed、Backend/Yjs 真换代、HTTP/Yjs/browser 精确恢复、9/9 清理 | 公网、live Provider 或真人行为 |
| 真实 Provider | `verify_real_providers.py --iterations 3 --strict` | 27 | `27/27` | 三城真实实体、路线、天气与 P50/P95 | 公网产品 E2E、长期 SLO |
| 新链路适配器 smoke | `verify_provider_adapters.py --strict` | 3 | `3/3` | 实际实体/路线/Planner 天气适配器走 live Provider | 多样本性能分布 |
| Yjs 进程测试 | `cd y-websocket; npm test` | 9 | `9 passed` | JWT 房间隔离、过期/缺失拒绝、真实 Node 子进程重启恢复、九文档批量 cleanup 边界 | 双浏览器完整产品状态 |
| 固定离线评测 | `python -m scripts.run_local_eval_suite` | 348 | 12 个分片均为 1.00 | Router、Task Parse、Verifier、E2E 的固定集回归 | 实时 Provider/真人泛化 |
| 故障注入 | `python -m scripts.run_fault_injection` | 24 | `24/24` | timeout、429、503、无效 JSON、Yjs restart 等降级合同 | 真实故障频率与公网 SLO |
| 本地消融 | `python -m scripts.run_local_experiments` | 10 variants | 完成 | 离线检索代理、路由、Verifier/Repair 对比 | 外部 LLM/真实 Provider 性能 |
| Compose 与 diff | 两套 `docker compose ... config --quiet`、`git diff --check` | 3 | 通过 | 配置可解析、无 whitespace error | 容器已部署或公网可用 |
| 证据绑定 | `python backend/scripts/verify_dual_entry_delivery.py` | 1 manifest | `LOCAL_DELIVERY_EVIDENCE_VALID` | migration 017、文档与 M1-dev gate 哈希一致 | 发布批准、真人或公网验证 |
| 真人聚合器 | `python backend/scripts/evaluate_auditor_human.py` | 0 real cases | `BLOCKED_HUMAN_DATA` | 真人门禁 fail closed | 任何真人质量结论 |

### 3.1 24 个默认跳过项的拆分

- 11 个 `integration`：本轮已显式启用并全部通过；
- 12 个外部项：8 个 LoRA/GPU、3 个完整 Agent 外部评测、1 个 RAGAS API+语料库集成；本轮没有执行；
- 1 个 Router 单例隔离项：当前进程已加载全局单例，测试按设计跳过。

因此当前可写成：**默认门禁 878 passed；另有 11 个受控服务集成项单独通过；12 个外部模型/GPU/RAGAS 测试仍未执行。** 不能把两次数字简单相加成“889 条完全通过且无跳过”。

### 3.2 固定离线评测的完整分片

| kind | pilot | dev | blind | 合计 |
|---|---:|---:|---:|---:|
| router | 16 / 1.00 | 32 / 1.00 | 48 / 1.00 | 96 |
| task_parse | 12 / 1.00 | 24 / 1.00 | 36 / 1.00 | 72 |
| verifier | 20 / 1.00 | 40 / 1.00 | 60 / 1.00 | 120 |
| end_to_end | 10 / 1.00 | 20 / 1.00 | 30 / 1.00 | 60 |
| **合计** | **58** | **116** | **174** | **348** |

这些分片使用固定本地数据，输出继续标记 `production_claim_not_made=true`。

### 3.3 合成审计样本与代理校准

- 北京、上海、杭州各 50 条，共 150 条；
- 60 条 `SIMULATED_AI_ITINERARY`；
- 60 条 `SIMULATED_CONTROLLED_MUTATION`；
- 30 条 `SIMULATED_BOUNDARY`；
- 三名独立 `gpt-5.6-sol` synthetic proxy 角色完成；
- 本轮新一轮盲评的关键 precision=1.00、recall=1.00、Evidence 回读率=1.00、三者关键一致率=1.00；
- 该 gate 为 `M1_DEV_PROXY_PASSED`，但 `human_validated=false`、`public_claim_eligible=false`。

### 3.4 真实 Provider 与双用户重启补证

真实 Provider：实体 9/9、路线 9/9、天气 9/9；P50/P95 分别为 189.35/215.26ms、66.69/79.40ms、103.24/187.92ms。应用适配器 smoke 又通过实体、路线、天气各 1 次，并修复 planner 天气节点不兼容 JWT/custom-host 的问题。

双用户重启：两个隔离 context 从同一 revision 1 并发编辑得到一个 200、一个 409；仅生成 revision 2。关闭浏览器后重启 Backend/Yjs，PostgreSQL 未重启、Yjs named volume 不变；全新 Yjs client 与两个新 context 回读的 revision、content hash、report 和 member constraint revision 全部一致，旧失败编辑没有重放。

## 4. 完整代表样本

完整 150 条数据位于 `backend/eval_data/auditor_simulated/cases.jsonl`。下面给出同一 source document 的原始/变异对，能够完整说明系统如何区分“原文事实”“受控注入错误”和“缺失外部事实”。

### 4.1 原始样本 `sim-bj-original-01`

```text
同行：2人，目的地北京，偏好公共交通，预算适中。
第1天：10:00-12:00 故宫博物院；12:15-13:15 北京模拟甲餐厅；14:00-16:00 景山公园；18:30-19:30 北京模拟乙餐厅；21:00-22:00 北京模拟甲酒店。
第2天：09:00-11:00 南锣鼓巷；12:15-13:15 北京模拟丙餐厅；14:00-16:00 颐和园；18:30-19:30 北京模拟丁餐厅；21:00-22:00 北京模拟乙酒店。
```

预期：没有受控错误；因为离线样本不提供营业时间和天气，输出 `OPENING_HOURS_MISSING=UNKNOWN`、`WEATHER_DATA_MISSING=UNKNOWN`，不得写成通过或违反。

### 4.2 变异样本 `sim-bj-mutation-01`

```text
同行：2人，目的地北京，偏好公共交通，预算适中。
第1天：10:00-12:00 故宫博物院；12:15-13:15 北京模拟甲餐厅；10:30-12:30 景山公园；18:30-19:30 北京模拟乙餐厅；21:00-22:00 北京模拟甲酒店。
第2天：09:00-11:00 南锣鼓巷；12:15-13:15 北京模拟丙餐厅；14:00-16:00 颐和园；18:30-19:30 北京模拟丁餐厅；21:00-22:00 北京模拟乙酒店。
```

预期：与原始样本比较，第一天新增时间重叠；必须捕获 `TIME_CHAIN_BROKEN=VIOLATED/HIGH`。营业时间、天气仍保持 `UNKNOWN`。Repair 只有在 postcheck 移除目标违反且不造成 HIGH/UNKNOWN 非回归时才可接受。

### 4.3 边界样本 `sim-bj-boundary-01`

```text
说明：仅有模糊需求，地点和时间都未提供。
```

预期：`IMPORT_PARSE_FAILED=UNKNOWN/HIGH`，停止实体猜测，不生成伪 POI，并请求用户补充。

### 4.4 三城推荐评测样本 `bj_01_first_time_landmarks`

```json
{
  "city": "北京",
  "intent": "attraction",
  "persona": "第一次来北京的独行游客",
  "query": "第一次去北京，只有一天，想看最有代表性的历史地标，别给我一串同质化公园。",
  "expected": {
    "min_places": 3,
    "semantic_requirement": "优先故宫、天安门周边、天坛或景山等代表性历史地标，组合应有辨识度，避免用普通公园凑数。"
  }
}
```

完整推荐集位于 `backend/eval_data/daily_queries/cases.json`，北京/上海/杭州各 50 条。

## 5. 本轮真实本地端到端体验

### 5.1 文本导入口：诚实失败

输入：

```text
Day 1 北京
09:00-11:30 故宫（已预约，不可移动）
11:00-12:00 景山公园
12:00-13:00 王府井午餐
Day 2 北京
09:00-11:00 颐和园
10:30-12:00 圆明园
18:00-22:00 南锣鼓巷
Day 3 北京
09:00-11:00 天坛
10:30-12:00 前门大街
```

实际事件链：

1. 测试账号登录成功；
2. 创建北京 3 天房间成功；
3. 进入 `/import`，创建 workspace 成功，HTTP 201；
4. 创建 import 时实体 Evidence Provider 不可用，HTTP 503；
5. UI 显示 `Amap entity search is unavailable`；没有生成或伪造 POI 候选。

结果：**该次 Compose 文本导入的降级合同通过，但业务闭环未通过。** 随后隔离的真实 Provider 与实际应用适配器 smoke 已通过；Compose 的自动化重启 E2E 仍刻意保持 `AMAP_MOCK=true`，所以不能把独立 Provider 证据倒灌成这次 import 请求成功。

### 5.2 模板入口：完整本地闭环

实际事件链：

1. 打开北京模板页；页面明确标注 `GPT-5.6-sol 生成的合成 DRAFT`，不是已核验 POI/酒店/真人审核路线；
2. 应用“北京亲子与室内路线”，创建 workspace 并写入 revision 1；
3. 工作台显示 3 天，前两天各 2 个合成锚点，第三天空；
4. 将 Day 1 第一个地点下移，服务端推进到 revision 2；
5. 增量检查显示：
   - `TIME_CHAIN_BROKEN`；
   - `PLACE_NOT_RESOLVED`；
   - 缺餐饮、缺酒店；
   - 新路线边证据不可用，不估算通勤变化；
6. 执行最终完整审计，报告绑定 revision 2，状态 `VIOLATED`；
7. 执行临行复检，显示时间窗口“尚早”，Provider 收据为 `amap / stored_fallback / not_attempted / live_recheck_disabled`；
8. 浏览器强制刷新后，revision 2、调换后的顺序、完整审计和临行复检差异均从服务端恢复。

结果：**模板入口的本地 PostgreSQL 权威状态闭环通过。** 它证明 append-only revision、审计绑定、复检追加和刷新恢复；不证明真实 POI、真实路线、实时天气、双用户或公网。

## 6. 外部测试矩阵与当前状态

### EXT-PROVIDER-01：真实 Evidence Provider 样本（已通过本地授权验证）

环境要求：单独授权的测试 Key/配额；`AMAP_MOCK=false`；保存 request hash、observed_at、latency、status、result_count 和降级原因，不保存密钥。

固定样本：

| 城市 | 实体精确样本 | 顺序/路线边样本 | 天气样本 |
|---|---|---|---|
| 北京 | 故宫博物院、景山公园、颐和园 | 故宫博物院 → 景山公园 | 行程首日逐日天气 |
| 上海 | 上海博物馆、外滩、豫园 | 上海博物馆 → 外滩 | 行程首日逐日天气 |
| 杭州 | 西湖、灵隐寺、杭州东站 | 灵隐寺 → 西湖 | 行程首日逐日天气 |

本轮固定成功样本为 27/27，应用适配器 smoke 为 3/3；实体、路线、天气 P50/P95 和每条脱敏收据均已落盘。歧义名、不存在地点及 timeout/429/5xx 继续由已有受控故障注入覆盖，不把受控故障称为本轮真实 Provider 的自然故障率。

### EXT-DUAL-RESTART-01：双用户 + 服务重启（已通过本地自动化验证）

1. 两个独立浏览器 context 使用不同账号进入同一 room/workspace；
2. A 写入自己的 confirmed HARD 约束，B 只能写自己的约束；
3. A/B 基于同一 revision 同时编辑，必须恰好一方成功，另一方得到稳定 409 并回滚乐观预览；
4. 成功方执行完整审计，另一方看到同一 revision/report 引用；
5. 依次重启 Backend 与 Yjs，不删除 PostgreSQL/Yjs volume；
6. 两端重新连接并刷新；
7. 两端必须看到相同 current revision、report、Evidence、成员约束和 ACK；失败方的旧编辑不得重放；Yjs 只恢复协同引用，不覆盖 PostgreSQL 权威状态。

原单场景已扩为九场景矩阵。验收结果：9/9 case 的 revision/content hash/audit/member/map/event ledger 与 Yjs places/events 均精确恢复；所有 fresh Yjs read 均先于恢复浏览器创建，PostgreSQL/Yjs 分别完成 9/9 清理。Codex 主流程复跑 `1 passed (48.3s)`。

### EXT-PUBLIC-01：已授权公网 HTTPS 双入口 E2E

1. `/health`、`/metrics`、`/api/evidence/latest` 均为 HTTPS 200；
2. 邮箱注册/登录、房间创建和 JWT Yjs WebSocket 成功；
3. 文本导入与模板入口各重复 3 次；
4. 导入、消歧、revision、编辑、审计、Repair、成员、分享、复检均可重复；
5. 检查无 mixed content、CORS 错误、Token 泄露和跨 workspace IDOR；
6. 公网服务滚动重启后回读同一状态；
7. 使用 E2E cleanup 删除专用测试账号/房间，不触碰真人数据。

验收：业务链全部通过才可写 `publicly_verified=true`；只有首页 200 或地图加载成功不算公网全栈通过。

### EXT-HUMAN-01：最终真人发布证据

- 至少 30 份唯一真实原始行程；
- 15～20 名唯一真实组织者；
- consent、HMAC 假名、真人 finding、独立 `critical_human_check`、Repair 接受/拒绝及原因齐全；
- 字段 F1 ≥ 0.90；
- POI 自动匹配 precision ≥ 0.95；
- 固定承诺 recall ≥ 0.95；
- 静默错配 = 0；
- BLOCKER/HIGH precision ≥ 0.90、recall ≥ 0.85；
- 关键 finding 人工核对准确率 ≥ 0.85；
- 关键 Evidence 回读率 = 1.00；
- 审计耗时 P80 ≤ 180 秒。

Repair 采纳率 40% 只是产品目标，不是篡改事实门禁的理由。当前实际聚合器输出为：`BLOCKED_HUMAN_DATA`、`0/30` 行程、`0/15～20` 组织者。

## 7. 最终允许表述

> BreezeTravel 已完成双入口可验证行程的本地代理验收、北京/上海/杭州真实高德与天气 Provider 的本地授权验证，以及两个隔离浏览器账号在 Backend/Yjs 重启后的全栈恢复 E2E。三名 GPT-5.6-sol 子 Agent 的 150 条校准属于 synthetic proxy，不是真人研究；公网 HTTPS 与最终真人校准仍是独立发布门禁，因此当前不作真人效果、商业验证或发布批准声明。

## 8. 后续本地导入闭环优化（同日）

第 5.1 节记录的是当时 Compose 处于 `local_real + AMAP_MOCK=true` 时的诚实失败。后续为受控本地开发新增了独立的 `local_fixture` profile：它只允许确定性 fixture，不与 `local_real`、真实 Provider 或公网混同。

- 文本候选现在携带 `amap_fixture / fixture` 溯源，UI 明确显示“本地 fixture 候选”，不再写成实时 POI 核验；
- 修复 `Day 1 北京` / `第 1 天：杭州` 被误解析为地点的问题；
- 北京 fixture 补入“景山公园”，使本文第 5.1 的代表文本可走 `解析 → 自动匹配 → revision 1 → 完整审计`；
- 实际 Docker 浏览器复测：3 个真实文本 stop 均自动匹配并显示 fixture 标识，revision 1 已落库；完整审计仍如实返回 `VIOLATED`（酒店、餐饮、营业时间和天气不足），没有把 fixture 当作已验证事实。

回归：导入/解析/fixture/检索针对性测试 `49 passed`；当前后端全量 `881 passed, 24 skipped`；受控前端 Playwright `3 passed`（新增 fixture 标识与 Day-header 防回归用例）；前端生产构建通过。

这使文本入口的**受控 fixture 业务闭环**通过；它仍不替代第 6 节的真实 Provider、公网 HTTPS 或真人发布门禁。
