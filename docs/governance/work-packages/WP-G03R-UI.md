# Work Package Prompt v1

本包只修复G03结果页的用户可见稳定性与主要交互。主对话是唯一集成者；本包不得修改Goal、binding、registry或其他治理状态，不得自行合并。任何产品写入必须发生在本提示词及其SHA-256已由binding commit登记、且独立分支/工作树从该binding commit创建之后。

## Identity and exact baseline

```yaml
prompt_schema_version: work-package-prompt-v1
package_id: WP-G03R-UI
goal_id: TC-VNEXT-G03-TOP3-AUDIT
role: CONTRIBUTOR
origin_develop_baseline: 8a33a4b22a405135f310376d8766d9170d80097d
integration_checkpoint_commit: 74264ec16d27f020201dca5e59ab14023bfd8632
registry_activation_commit: 74264ec16d27f020201dca5e59ab14023bfd8632
```

产品差异仍受当前Goal的`origin/develop`基线约束；本包功能分支必须从包含本提示词及其SHA-256登记的binding commit创建。写入前必须证明HEAD包含该binding commit，且工作树、分支和远端基线一致。

## Branch and isolated worktree

```yaml
branch: codex/g03r-ui
remote_branch: origin/codex/g03r-ui
worktree_path: D:/munto/code/claudeProject/agentTravel-g03r-ui
dialogue_ref: codex-task:01a050ae-0939-7f61-bc80-0ffd04480412
```

不得复用根工作树、控制面工作树、语义工作树或地点工作树。贡献包只commit/push并回传精确tip；不修改治理文件、不cherry-pick其他分支、不自行合并。

## Owned paths: deny by default

只有以下路径可写；未列入的路径一律禁止：

- `frontend/src/app/trip/result/page.tsx`
- `frontend/src/app/trip/result/itinerary-workspace.tsx`（新）
- `frontend/src/lib/trip-understanding-v3.ts`
- `frontend/e2e/g03r-result-ui.spec.js`（新）

## Explicit forbidden paths

- `AGENTS.md`、`CLAUDE.md`
- `docs/**`
- `backend/**`
- `packages/**`、`miniapp/**`、`y-websocket/**`
- `frontend/package.json`、`frontend/package-lock.json`、`frontend/playwright.config.js`、`frontend/playwright.product-delivery.config.js`
- `frontend/e2e/g03-product-delivery.spec.js`、`frontend/e2e/trip-understanding-v3.spec.js`
- `frontend/src/app/trip/result/activity-image.tsx`
- 所有lock文件、依赖与配置文件
- 所有未列入`owned_paths`的路径

地点图片、Provider URL、图片代理、Redis二进制缓存、公共图片端点、OpenAPI、migration和`033_trip_understanding_activity_media.sql`均不属于本包；它们保留给后续单独授权的UI-MEDIA切片。

## Confirmed blocking reproduction

在精确后端集成候选上，G03浏览器旅程首次运行时：

- 后端`materialize`与`checks`均为HTTP 200，trace中的checks响应包含3条可预览建议；
- DOM仍为0个`trip-check-item`，页面永久显示“正在准备最值得处理的三项…”；
- 结果页effect在异步完成前被cleanup置为disposed，但`checksAttemptedKey`已提前固定；同一resource/etag后续执行被拦截，成功响应因此无法进入UI，busy状态也可能不释放。

另一次Windows Proactor worker运行发生30秒租约接管，已通过CI同类Selector事件循环隔离为本地运行环境差异；不得用修改前端等待时长掩盖worker问题。本包只修复已由trace证明的前端状态竞态与结果页主要交互。

## User-observable outcome

- 卡片、地图、住宿或ETag在加载期间发生合法状态切换时，Top-3检查不会永久停留在准备态；过期响应不覆盖新状态，当前状态最终必能重试、成功显示或给出可恢复提示。
- 用户可以在桌面用明确拖拽手柄进行同日和跨既有日期移动，并看到可见落点；空日仍保留，不创建新日期。
- 键盘和移动端提供上移、下移、移到其他天及目标位置，不依赖拖拽才能完成同一任务。
- 删除使用可访问确认对话框；添加地点、地点详情/编辑/替换、地图、住宿、Top-3、隐私删除等现有真实功能全部保留。
- 页面只展示普通用户可理解的卡片、路线、住宿和建议，不出现内部ID、revision、receipt、Provider、证据阶段或错误堆栈。

## Required interaction contract

### Stable enhancement/check state

- 以当前`resourceRef + etag`为请求代际；请求开始、成功、失败、取消和被新代际替换必须形成可验证的有限状态，不允许cleanup后留下永久busy或永久attempted。
- 旧代际响应必须忽略；当前代际若因合法依赖变化被取消，必须允许重新执行。一次有效代际最多产生一次并发`materialize`，不得靠循环请求或延长超时获得通过。
- 住宿选择、卡片编辑、Top-3采纳及409冲突后的ETag更新必须清空或迁移正确的派生状态，并以服务端最新结果回读为准。
- 失败时显示用户友好、可恢复的提示；不得显示内部错误，也不得把后端已成功返回的3条建议丢弃。

### Itinerary movement and commands

- 桌面支持同日/跨日原生拖拽，必须有明确手柄、可见落点与键盘替代操作；48px触控目标，尊重`prefers-reduced-motion`。
- 计算移动目标时先从源日移除源卡片，再计算目标位置；只发送一次`ACTIVITY_MOVE`。无实际变化、非法落点或取消拖拽不得发命令。
- 只允许移动到既有日期；空日保持可见并可接收移动或新增地点。
- 发命令后至服务端新结果回读前锁定冲突编辑；失败或409读取最新结果并复原，不保留只存在于浏览器的假成功排序。
- 移动、删除和确认完成后恢复合理焦点，并通过`aria-live`给出简短结果；删除不得使用不可访问的裸浏览器确认流程。

## Locked product boundaries

- 不新增导出、分享、自动路线优化、价格/房态、实时开放状态或“已验证”等假功能。
- 卡片编辑只创建新revision并令旧地图为`NEEDS_UPDATE`；不得自动请求`map-renders`。只有用户点击“重新渲染地图”才触发地图请求。
- 不新增日期，不改变住宿锚点、地点身份、软假设、Top-3后端语义或公共API/schema。
- 不把原文映射、offset、置信度、UID/hash/revision/receipt、模型/Provider或内部阶段放入公共DOM、文案或日志。

## Commit sequence

必须形成两个可单独审查的串行提交：

1. `fix(g03r): make result enhancements race-safe`
   - 修复检查状态竞态、过期响应、409回读和busy/attempt生命周期；
   - 新E2E先稳定复现“后端3条、DOM不能永久0条”的失败，再证明修复；
   - 不做视觉重构或媒体功能。
2. `feat(g03r): rebuild itinerary result interactions`
   - 提取`itinerary-workspace.tsx`并完成拖拽、键盘/移动替代、删除确认、焦点和无障碍；
   - 保留现有真实功能与无自动地图重绘边界；
   - 不改依赖、配置或既有产品交付测试。

若第一提交无法独立通过竞态回归，停止第二提交并报告；不得用第二提交掩盖第一提交失败。

## Required verification

从`frontend`执行并完整报告：

- `npm run build`
- `npx playwright test e2e/g03r-result-ui.spec.js -c playwright.product-delivery.config.js`
- `npx playwright test e2e/g03r-result-ui.spec.js -c playwright.product-delivery.config.js --repeat-each=3`
- `npx playwright test e2e/g03-product-delivery.spec.js -c playwright.product-delivery.config.js`

新E2E必须覆盖：

- effect在请求完成前因map/stay/etag变化被cleanup，当前代际仍能恢复且最终显示3条建议；旧响应不覆盖新代际；同代际无重复并发调用；
- 同日、跨日、移入空日、无效落点、无变化和服务端失败/409；每次有效移动恰好一条命令，地图只变`NEEDS_UPDATE`且网络中无自动`map-renders`；
- 键盘上移/下移/跨日/目标位置、可访问删除确认、焦点恢复、aria-live、48px触控目标与reduced-motion；
- 1680×938与390×844两种视口；无内部字段、无Provider URL、无假功能。

若完整E2E需要本地服务，强制`local_fixture`、`AMAP_MOCK=true`、真实Qwen/高德key为空；不得产生Provider调用。Windows本地服务应使用Selector事件循环，且不得通过增加测试重试或放宽断言获得PASS。

## Completion handoff

完成时只回传：

- 两个原始commit及最终tip；
- 本地/上游/远端readback与clean状态；
- 每个提交精确路径差异；
- 所有必跑命令的真实结果与未运行项；
- 用户可见改进、剩余风险和主对话应执行的复核。

状态只能是`READY_TO_MERGE`、`IN_PROGRESS`或`BLOCKED_EXTERNAL`。贡献包不得自行修改registry状态、合并控制分支、部署、发布、合并`main`或激活G04。
