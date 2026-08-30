# Work Package Prompt v1

## Identity and exact baseline

```yaml
prompt_schema_version: work-package-prompt-v1
package_id: WP-G04-EPHEMERAL-UPLOAD
goal_id: TC-VNEXT-G04-SCREENSHOT
role: CONTRIBUTOR
baseline_commit: 0531c0642f437932fb4e305a0a99fbb66b19e4bc
registry_activation_commit: 1065cafdc1a8efdbd82624474919e95dc5ee2d24
```

你是“临时上传与清理”独立功能对话，只交付本包。主对话是唯一集成者。`registry_binding_commit`由主对话在启动消息中提供；开始写入前必须确认它是当前HEAD祖先。

## Branch and isolated worktree

```yaml
branch: codex/g04-ephemeral-upload
remote_branch: origin/codex/g04-ephemeral-upload
worktree_path: C:/Users/26450/.codex/worktrees/f87f/agentTravel
dialogue_ref: codex-task:01a05284-5aeb-7a63-bd70-2c9f17269047
```

开始前确认当前分支、绝对工作树、clean状态、HEAD包含binding commit，并回读`AGENTS.md`、`CURRENT_GOAL.md`、binding、registry。任一不一致时只读并回报。

## Owned and forbidden paths

### owned_paths

- backend/app/trip_understanding/screenshot_batch
- backend/tests/test_g04_screenshot_batches_unit.py

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

只允许修改owned paths；需要共享路径时停止该改动并向主对话提供明确接口建议。

## User-observable outcome

- 登录用户选择1～6张截图时，服务器能按选择顺序有界接收并验证PNG/JPEG/WebP。
- 原图仅以随机文件名进入受限临时目录；成功、失败、取消、超时和TTL路径均可验证删除。
- 只有清理全部成功，后续API层才有资格返回可消费引用；清理失败必须形成内部阻断结果。

## Non-goals and locked contracts

- 不实现API router、数据库、batch ref、认证、TripUnderstanding消费、OCR、UI、migration或OpenAPI。
- 不复用025的workspace-bound表，不恢复旧room/import入口，不保存原文件名或像素。
- 输入固定为重复字段`screenshots`，1～6张，单张≤10MiB、总体≤61MiB；MIME和magic必须一致，JSON Base64不属于本包入口。
- 临时文件使用32字节以上随机locator和owner-only mode/ACL；cleanup最多3次，并返回每次不可变attempt数据给集成者持久化。
- 导出稳定接口：`stage_screenshot_multipart(content_type, chunks, limits, temp_root)`、`cleanup_staged_batch(batch, terminal_reason, attempts=3)`以及`StagedScreenshot/StagedBatch/CleanupAttempt`类型；不得依赖数据库或FastAPI响应模型。

## Dependencies, inputs and outputs

- Dependencies：Goal activation `1065caf...`；Python标准库、Starlette/FastAPI现有依赖；可差异审查旧`screenshots.py`算法但不得修改或公开其旧响应。
- Inputs：content-type、异步body chunks、受控temp root、终态原因。
- Outputs：按part顺序的staged asset元数据、不可逆SHA-256、generated locator、清理attempt；不返回原始文件名。
- 异常必须分为数量/大小/MIME-magic/空文件/取消/超时/清理失败等内部typed errors，供集成者映射稳定公共错误。

## Acceptance and targeted verification

- 覆盖0/1/6/7张、三种格式、10MiB边界、61MiB总体、伪MIME、空文件、顺序和分块越界。
- 覆盖成功、解析失败、异常、取消、deadline和清理重试；所有创建过的文件最终不存在。
- 路径逃逸、原文件名持久化和Git目录写入为0。
- Required command：`cd backend && python -m pytest -q tests/test_g04_screenshot_batches_unit.py`
- Required lint：`cd backend && python -m ruff check app/trip_understanding/screenshot_batch tests/test_g04_screenshot_batches_unit.py`
- Success condition：全部退出码0；测试不得写仓库tracked文件。
- 完整API/PostgreSQL/browser/parity为`NOT_RUN`，由集成者负责。

## Git and authority restrictions

```yaml
must_not_merge: true
must_not_modify_goal_or_registry: true
must_not_create_numbered_migration: true
must_not_modify_shared_openapi_or_lockfiles: true
```

只可commit/push当前工作包分支；不得merge/rebase/squash/force-push，不得改Goal、binding、registry或官方Gate。完成后必须执行remote readback。

## Subagent boundary

```yaml
subagent_read_only: true
```

子Agent只可短期只读复核或诊断；发现的问题由本功能对话在owned paths内修复。

## Completion report

严格返回：

```text
package: WP-G04-EPHEMERAL-UPLOAD
branch: codex/g04-ephemeral-upload
remote_branch: origin/codex/g04-ephemeral-upload
worktree: C:/Users/26450/.codex/worktrees/f87f/agentTravel
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

`READY_TO_MERGE`只是请求；主对话复核路径、commit、测试、clean tree和remote readback后才会冻结。
