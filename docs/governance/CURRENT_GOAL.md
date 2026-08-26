# IN_PROGRESS GOAL：MP1-G01 微信小程序纵向闭环

## Metadata

- Goal ID：`TC-MP1-G01-miniapp-vertical-slice`
- Program ID：`TC-MULTI-FRONTEND-2026`
- Phase：`MP1`
- Status：`IN_PROGRESS`
- Branch：`codex/mobile-app`
- Baseline commit：`d51d78fd004d46b105f05134c61d5fbee385c974`
- Approved by / at：User / 2026-08-26
- Required gate：`Miniapp Automated Gate`

## Outcome

在不改变「行程查」权威主链的前提下，新增可编译的微信小程序前端。小程序与 Next.js Web 共用 FastAPI/PostgreSQL 状态、revision、Evidence、Audit、Advice 和 postcheck，并通过共享 OpenAPI 合同防止双端漂移。

```text
微信登录 → 文本/截图输入 → TripBrief 确认 → 地点消歧
→ 事实核验 → Advice/Repair → 新 Revision → 完整 postcheck
```

## Scope

- 新增 Taro 4.2.1、React 18.3.1、TypeScript、Webpack 5 小程序工程；
- 新增平台无关 Trip Check 类型、错误和幂等命令客户端；
- 新增微信 code2Session 登录，微信身份创建独立 `user_id`；
- 新增截图上传批次、单图上传、提交和取消 API；
- 新增追加式 migration `025_miniapp_identity_and_upload_batches.sql`；
- 保留旧 Web multipart、SSE、手机号和邮箱登录兼容；
- 交付类型检查、单测、OpenAPI 门禁和 `build:weapp` 产物。

## Non-goals

- 不扩城、不跨城，不改变 2～5 人、2～5 天范围；
- 不移植 Builder、Chat、Yjs、拖拽地图、模板、Planner、RAG 或 LoRA；
- 不增加微信与手机号/邮箱账号合并或绑定；
- 不新增消息队列、对象存储、Kubernetes、Provider 或付费调用；
- 不安装或操作微信开发者工具，不做真机、上传、发布或公网部署；
- 不更新 P6 candidate evidence，不复用旧 manifest 声明新 commit 已发布；
- 不合并 `main`，不创建 release，不进入 H1。

## Authority and approved contract changes

权威顺序继承 `AGENTS.md`。用户已明确批准本 Goal 的完整小程序主链、仅微信登录、独立微信账号，以及微信登录和截图批次两项公共 API/migration 扩展。

- `POST /api/auth/wechat/login`；
- `POST /api/trip-workspaces/{workspace_id}/screenshot-upload-batches`；
- `POST /api/trip-workspaces/{workspace_id}/screenshot-upload-batches/{batch_id}/files/{position}`；
- `POST /api/trip-workspaces/{workspace_id}/screenshot-upload-batches/{batch_id}/commit`；
- `DELETE /api/trip-workspaces/{workspace_id}/screenshot-upload-batches/{batch_id}`；
- migration `025_miniapp_identity_and_upload_batches.sql`。

除上述合同外，任何新增公共 API、migration、生产依赖、基础设施、账号绑定、部署或真实微信凭据使用均停止并请求批准。

## Invariants

- `TripWorkspace → ItineraryRevision → EvidenceSnapshot → AuditEngine → RepairOption/EditCommand` 仍是唯一权威主干；
- 客户端只持久化 token、未完成命令和资源标识，业务状态从 `/resume` 回读；
- 所有语义 mutation 保留 `If-Match`、`Idempotency-Key`、新 revision 与完整 postcheck；
- 微信 code、openid、session_key 和 AppSecret 不进入数据库、日志、Git 或响应；数据库只保存 `HMAC(openid)`；
- 截图 1～6 张、每张不超过 10MB；原图成功、失败、取消或过期后删除；清理失败保持 `PRIVACY_BLOCKED`；
- `UNKNOWN/UNAVAILABLE` 不计 PASS，局部失败不抹掉成功事实；
- Web 与小程序只共享合同和应用语义，不共享 DOM/UI 组件。

## Acceptance and verification

- 微信认证：配置缺失、无效 code、Provider 超时、重复/并发登录和敏感信息留存测试全部通过；
- 截图批次：缺图、重复位置、冲突、大小/格式、提交、取消、过期、OCR/清理失败和旧 multipart 兼容测试全部通过；
- migration 025 在全新和已有 PostgreSQL 上执行并回读；
- OpenAPI 生成检查无漂移；共享客户端类型、错误与幂等测试通过；
- 小程序登录、恢复、文本/截图、Brief、消歧、轮询、PARTIAL、Advice/Repair/postcheck 单测通过；
- backend pytest、Ruff、Web build、dual-entry、小程序 typecheck/test/build 全部实际运行；
- 微信开发者工具、真实 AppID/AppSecret、真机和发布固定为 `NOT_RUN`。

## Budget, checkpoints and stop conditions

- 增量费用：0；不调用真实微信、Provider 或公网部署；
- 最长 60 分钟形成一次可恢复的本地与远端 checkpoint；
- 每个切片执行：定向验证 → diff → 显式暂存 → staged diff/check → commit → push；
- 当前未跟踪 `tests/` 属于用户，不读取、不修改、不暂存、不提交；
- 需要扩大公共合同、无法保持旧 Web 兼容、发现隐私/secret 泄漏、连续两个切片无法改善同一门禁或需要真实凭据时停止。

## Completion rule

只有 `Miniapp Automated Gate=PASS`、工作树除用户 `tests/` 外干净、所有 commit 已推送且 upstream 可确认时，才可归档本 Goal。完成不代表开发者工具预览、真机验证、公网部署、生产发布或 P6 evidence 晋级。
