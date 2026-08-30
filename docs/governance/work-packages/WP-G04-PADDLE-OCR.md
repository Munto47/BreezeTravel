# Work Package Prompt v1

## Identity and exact baseline

```yaml
prompt_schema_version: work-package-prompt-v1
package_id: WP-G04-PADDLE-OCR
goal_id: TC-VNEXT-G04-SCREENSHOT
role: CONTRIBUTOR
baseline_commit: 0531c0642f437932fb4e305a0a99fbb66b19e4bc
registry_activation_commit: 1065cafdc1a8efdbd82624474919e95dc5ee2d24
```

你是“Paddle OCR与来源映射”独立功能对话，只交付本包。主对话是唯一集成者。`registry_binding_commit`由启动消息提供；写入前必须确认它是HEAD祖先。

## Branch and isolated worktree

```yaml
branch: codex/g04-paddle-ocr
remote_branch: origin/codex/g04-paddle-ocr
worktree_path: C:/Users/26450/.codex/worktrees/7eed/agentTravel
dialogue_ref: codex-task:01a05284-5ade-77e0-b279-162943d5fab6
```

开始前确认branch/worktree/clean/binding并完整读取当前指导与Goal。任一不一致时只读回报。

## Owned and forbidden paths

### owned_paths

- backend/app/trip_understanding/screenshot_ocr
- backend/evals/g04_screenshot
- backend/scripts/run_g04_screenshot_parity.py
- backend/tests/test_g04_screenshot_ocr_unit.py

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

只允许修改owned paths。不得修改旧import截图代码或历史P5/P6 candidate/blind资产。

## User-observable outcome

- 多张截图按上传顺序转成同一可读语义文本；单图失败不会抹掉其他成功内容。
- OCR不确定的地点只要求局部确认，不会被当成已核验地点查询POI。
- 阅读顺序、bbox和低置信来源可在内部回读，但普通用户结果不暴露它们。

## Non-goals and locked contracts

- 不实现上传、临时文件、API、数据库、TripUnderstanding worker/pipeline wiring、UI、VL调用或migration。
- PaddleOCR 3.7.0 / PaddlePaddle 3.3.1是默认baseline；adapter必须lazy-load，测试不得下载模型或联网。
- `ScreenshotSourceDocumentV1`固定包含version、semantic_text、partial、images、lines、engine_binding、document_hash；line包含image_index、reading_index、text、confidence、四点bbox、Unicode code-point半开semantic span和requires_confirmation。
- 图间顺序固定为上传顺序；图内使用确定性纵向行聚类后y→x排序；低置信阈值0.85。
- OCR并发1、单图15秒、整批45秒由调用方RunSpec传入；本包不得静默改变。
- 与低置信span相交的下游地点由集成器阻断POI；本包必须提供可精确判断的span，而不是全批阻断。

## Dependencies, inputs and outputs

- Dependencies：Goal activation、现有Paddle依赖；可只读参考旧`backend/app/importing/screenshots.py`与历史synthetic receipt，不能把旧成绩当当前Gate。
- Inputs：按顺序的staged assets、engine protocol、threshold和deadline RunSpec。
- Outputs：`extract_screenshot_document(assets, engine, run_spec) -> ScreenshotSourceDocumentV1`；typed partial/all-failed/timeout errors。
- eval目录提供许可/证据分层manifest schema、关键字段F1、adjacency-F1、低置信召回、文本/截图地点差值、严重错误和P95确定性scorer；runner只读取显式manifest，不提交原图。

## Acceptance and targeted verification

- fake Paddle覆盖新版/兼容输出、四点bbox、跨图顺序、同y行x排序、Unicode span、0.85边界、partial、全失败和timeout。
- document hash对canonical内容确定；不含路径、原文件名或未冻结时间。
- scorer覆盖关键字段F1≥95%、低置信确认召回100%、adjacency-F1≥97%、地点P/R下降≤1pp、严重错误0、20次P95计算。
- Required command：`cd backend && python -m pytest -q tests/test_g04_screenshot_ocr_unit.py`
- Required lint：`cd backend && python -m ruff check app/trip_understanding/screenshot_ocr evals/g04_screenshot scripts/run_g04_screenshot_parity.py tests/test_g04_screenshot_ocr_unit.py`
- Success condition：全部退出码0；无网络、无模型下载、无tracked原图。
- actual Paddle、PostgreSQL、API、browser、真实来源manifest运行为`NOT_RUN`，由集成者执行。

## Git and authority restrictions

```yaml
must_not_merge: true
must_not_modify_goal_or_registry: true
must_not_create_numbered_migration: true
must_not_modify_shared_openapi_or_lockfiles: true
```

只可commit/push本分支；不得merge/rebase/squash/force-push，不得改变公共API、Provider选择或Gate。必须remote readback。

## Subagent boundary

```yaml
subagent_read_only: true
```

子Agent仅可只读检查排序/scorer；问题由本对话修复。

## Completion report

严格返回：

```text
package: WP-G04-PADDLE-OCR
branch: codex/g04-paddle-ocr
remote_branch: origin/codex/g04-paddle-ocr
worktree: C:/Users/26450/.codex/worktrees/7eed/agentTravel
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

冻结后的新提交或脏文件会使`READY_TO_MERGE`失效。
