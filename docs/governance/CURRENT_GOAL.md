# COMPLETED GOAL：Trip Intake 确认恢复与完整本地 E2E

## Metadata

- Goal ID：`TC-INTAKE-CONFIRM-E2E-HOTFIX`
- Program ID：`TC-INTAKE-V2-2026`
- Status：`COMPLETED`
- Branch：`codex/trip-intake-deepseek-stability`
- Baseline：`d1292d71e635b0ab36323d168e0ddc054aa00ea5`
- Approved by / at：User / 2026-08-27

## Outcome

修复不完整 Trip Intake 点击确认时泄漏 Pydantic 校验错误的问题；允许用户在同一确认动作中保存已填写的城市、日期、人数并完成确认。随后使用测试数据完成真实 DeepSeek、冻结 Provider、PostgreSQL 的完整本地 E2E，并在内置浏览器回读用户可见主链。

## Scope

- Trip Intake 确认交互与服务端领域错误转换；
- 定向 regression、前端 production build；
- 北京、上海、杭州三城真实 DeepSeek 本地 API E2E；
- schema-invalid、timeout fallback、Provider 局部失败、幂等、SSE 恢复与完整 postcheck；
- 内置浏览器测试数据主链与刷新回读。

## Non-goals

- 不改变公共 API、数据库 migration、模型、Provider 数据范围或默认生产行为；
- 不修改 frozen blind/oracle，不重试或改写 O5/O6 blind `REJECT`；
- 不部署公网，不进入 H1、生产或 release。

## Invariants

- READY 只能在城市、正整数人数和完整日期均已形成证据后产生；
- 用户在表单填写的值必须先写入新 Intake revision，再确认该 revision；
- 对外错误不得泄漏 Pydantic 内部结构；
- 正常 E2E 必须回读 `deepseek-v4-flash`、`fallback=0`、新 itinerary revision 与完整 postcheck；
- `UNKNOWN`、Provider 局部失败和 fallback 不得伪装为正常主链成功。

## Verification

- 定向 Trip Intake tests 与前端 build；
- 三城正常链 `3/3 PASS`，无意外 5xx；
- refresh、idempotency、SSE reconnect、schema-invalid/timeout fallback、Provider partial failure 全部 PASS；
- 内置浏览器从测试 Intake 至权威 workspace/postcheck 完成可见回读。

## Completion record

- Status：`COMPLETED`；
- Product subject commit：`b53d5e638611f0df3d24bb0576f56ac0c5267e6a`；
- 三城 API E2E：北京、上海、杭州 `3/3 PASS`，真实模型 `deepseek-v4-flash`，正常链 `fallback=0`，共 82 个 HTTP 步骤、意外 5xx 为 0，receipt SHA-256 为 `afdda84d98985aa6e1fa23be51fb9ef77abf92d3f5eb7d21a430f5b126fdd678`；
- 内置浏览器 E2E：测试数据完成 Intake、确认、materialize、地点确认、Audit、Advice、采纳、新 revision 与完整 postcheck；最终权威状态为 `SUCCEEDED/POSTCHECK`，revision 从 1 变为 2，未出现 Pydantic 或 `crypto.randomUUID` 错误；
- 确认一致性复核：既存 revision 5 的完整日期范围与 `temporal.days=UNKNOWN` 冲突已由确认动作安全归一；浏览器回读 revision 6 为 `READY`、`EXACT 4 天`、阻断项 0，并已成功 materialize；
- 工程复核：backend `2020 passed, 32 skipped`，Ruff PASS，frontend production build PASS，candidate 绑定的原 120 条 validator PASS，remediation validator PASS；
- Goal contract：`structurally_valid=true`；
- `INTAKE_V2_DEVELOPMENT_READY=false`：本次热修复不得覆盖 frozen blind `REJECT`；
- 本 Goal 不得改写发布门禁，不得因此宣称 `V1_CANDIDATE_READY`。
