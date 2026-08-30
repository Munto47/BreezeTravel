# Work Package Prompt v1

本包只修复`030d2129736ac354a4febe6631e8141098e70a75`最终独立复核发现的两个剩余结果页缺口：地图/住宿读取可能挂起或无限重叠轮询并永久阻断Top-3，以及用户关闭改动预览后迟到响应仍可重新打开预览。主对话是唯一集成者；本包不得修改Goal、binding、registry或其他治理状态，不得自行合并。任何产品写入必须发生在本提示词及其SHA-256由控制分支登记、独立分支/工作树从精确binding创建、前序UI提交按顺序准备并完成远端回读之后。

## Identity and exact candidate

```yaml
prompt_schema_version: work-package-prompt-v1
package_id: WP-G03R-UI-ENHANCEMENT-RECOVERY
goal_id: TC-VNEXT-G03-TOP3-AUDIT
role: CONTRIBUTOR
origin_develop_baseline: 8a33a4b22a405135f310376d8766d9170d80097d
ui_candidate_source_tip: 994ac8557f1d507787b9ca26e724d7df684d3faa
ui_stability_source_tip: 030d2129736ac354a4febe6631e8141098e70a75
ui_commit_1: 8373484b47b1cbb60d1e803e589ed4fb4c5bb028
ui_commit_2: 994ac8557f1d507787b9ca26e724d7df684d3faa
ui_stability_commit: 030d2129736ac354a4febe6631e8141098e70a75
repair_review_cycle: 2
```

控制分支先提交并远端回读本提示词及registry登记，再从该binding commit创建恢复分支，依次摘取上述两个原UI提交和一个稳定性提交。主对话随后把准备后的精确tip登记并远端回读；贡献者只有收到该精确tip后才能写产品。不得改写、amend、squash、rebase或force-push既有UI历史。

## Branch and isolated worktree

```yaml
branch: codex/g03r-ui-enhancement-recovery
remote_branch: origin/codex/g03r-ui-enhancement-recovery
worktree_path: C:/Users/26450/.codex/worktrees/d09c/agentTravel
dialogue_ref: codex-task:01a05203-707c-79d2-a36b-d07e108f6abe
```

不得复用根工作树、控制工作树、原UI工作树或稳定性工作树。贡献包只形成一个追加式修复提交、push并回传精确tip；不修改治理文件、不摘取其他分支、不自行合并。

## Owned paths: deny by default

只有以下三条路径可写；未列入的路径一律禁止：

- `frontend/src/app/trip/result/page.tsx`
- `frontend/src/lib/trip-understanding-v3.ts`
- `frontend/e2e/g03r-result-ui.spec.js`

## Explicit forbidden paths

- `AGENTS.md`、`CLAUDE.md`
- `docs/**`
- `backend/**`
- `packages/**`、`miniapp/**`、`y-websocket/**`
- `frontend/src/app/trip/result/itinerary-workspace.tsx`
- `frontend/src/app/trip/result/activity-image.tsx`
- `frontend/package.json`、`frontend/package-lock.json`
- `frontend/playwright.config.js`、`frontend/playwright.product-delivery.config.js`
- `frontend/e2e/g03-product-delivery.spec.js`、`frontend/e2e/trip-understanding-v3.spec.js`
- 所有依赖、锁文件、配置、图片、媒体、API、migration和未列入`owned_paths`的路径

## Confirmed blocking reproduction

独立复核确认`030d212...`的`refreshEnhancements`直接等待无`AbortSignal`的map/stay GET，并在任一状态为`PREPARING`时每800ms使用`setInterval`再次调用。结果是：

- 任一GET永不结束时，首次`Promise.allSettled`永不返回，map/stay保持`null`，依赖二者终态的checks永远不启动；
- 响应慢于800ms或持续返回`PREPARING`时，多轮读取并发重叠且没有总次数或总时长上限；
- 当前E2E只覆盖立即503，不覆盖挂起、慢响应或持续`PREPARING`，因此`build + 19/19 + 57/57`不能证明这条用户路径安全；
- 关闭现有改动预览只清空视图，没有推进preview request id。若新预览仍在途，用户点击关闭后迟到成功仍会重新显示预览。

最终独立复核结论为`1 P1 + 1 P2 / NOT_READY_TO_MERGE`。`030d212...`保持冻结，不得直接追加、amend或force-push。

## Required fix

- `readTripUnderstandingMap`、`readTripUnderstandingStay`和`previewTripUnderstandingChange`只增加可选`AbortSignal`参数并原样传给现有`fetch`；请求URL、method、credentials、cache、headers、body、返回类型、错误码和公共API全部不变。
- 结果页为当前`resource + enhancement generation`维护唯一增强轮次；每轮可并行读取map与stay，但上一轮未终态前不得启动下一轮，同一端点最大在途数必须为1。
- 冻结自动读取边界：单请求超时`3000ms`、单session总预算`10000ms`、最多`8`轮（含首次）、成功返回`PREPARING`后的轮间隔`800ms`；达到任一上限即停止。挂起请求到单轮边界必须真正`abort`，不能只用`Promise.race`让底层网络继续占用。
- 用“上一轮终态后再排下一轮”的可取消一次性timer或async loop替代`setInterval`。持续`PREPARING`只允许在上述固定边界内继续；停止后把仍挂起、失败或仍为`PREPARING`的部分投影为现有诚实`UNAVAILABLE`回退，不把UNKNOWN/UNAVAILABLE算成成功，并允许checks继续形成可解释结果或显示其现有可重试状态。
- 任一端点已经返回公开终态时保留该终态，只对未能在预算内终态的端点降级。不得因为另一个端点失败而覆盖已成功结果。
- resource切换、组件卸载、new revision/generation、mutation与reconciliation必须取消旧增强请求和待执行timer；旧代际success/catch/finally不得写入新代际状态或清除新代际控制器。
- 有界停止后显示单一、可访问的“重新读取路线与住宿状态”操作；它只启动一个新的有限GET session，连续点击仍保持单飞，不清除已完成Top-3，也不得POST`/map-renders`或制造“路线已优化/已核验”等成功文案。
- 关闭改动预览必须同时使当前preview request id失效并真实abort其请求；mutation、resource/generation切换与卸载沿用同一取消边界。关闭后任何迟到success/catch/finally都不得重开预览、改写当前message或busy。
- 不改变公共API/schema、后端、Provider、10秒checks上限、卡片编辑命令、地图手动更新合同、用户隐私投影或依赖。

## Deterministic regression

- 新增挂起端点用例：map或stay至少一个route被barrier挂起；安装Playwright fake clock，推进`3001ms`后断言请求被abort、增强round最大并发1、另一端点成功结果保留、Top-3最终显示3条、诚实降级与手动恢复可见；再推进30秒计数不增长，测试结束前释放所有barrier。
- 新增慢响应单飞用例：首轮耗时超过800ms时推进时钟，确认旧实现会触发的第二轮没有启动；释放首轮后才可调度下一轮，并保持map/stay各自最大在途数1。
- 新增持续`PREPARING`用例：每轮立即返回`PREPARING`，推进完整总预算后记录固定有限调用数；继续推进至少两倍预算，调用数不再增长，页面采用现有诚实降级且checks不被永久阻断。
- 对map与stay分别验证：一个终态成功、另一个挂起/失败/持续准备时，成功结果被保留，只有未终态部分降级。
- 新增手动恢复用例：停止后fixture切换为AVAILABLE；单击或快速双击恢复只新增一个session，最终更新终态并隐藏恢复提示；已完成materialize/checks不重复，`POST /map-renders`始终为0。
- 新增generation取消用例：resource或ETag更新后旧增强请求被abort，且新session只能在旧promise终态后启动；释放旧route后不得覆盖新map/stay，也不得启动旧timer。
- 新增预览关闭用例：保留旧预览，启动第二个deferred preview，在其返回前点击关闭；断言第二请求被abort，再释放迟到success与failure两种结果后预览仍关闭，busy/message不被旧请求污染，adopt写入为0。
- 所有时序测试使用deferred/barrier、请求计数与fake clock；不得使用相对sleep、重跑、`--workers=1`、增加等待、放宽断言或修改Playwright配置获得PASS。
- 保留现有19个测试及其断言；只可追加或强化测试，不得删除、skip、降低计数、隐私、一次命令、焦点、409恢复或无自动`map-renders`门槛。

## Commit

只允许一个新提交：

```text
fix(g03r): bound enhancement polling and stale previews
```

不得amend或重写前三个UI提交，不得force-push。

## Required verification

从`frontend`执行并完整报告：

- `npm run build`
- `npx playwright test e2e/g03r-result-ui.spec.js -c playwright.product-delivery.config.js`
- `npx playwright test e2e/g03r-result-ui.spec.js -c playwright.product-delivery.config.js --repeat-each=3`
- 可追加`--workers=1`诊断，但不能替代上述原始命令。

原始命令必须使用默认workers、`retries=0`，新增测试数记为`N`后必须得到`N/N`与`3N/3N`，且`N >= 24`。若测试环境残留服务或端口冲突，先完成可证明的清理再按原命令重新开始；不得把环境失败报告成产品PASS。贡献者无需运行Provider、PostgreSQL或完整后端旅程；主集成合并后运行完整产品矩阵。

## Completion handoff

完成时只回传：

- 单一新commit和最终tip；
- 本地/上游/远端readback与clean状态；
- 精确三条路径差异及`frontend/package-lock.json`前后SHA-256一致；
- build、默认workers单轮与repeat3的原始命令、退出码、精确计数和retries；
- 挂起/慢响应/持续PREPARING/代际取消/预览关闭的断言计数；
- 仍未运行项、剩余风险和主对话应执行的复核。

状态只能是`READY_TO_MERGE`、`IN_PROGRESS`或`BLOCKED_EXTERNAL`。不得部署、发布、合并`main`、修改Goal或激活G04。
