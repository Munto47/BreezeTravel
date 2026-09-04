# WP-G07-UI-CONVERGENCE：三视图与响应式体验合流（第一轮恢复修复）

## Identity and exact baseline

- prompt_schema_version: work-package-prompt-v1
- package_id: WP-G07-UI-CONVERGENCE
- goal_id: TC-VNEXT-G07-CANDIDATE
- role: CONTRIBUTOR
- baseline_commit: 95bcb76a9688a03a0527e02317918ecdbb48bfe2
- registry_activation_commit: 97a09f41b7eacbb21210d62c148bb46e07385f2b

你是独立前端体验功能对话。主对话是唯一集成者；本对话只拥有这一项工作包，不得承担后端语义、公共合同、候选执行或第二个功能包。registry_binding_commit 与 branch_point_commit 由主对话在正式启动消息中提供，不写入本文件以避免提交自引用。

## Branch and isolated worktree

- branch: codex/g07-ui-convergence-r1-fix
- remote_branch: origin/codex/g07-ui-convergence-r1-fix
- worktree_path: C:/Users/18770/.codex/worktrees/30f3/BreezeTravel
- dialogue_ref: codex-task:01a06c20-ab96-73f1-a285-75186e0f3267

开始写入前必须确认主对话发送的 registry_binding_commit 与 branch_point_commit 都是当前 HEAD 的祖先，且当前 HEAD 精确等于 branch_point_commit；分支、上游、worktree、提示词 SHA-256 与 registry 必须完全一致，工作树必须干净。随后在 backend 运行工作包校验。任一不一致时只读回报，不能自行改 registry、rebase 或更换工作树。

## Repair-cycle override

原提交 `64c27754fda194e7f881466225382d23e0ea823f` 已由主任务保留在 `origin/codex/g07-ui-convergence`，但未登记 READY、未合并。独立反方复审发现两个直接破坏有界恢复合同的 P1，因此本轮只允许采用以下不同策略修复：

1. 在新的 branch_point 上执行 `git cherry-pick --no-commit 64c27754fda194e7f881466225382d23e0ea823f` 复用原实现，不改写、amend 或 force-push 旧分支。
2. 登录请求的 10 秒边界必须覆盖响应正文读取，而不是只覆盖响应头；当 `fetch()` 已返回 Response、但 JSON body 永久停滞时，登录、验证码和测试账号入口都必须结束 loading、显示安全中性错误并可重试。
3. 地图“重新渲染”按钮只能由服务端 `available_actions` 中的 `RENDER_MAP` 开放，不能因为本地看到 LIMITED/UNAVAILABLE 就强制产生写请求。POST 结果不确定时，必须区分“POST 已明确被接受”和“只读 GET 仍返回请求前相同的 LIMITED/UNAVAILABLE”。后一种不能解除写锁、不能宣称本次已确认，也不能允许同页再次产生新的地图 POST；只允许继续只读确认。若 POST 已明确返回成功，后续同状态终态可以作为该次请求的权威结果。
4. 在 `g03r-result-ui.spec.js` 增加两个确定性反例：响应头已到但 body 不结束的登录请求在 10 秒后恢复；起始为 LIMITED 或 UNAVAILABLE 的地图 POST 超时后，同状态 GET 不得解锁或产生第二次 POST。原 59 项适用浏览器旅程必须继续通过。

这两个 P1 关闭前不得申请 READY。红/玫瑰装饰色与重复启动验证码倒计时作为 P2 记录，本轮不要求扩大修改。

## Owned paths

- frontend/src/app/trip/result/page.tsx
- frontend/src/app/trip/result/itinerary-workspace.tsx
- frontend/src/app/trip/result/result-navigation.tsx
- frontend/src/app/trip/result/result-presentation.ts
- frontend/src/app/trip/result/accessible-dialog.tsx
- frontend/src/app/login/page.tsx
- frontend/e2e/trip-understanding-v3.spec.js
- frontend/e2e/g02-product-delivery.spec.js
- frontend/e2e/g03-product-delivery.spec.js
- frontend/e2e/g03r-result-ui.spec.js

## Forbidden paths and mechanisms

- AGENTS.md、CLAUDE.md、README.md 与 docs/governance
- backend、miniapp、packages/trip-check-client 与数据库 migration
- frontend/src/lib/trip-understanding-v3.ts、frontend/src/types、frontend/src/app/page.tsx、frontend/src/app/share、frontend/src/app/profile、frontend/src/app/about 与全局样式
- 所有依赖清单、依赖锁文件、OpenAPI 和生成物
- 公共 v3 API、命令类型、ETag、认证协议、匿名写入边界或 Provider 配置
- 浏览器直接搜索地点、调用高德路线、计算驾车路线或补画推测路线
- OCR、截图上传、图片理解、RAG、Yjs、旧房间、聊天、Planner 或运行时多 Agent

只允许修改 owned paths。可以只读查看 D:/CODEX/TripCheck 的前端作为视觉与交互参考，但不得复制其大组件、整份 CSS、路由、城市选择或后端实现。若正确实现确实需要禁止路径、新依赖或公共合同变化，停止该部分并回报主对话，不得扩大路径或绕过测试。

## User-observable outcome

用户粘贴文字并生成后，默认直接进入逐日行程卡片，可在“行程 / 地图与住宿 / 优先检查”三个清楚的主视图间切换。桌面使用可展开的窄图标侧栏，手机使用固定三项底部导航。卡片编辑后旧路线分钟立即失效并显示“路线需要更新”，用户手动更新地图后再看到服务端返回的真实路线。住宿、Top-3、分享、隐私删除、软假设和错误恢复继续可用。

视觉继续使用 BreezeTravel 的暖白背景、翡翠绿、逐日色带和类别插画。地点插画只是本地界面装饰，不能依赖外部照片才能使用，也不得被描述成图片识别能力。

## Implementation slices

### 1. Three-view shell and navigation

- 生成完成后的默认视图是“行程”，三视图不得同时堆在一个长页面。
- 桌面侧栏默认约 68px，悬停或键盘聚焦后可展开到约 184px；图标有可读标签、当前态和清晰焦点。
- 手机在底部固定“行程 / 地图住宿 / 优先检查”三项，并为安全区域留出 padding；不得遮挡主要操作。
- 导航状态必须可由键盘操作并有 aria-current 或同等语义；切换视图不丢失已加载结果或编辑状态。

### 2. Itinerary cards and transport connectors

- 保留逐日卡片、色带、类别插画、地点详情、编辑、替换、移动、删除和新增。
- 每日卡片按横向带状结构呈现；相邻卡片之间显示连接器。
- 连接器只从当前 MapRenderView 派生。只有地图状态为 AVAILABLE、同日相邻卡片名称可唯一对应、所选 walking 或 transit 结果为 AVAILABLE 且 duration_minutes 为正数时，才显示对应方式和分钟。
- 任一条件不满足时显示中性“路线待确认”；地图为 NEEDS_UPDATE 时显示“路线需要更新”。不得沿用旧分钟、推断名称、补造时长或显示驾车。
- 任一卡片语义编辑成功后，新 revision 到来即立刻隐藏所有旧分钟并投影为“路线需要更新”；该编辑流程不得发起地图重算或路线 Provider 请求。

### 3. Map and stay theatre

- 地图与住宿合并为一个主视图，使用沉浸式地图布局；桌面显示可收起的浮动地点目录，手机默认收起。
- 地点目录、地图标记、路线和每日色带使用一致的日颜色；目录保持可键盘展开和收起。
- 只消费服务端已有地点与路线几何。没有 geometry 时不渲染 polyline，但必须保留地点目录和服务端文字路线摘要；不能把点位直连成猜测路线。
- walking/transit 切换只展示对应服务端结果；不得新增 driving 默认值，也不得在浏览器调用高德或其他地点/路线服务。
- PREPARING、LIMITED、UNAVAILABLE 与 NEEDS_UPDATE 使用中性状态，不留空白地图；提供现有可恢复操作。住宿待选择、区域建议、三家候选与选择流程不得被界面重做移除。

### 4. Priority checks, dialogs and login

- 优先检查只显示 Top-3，标签只能是“必须调整 / 可以更好 / 需要确认”；普通用户 DOM 不得出现 Finding code、Evidence、revision、receipt、Provider、模型或内部阶段。
- 保留建议预览与采纳流程；红色只用于有可靠证据的“必须调整”，其余失败和待确认状态使用中性色。
- 删除 owned paths 中所有 window.prompt 与 window.confirm，并消除重复删除入口。统一使用可访问对话框：有明确标题、role=dialog、aria-modal、初始焦点、Tab 焦点循环、Escape 关闭和关闭后焦点恢复；破坏性操作必须有清晰确认。
- 软假设编辑、原文删除、行程删除及卡片删除均使用同一可访问交互模式；请求失败时不得显示成功，并保留重试或取消路径。
- 登录页改为暖白与翡翠绿；邮箱、验证码等字段有永久可见 label，OTP 控件可读且键盘可用，错误为安全的中性文案并有焦点反馈。不得显示底层 Error.message、Provider 或内部字段。

### 5. Bounded recovery and responsive closure

- 初次结果读取处于 PROCESSING 时必须有总等待上限，最长 60 秒后进入可恢复状态，显示“重试”和“返回首页”；不得无限加载。
- 任何读取、编辑、地图、建议、删除或登录失败不得留下空白页、无限 spinner 或不可达操作。
- 在 1440x900、1280x720、1024x768、390x844、360x800 五个视口验证无横向溢出、导航遮挡或不可达核心按钮。
- 支持 prefers-reduced-motion；轻微阴影、动画和间距问题可以记录为后续项，但不能阻断主路径。

## Locked contracts and preserved capabilities

- 依赖已合并的文字提交 95bcb76a9688a03a0527e02317918ecdbb48bfe2；不得回退其 40/40 安全、37/40 精确结果。
- 不修改公共 API、MapRenderView、共享客户端类型、数据库、登录协议或匿名固定示例边界。
- 首页继续是不带城市、日期、人数或房间前置表单的纯文字入口；本包只回归验证，不修改首页。
- 自定义攻略继续走现有登录路径；匿名固定示例保留。本轮不扩大匿名写入。
- 分享、隐私删除、软假设、ETag 并发冲突、错误读回、住宿、Top-3 预览与采纳都必须保持。
- 用户界面继续隐藏原文映射、source span、offset、UID、hash、revision、receipt、RunSpec、Provider、模型与内部错误。

## Acceptance and targeted verification

完成前至少满足：

1. 三视图、桌面侧栏、手机底栏、卡片连接器、地图目录、住宿和 Top-3 行为均有自动化断言。
2. 编辑后旧分钟立即消失、出现“路线需要更新”，且没有自动地图 POST 或浏览器 Provider 请求。
3. 无 geometry 时 DOM 中无路线 polyline，但地点目录与文字路线摘要仍可见。
4. 所有 owned paths 中 window.prompt 与 window.confirm 为零；对话框完成焦点循环、Escape 和焦点恢复验证。
5. 初始 PROCESSING 有界失败后出现重试与返回首页；中性失败状态和恢复动作可验证。
6. 登录 label、OTP、键盘操作、安全错误文案及暖白翡翠视觉可验证。
7. 五个目标视口无横向溢出或核心控件遮挡。
8. 现有分享、隐私删除、软假设、并发恢复、住宿和 Top-3 旅程无回退。

必跑命令：

- 在 backend 运行 python -m scripts.validate_work_packages --package-id WP-G07-UI-CONVERGENCE。
- 在 frontend 运行 npm run build。
- 在可用的本地 fixture 后端上运行 npx playwright test -c playwright.candidate.config.js --workers=1 --reporter=list；如果贡献任务无法启动完整 fixture 服务，必须至少运行直接相关的可控浏览器测试并在回报中精确列出未运行项，完整套件由集成者复跑。
- 在 backend 运行 python -m scripts.validate_core_mainline。
- 运行 git diff --check、核对实际改动严格等于 owned paths 子集，并确认最终工作树干净。

成功条件是构建通过、适用浏览器测试零 unexpected/零 skipped、无安全底线失败，并由主任务复跑完整 fixture R0。真实 Qwen/高德、正式 Provider 矩阵、OCR、blind、H1、公网、生产、release 与 main 合并均为 NOT_RUN。

## Git and authority restrictions

- must_not_merge: true
- must_not_modify_goal_or_registry: true
- must_not_create_numbered_migration: true
- must_not_modify_shared_openapi_or_lockfiles: true
- subagent_read_only: true

只可在 codex/g07-ui-convergence-r1-fix 上工作。branch_point_commit 之后只形成一个最终功能提交，且直接父提交必须等于 branch_point_commit；实现期间不要创建中间提交。不得自行合并、rebase、squash、amend 已发送提交、force-push、改 Goal/registry/Gate 或删除历史证据。完成时 push 自己的分支并提供远端精确 readback。

子 Agent 只可做短期标注、独立复核、反方审查或故障诊断，不得提交产品代码或改变工作包状态；发现的问题由本功能对话修复。

## Completion report

严格回报 package、branch、remote_branch、worktree、baseline、registry_activation_commit、收到的 registry_binding_commit 与 branch_point_commit、final_commit、remote_readback、changed_paths、commands_and_results、checks_not_run、known_risks_and_remaining、subagent_usage、status 与 status_reason。

READY_TO_MERGE 只是请求。只有主对话复核路径、提交、测试、干净工作树和远端 readback 后，才能登记官方状态。
