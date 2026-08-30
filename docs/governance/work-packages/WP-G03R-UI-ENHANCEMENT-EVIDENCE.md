# Work Package Prompt v1

本包是`WP-G03R-UI-ENHANCEMENT-RECOVERY`第二轮且最后一轮修复复审内的验证附录，不是第三轮产品修复。`7fb559d071f03da940c398f1dafc0372f1bb9a48`的产品实现保持冻结；本包只消除独立门禁发现的Playwright假时钟竞态，并补齐已经冻结的10秒总预算、混合终态和旧代际终结证据。主对话是唯一集成者；贡献者不得修改产品代码、治理文件、配置或依赖，不得自行合并。

## Identity and exact candidate

```yaml
prompt_schema_version: work-package-prompt-v1
package_id: WP-G03R-UI-ENHANCEMENT-EVIDENCE
package_kind: VERIFICATION_ADDENDUM
goal_id: TC-VNEXT-G03-TOP3-AUDIT
role: CONTRIBUTOR
repair_review_cycle: 2
origin_develop_baseline: 8a33a4b22a405135f310376d8766d9170d80097d
recovery_implementation_commit: 7fb559d071f03da940c398f1dafc0372f1bb9a48
recovery_prompt_sha256: 70c2079a9c268174c4f5c6766b2295414427dd99c56220f0916661e62dd1d8bf
```

控制分支必须先提交并远端回读本提示词及registry binding。主集成随后把binding commit追加到已经包含`7fb559d...`的恢复分支，推送并登记新的精确prepared tip；贡献者只有收到activation commit、prepared tip和本提示词SHA-256后才能写测试。不得amend、squash、rebase或force-push既有UI历史。

## Branch and isolated worktree

```yaml
branch: codex/g03r-ui-enhancement-recovery
remote_branch: origin/codex/g03r-ui-enhancement-recovery
worktree_path: C:/Users/26450/.codex/worktrees/d09c/agentTravel
dialogue_ref: codex-task:01a05203-707c-79d2-a36b-d07e108f6abe
```

复用同一恢复任务和独立工作树，不创建新的产品策略或writer。只形成一个追加测试提交、push并回传精确tip；不修改治理文件、不摘取其他分支、不自行合并。

## Owned paths: deny by default

只允许写一条路径；未列入的路径一律禁止：

- `frontend/e2e/g03r-result-ui.spec.js`

## Explicit forbidden paths

- `AGENTS.md`、`CLAUDE.md`
- `docs/**`
- `backend/**`
- `packages/**`、`miniapp/**`、`y-websocket/**`
- `frontend/src/**`
- `frontend/package.json`、`frontend/package-lock.json`
- `frontend/playwright.config.js`、`frontend/playwright.product-delivery.config.js`
- `frontend/e2e/g03-product-delivery.spec.js`、`frontend/e2e/trip-understanding-v3.spec.js`
- 所有产品实现、依赖、锁文件、配置、fixture资产和未列入`owned_paths`的路径

若确定性测试证明产品实现存在真实缺陷，立即保持`IN_PROGRESS`并把最小复现交回主对话；本包不得顺手修改页面或请求库。

## Confirmed gate failure and root cause

主集成在精确候选`7fb559d...`上从`frontend`运行原始默认workers命令：

```text
npx playwright test e2e/g03r-result-ui.spec.js -c playwright.product-delivery.config.js
```

结果为`26/27`，唯一失败是慢响应单飞用例在释放首轮响应后执行`runFor(799)`，预期`{map:1, stay:1}`但瞬时观察到`{map:2, stay:1}`。隔离repeat10与workers=1 repeat30虽通过，但只能作诊断，不能替代失败的原始门禁。

两个独立只读复核与Playwright 1.52本地类型说明确认：`page.clock.install()`不会暂停时间；首轮释放后的DOM等待会自然消耗800ms边界，随后`runFor(799)`可能越界。第二轮map与stay Promise连续建立，但route回调分两次被测试fixture观察，因此`{2,1}`是非稳态快照，不是产品存在map-only分支。产品代码静态上仍是同轮map/stay并行、轮次串行且上一轮settled后才排一次800ms timer。

当前候选因此保持`NOT_READY_TO_MERGE`，必须获得修正后原始命令的新鲜通过证据。

## Required deterministic evidence repair

1. 慢响应边界用例必须显式暂停fake clock。保留“首轮仍挂起并推进801ms时调用数仍为1/1、两端最大在途均为1”的核心单飞证明；在首轮响应释放、下一轮800ms timer开始计时前冻结时钟，再用确定性barrier验证`799ms`不启动、边界到达后map与stay两个route都已被观察才读取`2/2`。不得在两个route回调之间读取非稳态计数。
2. 持续`PREPARING`及其他依赖毫秒边界的用例同样显式暂停时钟；不得把`clock.install()`误当作冻结，也不得用真实sleep、增加等待、重跑或`--workers=1`掩盖竞态。
3. 新增或强化一个“10秒总预算实际成为停止原因”的用例：每轮响应时间低于单请求3000ms，且在8轮上限前让累计session时间跨过10000ms；到预算后仅未完成端诚实降级，继续推进至少两倍预算后调用数不增长，Top-3和手动GET恢复仍可用。
4. 10秒用例至少覆盖一个端点已经返回公共终态、另一端持续`PREPARING`的混合状态，并证明成功端点终态被保留。可在同一用例覆盖双向，或用两条对称用例；不得把UNKNOWN/UNAVAILABLE计成成功。
5. 强化generation取消证据：fixture必须以明确的deferred/barrier区分“网络已abort”和“旧async promise的catch/finally已结束”。释放旧promise前，新generation的map/stay GET为0；释放后才允许新session启动，旧success/catch/finally不得覆盖或清理新代际。
6. 保留原有27条测试及其既有产品断言；只可追加或强化，不得删除、skip、降低隐私、一次命令、焦点、409恢复、Top-3、单飞、真实abort或无自动`map-renders`门槛。测试总数必须`N >= 28`且`retries=0`。
7. 页面实现、请求实现、800ms/3000ms/10000ms/8轮常量、公共API和用户文案全部冻结。若某项新增证据无法在不改产品的前提下通过，报告真实缺陷，不降低断言。

## Commit

只允许一个追加提交：

```text
test(g03r): make enhancement timing deterministic
```

不得amend或重写既有四个UI产品提交，不得force-push。

## Required verification

从`frontend`执行并完整报告：

- `npm run build`
- `npx playwright test e2e/g03r-result-ui.spec.js -c playwright.product-delivery.config.js`
- `npx playwright test e2e/g03r-result-ui.spec.js -c playwright.product-delivery.config.js --repeat-each=3`

原始命令必须使用默认workers、`retries=0`，结果必须为`N/N`与`3N/3N`。每条原始门禁在完成确定性补丁后运行一次；诊断性定向或`--workers=1`命令必须另列且不能替代门禁。环境失败保持`UNKNOWN/IN_PROGRESS`，不得连续重跑直至偶然通过。

## Completion handoff

完成时只回传：

- 单一追加测试commit和最终tip；
- 本地/上游/远端readback与clean状态；
- 精确单路径diff，产品实现相对`7fb559d...`字节不变，以及`frontend/package-lock.json`前后SHA-256一致；
- build、默认workers单轮与repeat3的原始命令、退出码、精确计数和retries；
- fake clock暂停位置、10秒预算成为停止原因、混合终态保留、旧promise终结barrier的断言说明；
- 未运行项、剩余风险和主对话应执行的独立复核。

状态只能是`READY_TO_MERGE`、`IN_PROGRESS`或`BLOCKED_EXTERNAL`。不得部署、发布、合并`main`、修改Goal或激活G04。
