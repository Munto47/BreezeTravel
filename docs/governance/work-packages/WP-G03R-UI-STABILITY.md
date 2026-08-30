# Work Package Prompt v1

本包只修复主集成按版本化原始命令与最终只读复审发现的结果页稳定性缺口：同代际重复`materialize`、新revision派生状态回流、写请求抢在旧检查终止前发出、增强首次失败永久阻塞、迟到预览覆盖以及编辑对话框焦点不闭环。主对话是唯一集成者；本包不得修改Goal、binding、registry或其他治理状态，不得自行合并。任何产品写入必须发生在本提示词及其SHA-256已由控制分支登记、独立分支/工作树已从精确准备点创建并完成远端回读之后。

## Identity and exact candidate

```yaml
prompt_schema_version: work-package-prompt-v1
package_id: WP-G03R-UI-STABILITY
goal_id: TC-VNEXT-G03-TOP3-AUDIT
role: CONTRIBUTOR
origin_develop_baseline: 8a33a4b22a405135f310376d8766d9170d80097d
ui_candidate_source_tip: 994ac8557f1d507787b9ca26e724d7df684d3faa
ui_commit_1: 8373484b47b1cbb60d1e803e589ed4fb4c5bb028
ui_commit_2: 994ac8557f1d507787b9ca26e724d7df684d3faa
```

控制分支先提交并远端回读本提示词及registry登记，再从该binding commit创建稳定性分支，按原顺序摘取上述两个UI提交。主对话随后把准备后精确tip登记并远端回读；贡献者只有收到该精确tip后才能写产品。不得改写、amend或force-push既有`codex/g03r-ui`历史。

## Branch and isolated worktree

```yaml
branch: codex/g03r-ui-stability
remote_branch: origin/codex/g03r-ui-stability
worktree_path: D:/munto/code/claudeProject/agentTravel-g03r-ui-stability
dialogue_ref: codex-agent:/root/ui_recovery
```

不得复用根工作树、控制工作树或原UI工作树。贡献包只形成一个追加式修复提交、push并回传精确tip；不修改治理文件、不摘取其他分支、不自行合并。

## Owned paths: deny by default

只有以下两条路径可写；未列入的路径一律禁止：

- `frontend/src/app/trip/result/page.tsx`
- `frontend/e2e/g03r-result-ui.spec.js`

## Explicit forbidden paths

- `AGENTS.md`、`CLAUDE.md`
- `docs/**`
- `backend/**`
- `packages/**`、`miniapp/**`、`y-websocket/**`
- `frontend/src/app/trip/result/itinerary-workspace.tsx`
- `frontend/src/lib/trip-understanding-v3.ts`
- `frontend/package.json`、`frontend/package-lock.json`
- `frontend/playwright.config.js`、`frontend/playwright.product-delivery.config.js`
- `frontend/e2e/g03-product-delivery.spec.js`、`frontend/e2e/trip-understanding-v3.spec.js`
- 所有依赖、锁文件、配置、图片、媒体、API、migration和未列入`owned_paths`的路径

## Confirmed blocking reproduction

主集成在clean远端候选`994ac855...`上按原合同命令运行：

```text
npx playwright test e2e/g03r-result-ui.spec.js -c playwright.product-delivery.config.js --repeat-each=3
```

结果为`56 PASS / 1 FAIL`、`retries=0`。失败repeat中，首个`materialize(A)`仍在途时SSE权威回读把当前ETag更新为B；effect因active key A与attempt key B不同而抢先abort。服务端其实已返回兼容的prepared ETag B，但客户端continuation尚未消费；catch/finally随后又启动`materialize(B)`。最终`materializeCalls=2`、`maxMaterializeInFlight=1`、`checksCalls=1`。这不是并发重复，但仍违反同一兼容代际只调用一次的合同。失败trace、截图和error context保留在原UI工作树测试输出中，只作诊断证据，不进入Git。

最终只读复审另确认：

- 卡片、住宿、建议采纳或materialize先返回新ETag、随后权威`/result`回读失败时，只清空局部override，旧`result.map=AVAILABLE`仍可回流；旧增强读取也可能在同一generation写回，写响应中的`NEEDS_UPDATE`未成为安全投影。
- `beginMutation()`只设ref/state，检查请求到下一次effect才abort；同一调用栈内的新写POST可能与旧`materialize`并发。
- map或stay首次读取失败后保持`null`；轮询只识别已知`PREPARING`，checks又要求二者非空，Top-3会永久停在等待态。
- 慢preview的catch/finally未验证request id/key，可清掉新代际PREPARE或写入旧错误。
- 编辑/新增/替换对话框缺少focus trap，关闭、Escape、保存后没有统一恢复触发焦点。

## Required fix

- 同一resource的在途`materialize`遇到ETag或增强依赖变化时，不得仅因key变化立即abort；允许它在现有10秒有界期限内完成。
- 返回prepared key与当前`resource + etag`兼容时直接继续读取checks，不再POST第二次`materialize`。
- 返回key不兼容或有界期限到达时，结束旧请求并只排一次当前代际；必须保持最大并发1。
- resource切换、组件卸载和mutation/reconciliation lock仍立即终止旧请求。
- 所有得到新`resource + etag`或写响应的入口必须同步失效旧增强generation、检查与预览；权威回读失败时不得让旧`AVAILABLE`回流，须使用写响应可证明的`NEEDS_UPDATE`或保守公共投影，随后由权威回读替换。
- 开始卡片、住宿、adopt、地图或隐私写入时必须同步abort旧检查，并等待被跟踪的检查promise进入终态后才发送写POST；不得让写入与旧`materialize`并发。
- map或stay首次读取失败必须有可恢复的有界路径，或以顶层公开结果的安全状态继续/诚实降级；不得永久阻止Top-3且不得把UNKNOWN/UNAVAILABLE算成成功。
- preview必须有独立request id、key或AbortController；只有当前请求能在成功、失败和finally更新preview、message与busy。
- 编辑/新增/替换对话框必须形成focus trap；关闭、Escape、保存和失败后恢复合理触发焦点或当天标题，不把键盘用户留在背景/body。
- 不改变公共API、请求schema、10秒上限、Provider、后端或用户文案合同。

## Deterministic regression

- 把原60/150ms相对sleep替换为deferred/barrier：确认A请求已进入，再释放SSE/B result，最后显式释放首请求返回B；flush额外两帧后仍必须是`resultReads=2`、`materialize=1`、`maxInFlight=1`、`checks=1`、`abort=0`。
- 挂起用例使用Playwright fake clock：A挂起且B已回读时先证明没有第二POST；推进`10001ms`后证明旧请求结束、A/B各一次、abort=1、最大并发1、checks一次、busy释放且最终显示3条。
- 保留409与普通失败显式重试覆盖，不得把真正不兼容代际合并。
- 从旧`AVAILABLE`开始模拟写POST返回新ETag/`NEEDS_UPDATE`而第一次result回读失败；恢复锁期间地图不得回到AVAILABLE，旧增强迟到响应不得覆盖，显式恢复后才采用权威结果，且网络中无自动`map-renders`。
- 在materialize挂起期间触发每类代表性写操作，严格统计检查POST与写POST总并发；写POST必须在旧检查终态后才开始。
- map和stay各自首次503后必须自动有界恢复或出现可操作降级，最终Top-3显示3条或给出诚实可恢复提示，不得永久“等待准备”。
- 慢preview跨mutation、ETag和resource切换后，其success/catch/finally均不得覆盖新代际preview、message或busy。
- 键盘从编辑、替换、添加触发器打开对话框后只能在modal内循环；Escape、关闭与保存回到合理触发点/新卡片/当天标题。
- 不得靠重跑、`--workers=1`、增加等待、放宽断言、修改Playwright配置或降低门槛获得PASS。

## Commit

只允许一个新提交：

```text
fix(g03r): avoid duplicate materialize across compatible etags
```

不得amend或重写前两个UI提交，不得force-push。

## Required verification

从`frontend`执行并完整报告：

- `npm run build`
- `npx playwright test e2e/g03r-result-ui.spec.js -c playwright.product-delivery.config.js`
- `npx playwright test e2e/g03r-result-ui.spec.js -c playwright.product-delivery.config.js --repeat-each=3`
- 可追加`--workers=1`诊断，但不能替代上述原始命令。

原始命令必须在默认workers、retries=0下得到`19/19`和`57/57`。若产品逻辑仅改变上述检查状态机且既有G03旅程在原UI候选已通过，贡献者无需自行重启完整后端旅程；主集成合并后仍运行完整产品矩阵。

## Completion handoff

完成时只回传：

- 单一新commit和最终tip；
- 本地/上游/远端readback与clean状态；
- 精确两条路径差异；
- 原始单轮、默认workers repeat3、build的真实结果；
- 仍未运行项、剩余风险和主对话应执行的复核。

状态只能是`READY_TO_MERGE`、`IN_PROGRESS`或`BLOCKED_EXTERNAL`。不得部署、发布、合并`main`、修改Goal或激活G04。
