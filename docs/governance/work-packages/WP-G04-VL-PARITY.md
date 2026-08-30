# Work Package Prompt v1

## Identity and exact baseline

```yaml
prompt_schema_version: work-package-prompt-v1
package_id: WP-G04-VL-PARITY
goal_id: TC-VNEXT-G04-SCREENSHOT
role: CONTRIBUTOR
baseline_commit: 0531c0642f437932fb4e305a0a99fbb66b19e4bc
registry_activation_commit: 1065cafdc1a8efdbd82624474919e95dc5ee2d24
```

你是“VL对比合同与截图输入组件”独立功能对话。当前状态固定为`WAITING_FOR_WRITER_SLOT`；收到第二条明确启动消息前不得运行命令或写入。`registry_binding_commit`由主对话提供。

## Branch and isolated worktree

```yaml
branch: codex/g04-vl-parity
remote_branch: origin/codex/g04-vl-parity
worktree_path: C:/Users/26450/.codex/worktrees/9ce2/agentTravel
dialogue_ref: codex-task:01a05284-5ae9-7062-b19a-8919ac7eb9ba
```

获准启动后先确认branch/worktree/clean/binding并读取现行合同；不一致时只读回报。

## Owned and forbidden paths

### owned_paths

- backend/app/trip_understanding/screenshot_vl
- backend/scripts/run_g04_vl_parity.py
- backend/tests/test_g04_vl_parity.py
- frontend/src/components/g04-screenshot-source.tsx

### forbidden_paths

- AGENTS.md
- CLAUDE.md
- .github/workflows/core-mainline.yml
- docs/governance
- docs/product/TRIP_CHECK_API_CONTRACT.md
- backend/app/api
- backend/app/config.py
- backend/app/db/connection.py
- backend/app/db/migrations
- backend/app/main.py
- backend/app/trip_understanding/models.py
- backend/app/trip_understanding/repository.py
- backend/app/trip_understanding/service.py
- backend/app/trip_understanding/worker.py
- backend/app/trip_understanding/pipeline.py
- backend/tests/test_g04_screenshot_api.py
- backend/tests/test_g04_screenshot_pipeline.py
- backend/tests/test_g04_screenshot_postgres.py
- packages/trip-check-client/openapi.json
- packages/trip-check-client/openapi.current.json
- packages/trip-check-client/src/generated
- frontend/src/app/page.tsx
- frontend/src/lib/trip-understanding-v3.ts
- frontend/e2e/g04-screenshot-parity.spec.js
- frontend/package-lock.json
- miniapp/package-lock.json
- packages/trip-check-client/package-lock.json
- y-websocket/package-lock.json
- backend/eval_data/agent_gate_v1/automation_runner_requirements.lock
- backend/eval_data/agent_gate_v1/automation_runner_browser_package-lock.json

只允许修改owned paths；首页装配、共享client和运行时Provider选择由集成者完成。

## User-observable outcome

- 首页可嵌入一个键盘可用的多图选择组件：预览文件名的安全显示、顺序调整、删除、1～6张和大小/格式本地提示。
- partial、读取中和可重试状态使用中性文案，不展示模型、置信度、bbox、内部状态或红色错误墙。
- 若未来有合格VL binding，可用同一指标比较；当前无binding时诚实形成`NOT_RUN_NO_EXACT_BINDING`，Paddle保持默认。

## Non-goals and locked contracts

- 当前没有eligible Qwen-VL exact account/region/model/cost readback；本包不得联网、调用Provider、读取密钥、实现运行时fallback或把VL设为默认。
- 不实现API、数据库、上传、OCR、homepage装配、OpenAPI、migration或最终E2E。
- 比较门槛固定：关键字段、阅读顺序、最终卡片、bbox和P95均不得差于Paddle，且至少一个错误率相对下降≥20%；否则`EXPERIMENT_ONLY`。
- 若未来获准外发，必须先完成本地敏感信息遮蔽；本包当前只定义可测试的admission/comparison receipt，不发送图片。
- UI不得读取或渲染OCR文本/bbox/confidence/hash/ref/model/receipt；只输出选择后的`File[]`顺序和用户动作回调。

## Dependencies, inputs and outputs

- Dependencies：冻结的Goal/API合同；正式启动前Upload或OCR包至少一个已由集成者冻结。
- Inputs：Paddle与可选VL指标receipt；浏览器File对象和本地限制。
- Outputs：确定性`evaluate_vl_candidate()`决策/NOT_RUN receipt；`G04ScreenshotSource`受控组件及typed props。
- 无exact binding时runner必须在零网络下成功输出`NOT_RUN_NO_EXACT_BINDING`，而不是失败或伪造结果。

## Acceptance and targeted verification

- 后端测试覆盖无binding、指标任一退化、无20%改善、完整胜出和输入receipt异常。
- 前端组件覆盖1/6/7张、本地格式/大小提示、上移/下移/删除、禁用状态和可访问label；不得出现内部禁用词。
- Required backend command：`cd backend && python -m pytest -q tests/test_g04_vl_parity.py`
- Required lint：`cd backend && python -m ruff check app/trip_understanding/screenshot_vl scripts/run_g04_vl_parity.py tests/test_g04_vl_parity.py`
- Required frontend command：`cd frontend && npm run build`
- Success condition：全部退出码0，网络/Provider调用0；如依赖尚未安装，准确报告环境失败，不修改锁文件。
- homepage、API、browser、actual VL均为`NOT_RUN`，由集成者负责或保持未运行。

## Git and authority restrictions

```yaml
must_not_merge: true
must_not_modify_goal_or_registry: true
must_not_create_numbered_migration: true
must_not_modify_shared_openapi_or_lockfiles: true
```

只可commit/push当前分支；不得merge/rebase/squash/force-push。不得扩大Provider、费用、数据范围或公开接口。必须remote readback。

## Subagent boundary

```yaml
subagent_read_only: true
```

子Agent只可只读UI/a11y或比较逻辑复核；功能修复由本对话完成。

## Completion report

严格返回：

```text
package: WP-G04-VL-PARITY
branch: codex/g04-vl-parity
remote_branch: origin/codex/g04-vl-parity
worktree: C:/Users/26450/.codex/worktrees/9ce2/agentTravel
baseline: 0531c0642f437932fb4e305a0a99fbb66b19e4bc
registry_activation_commit: 1065cafdc1a8efdbd82624474919e95dc5ee2d24
final_commit: <40-hex or NONE>
remote_readback: <40-hex and PASS, or exact failure>
changed_paths: <actual paths>
commands_and_results: <commands, exit status, counts>
checks_not_run: <NOT_RUN items>
known_risks_and_remaining: <items or NONE>
subagent_usage: <purpose and output, or NONE>
status: READY_TO_MERGE | IN_PROGRESS | BLOCKED_EXTERNAL
status_reason: <why>
```

只有主对话可将本包从等待改为`IN_PROGRESS`或登记官方ready commit。
