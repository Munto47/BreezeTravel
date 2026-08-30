# Work Package Prompt v1

本包只修复G03行程语义P1缺陷。主对话是唯一集成者；本包不得修改Goal、registry或其他治理状态，不得自行合并。

## Identity and exact baseline

```yaml
prompt_schema_version: work-package-prompt-v1
package_id: WP-G03R-SEMANTIC
goal_id: TC-VNEXT-G03-TOP3-AUDIT
role: CONTRIBUTOR
baseline_commit: 8a33a4b22a405135f310376d8766d9170d80097d
registry_activation_commit: d4ce966de55b6d72e0b5daced9764223d7a6913a
```

产品差异始终与上述现场`origin/develop`基线比较。功能分支必须从包含本提示词及其SHA-256登记的binding commit创建。

## Branch and isolated worktree

```yaml
branch: codex/g03r-semantic
remote_branch: origin/codex/g03r-semantic
worktree_path: D:/munto/code/claudeProject/agentTravel-g03r-semantic
dialogue_ref: codex-task:g03r-semantic
```

写入前确认当前HEAD包含registry binding commit、工作树干净、分支和路径完全一致。不得复用根工作树或控制面工作树。

## Owned and forbidden paths

### owned_paths

- backend/app/trip_understanding/qwen_provider.py
- backend/app/trip_understanding/full_text.py
- backend/app/trip_understanding/pipeline.py
- backend/eval_data/trip_text_cards_agent_v2/qwen_inference_prompt.md
- backend/tests/test_g03r_trip_semantics.py

### forbidden_paths

- AGENTS.md
- CLAUDE.md
- docs/governance
- backend/app/db/migrations
- packages/trip-check-client/openapi.json
- packages/trip-check-client/openapi.current.json
- packages/trip-check-client/src/generated
- frontend/package-lock.json
- miniapp/package-lock.json
- packages/trip-check-client/package-lock.json
- y-websocket/package-lock.json
- backend/eval_data/agent_gate_v1/automation_runner_requirements.lock
- backend/eval_data/agent_gate_v1/automation_runner_browser_package-lock.json

只允许修改`owned_paths`。任何必要改动若落入禁止路径，停止该改动并在完成回报中交给主对话。

## User-observable outcome

- 真正计划地点保持原文日序、日内顺序和原子名称。
- 推荐、听说、经过、换乘、条件选择、明确排除只保留正确内部角色，不进入行程。
- 描述句、URL、电话、导航和预约说明不成为地点。
- POI解析调用集合与公共卡片集合使用同一资格规则且完全一致。

## Non-goals and locked contracts

- 不改变公共或内部schema、角色枚举、migration、OpenAPI、依赖或锁文件。
- 不改变exact model snapshot、temperature、7秒deadline、768 output tokens、并发1、最多一次schema repair或失败策略。
- 不读取、修改或运行sealed blind/oracle；历史裁决只作`DEVELOPMENT_DIAGNOSTIC`。
- 不激活G04，不扩展到FUX-03、H1、公网、生产、商业、发布、部署或`main`。
- 不增加运行时多Agent、Provider、数据权限、账号或费用。

## Dependencies, inputs and outputs

- Dependencies：`origin/develop@8a33a4b22a405135f310376d8766d9170d80097d`、既有`ActivityMention`五类角色、现有Qwen结构化输出schema及固定运行配置。
- Inputs：相同的`54 dev + 18 validation`非blind输入、既有历史非blind裁决和历史Qwen输出，仅用于逐case开发诊断。
- Outputs：三个运行时文件、Qwen语义提示词和一个独立测试文件中的最小修复。

不得扩大公共API、Provider、数据权限、依赖或产品范围。

## Acceptance and targeted verification

按三个独立因素实现并提交：

1. 日序与顺序：识别`第1天 / 第 2 天 / 第一天 / Day 3 / D4`；按最近前置标题确定日序；mention按原文span排序，再按日生成`sequence_index`。
2. 五类角色：元说明跳过；无条件取消`EXCLUDED`；条件选择`OPTIONAL`；仅经过/换乘`PASS_THROUGH`；推荐/听说/非本次安排`REFERENCE`；明确到访`PLANNED`。每个实际出现位置独立判断。
3. 原子边界与计划召回：仅接受原文逐字、URL外、合法原子地点；本地回退按局部子句、动作和并列词提取；同一个资格判断控制POI调用和公共卡片。

每个因素在同一72条输入上只比较一次；不满足门槛则创建可审计revert，最多两轮且第二轮必须采用不同局部策略。Provider超时或环境失败记`UNKNOWN`，不得概率性重跑。

验收门槛：禁入内容成为地点`0`；额外`PLANNED`地点`0`；计划原子召回`432/432`；角色`720/720`；日序及顺序`432/432`；结构化可比较结果`72/72`。

Required commands：

- `python -m pytest tests/test_g03r_trip_semantics.py tests/test_qwen_trip_understanding.py tests/test_trip_understanding_v3.py -q`
- `python -m evals.trip_text_cards_v1.validator`
- 每个单因素一次固定72条Qwen比较
- `python -m scripts.validate_core_mainline`

正式72条若因既有批准凭据未注入而无法运行，必须列为`NOT_RUN / BLOCKED_EXTERNAL`，不能报告`READY_TO_MERGE`。其他未运行检查也必须明确列为`NOT_RUN`。

## Git and authority restrictions

```yaml
must_not_merge: true
must_not_modify_goal_or_registry: true
must_not_create_numbered_migration: true
must_not_modify_shared_openapi_or_lockfiles: true
```

只可commit/push当前`codex/g03r-semantic`分支。不得修改Goal、binding、registry、官方Gate，不得rebase、squash、force-push或自行合并。完成时必须提供远端`remote readback`；若最终状态为`BLOCKED_EXTERNAL`，仍可提交并推送已验证的代码检查点。

## Subagent boundary

```yaml
subagent_read_only: true
```

子Agent只可做短期标注、独立复核、反方审查或故障诊断，不得拥有功能分支、提交产品代码或改变工作包状态。本次未要求使用子Agent。

## Completion report

严格返回：

```text
package: WP-G03R-SEMANTIC
branch: codex/g03r-semantic
remote_branch: origin/codex/g03r-semantic
worktree: D:/munto/code/claudeProject/agentTravel-g03r-semantic
baseline: 8a33a4b22a405135f310376d8766d9170d80097d
registry_activation_commit: d4ce966de55b6d72e0b5daced9764223d7a6913a
final_commit: <40-hex or NONE>
remote_readback: <40-hex and PASS, or exact failure>
changed_paths: <actual paths>
commands_and_results: <commands, exit status, counts>
checks_not_run: <NOT_RUN items>
known_risks_and_remaining: <items or NONE>
subagent_usage: NONE
status: READY_TO_MERGE | IN_PROGRESS | BLOCKED_EXTERNAL
status_reason: <why>
```

`READY_TO_MERGE`只表示向主对话请求验收。主对话复核路径、提交、测试、clean worktree和远端readback后才能登记；当前缺少正式72条凭据时必须使用`BLOCKED_EXTERNAL`。
