# 公网 Demo 运行手册

> `LEGACY_RUNBOOK / NOT_VNEXT_AUTHORITY / DO_NOT_EXECUTE_FOR_VNEXT`：这是旧room/RAG演示手册，不是Blueprint/V0.x发布步骤。任何公网、部署、真人或production动作仍需人工批准和新Runbook；下列历史命令不得作为新版完成证据。

当前仓库提供部署配置与健康检查。虽然 `breezetravel.cn` 可返回前端页面，但 2026-07-29 的核验中其 `/api/health` 与 `/api/evidence/latest` 均为 404，故**尚不能声明公网全栈 Demo 已上线**。只有以下 smoke 通过后，才能更新 README 的上线表述。

## 环境分层

| 环境 | 目的 | 必须配置 |
|---|---|---|
| local-demo | 零成本演示 | `DEMO_MODE=true`、`AMAP_MOCK=true`、`DEV_LOGIN_BYPASS=true` |
| evaluation | 可复现实验 | 真实公开资料、独立数据库、评测密钥，不对公网开放 |
| public-demo | 对外试玩 | `PUBLIC_DEMO_MODE=true`、严格 CORS、Secret、公开资料或明确标示的演示语料 |

## 发布检查

1. 将 `JWT_SECRET_KEY`、LLM/Embedding/地图密钥仅配置在平台 Secret；仓库和前端不得包含服务端密钥。
2. 设置 `CORS_ORIGIN_REGEX` 为实际 Vercel 域名；不要使用宽泛 `.*`。
3. 公网环境设置 `PUBLIC_DEMO_MODE=true`。该单实例保护默认每 IP 每分钟 12 次聊天请求；多实例部署必须使用平台 WAF 或 Redis 限流。
4. 发布后执行：`GET /health`、`GET /api/evidence/latest`，再在浏览器完成登录/入房/发送问题/看到引用/排线五步冒烟。
5. 保留前一个平台版本或镜像 Tag；回滚使用平台版本回退，不依赖服务器 `git reset --hard`。

## 录屏脚本（3 分钟）

1. 说明旅行小组协同规划的实际痛点（15 秒）。
2. 用一条有公开来源的问题展示 SSE 工具步骤、回答依据和 Trace ID（60 秒）。
3. 第二个浏览器标签投票/备注，展示 Yjs 合并（30 秒）。
4. 生成排线并展示约束/备选池（35 秒）。
5. 打开 `/metrics` 与 `/api/evidence/latest`，说明指标、失败降级与仍未上线的能力（40 秒）。
