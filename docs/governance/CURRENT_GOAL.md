# COMPLETED GOAL：局域网 HTTP / 旧 WebView UUID 兼容修复

## Metadata

- Goal ID：`TC-DEVICE-UUID-HTTP-HOTFIX`
- Program ID：`TC-INTAKE-V2-2026`
- Status：`COMPLETED`
- Branch：`codex/trip-intake-deepseek-stability`
- Baseline：`1cd7bb909b22add6315081945a1951d53eae5184`
- Approved by / at：User / 2026-08-27

## Outcome

修复行程查在内置浏览器、局域网 HTTP 和旧 WebView 中因 `crypto.randomUUID` 不可用而无法生成幂等键或命令 ID 的问题，并在用户当前打开的 Intake 页面完成真实交互回读。

## Scope

- 新增统一的浏览器 UUID 兼容函数；
- 替换 Trip Intake、Import、Workspace、Suggestion、Template、Share 与成员确认中的直接 `crypto.randomUUID()` 调用；
- 重建局域网设备测试前端并在内置浏览器验证请求已越过 UUID 生成阶段。

## Non-goals

- 不改变公共 API、数据库 migration、Trip Intake 抽取逻辑、模型、Provider 或发布门槛；
- 不把内置浏览器测试升级为 H1、真人真机、生产或公网证据；
- 不改写 O5/O6 frozen blind 的 `REJECT` 结论。

## Invariants

- 优先使用原生 `crypto.randomUUID`；
- 缺少 `randomUUID` 时使用 `crypto.getRandomValues` 生成 RFC 4122 v4 格式；
- 极旧 WebView 连 `crypto` 都不可用时，仅为非安全用途的命令/幂等标识提供带时间与计数器熵的 UUID 格式兜底；
- 不使用该 UUID 作为认证、授权、分享 token 或其他安全凭证。

## Verification

- 前端 production build：`PASS`；
- 直接 `crypto.randomUUID()` 产品调用：0；
- 内置浏览器原始复现：`crypto.randomUUID is not a function`；
- 修复后同页面确认请求成功生成幂等键并到达后端，页面不再出现 crypto 错误；后端因该旧草稿缺少完整日期返回领域校验错误，属于独立且预期的输入状态；
- 局域网服务保持运行，前端 `http://10.23.154.6:13000`、后端健康检查与 WebSocket 均可访问。

## Completion record

- Implementation / Gate：`COMPLETED`；
- Goal contract：`structurally_valid=true`；
- `INTAKE_V2_DEVELOPMENT_READY=false`：本兼容修复不得覆盖 O5/O6 frozen blind `REJECT`；
- 本 Goal 不得改写或替代发布门禁，不得因此宣称 `V1_CANDIDATE_READY`。
