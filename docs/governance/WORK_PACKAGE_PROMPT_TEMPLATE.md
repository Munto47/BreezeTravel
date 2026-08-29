# Work Package Prompt v1

本模板由主对话框填写并提交。每个长期功能对话只接收一份完整提示词，只拥有一个登记工作包。占位符不得直接交付；文件使用UTF-8/LF，SHA-256登记到`current_work_packages.json`。

Goal激活commit先冻结公共合同、migration编号和product baseline；提示词及`WAITING_FOR_WRITER_SLOT`登记在后续控制面commit中。这样提示词可以精确引用已经存在的Goal激活commit，避免让Git commit自引用。功能分支从包含其完整prompt binding的registry commit创建，同时仍以提示词中的product baseline判断产品差异。

## Identity and exact baseline

```yaml
prompt_schema_version: work-package-prompt-v1
package_id: <WP-GNN-NAME>
goal_id: <TC-VNEXT-GNN-NAME>
role: CONTRIBUTOR
baseline_commit: <40-hex product baseline>
registry_activation_commit: <40-hex Goal activation commit>
registry_binding_commit: <创建分支前由主对话回填到交付消息，不写入本文件以避免commit自引用>
```

角色固定为独立功能对话。主对话框是唯一集成者；本对话不得承担第二个包。

## Branch and isolated worktree

```yaml
branch: codex/<package-branch>
remote_branch: origin/codex/<package-branch>
worktree_path: <absolute independent worktree path>
dialogue_ref: <user-visible Codex task reference, started时登记>
```

开始写入前运行工作包校验，确认当前HEAD包含registry binding commit、branch/worktree与登记一致且工作树干净。不得复用主worktree或其他功能包worktree。

## Owned and forbidden paths

### owned_paths

- <exact repository-relative path>

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

只允许修改`owned_paths`。如必要改动落在禁止路径，停止该改动并在回报中交给主对话框处理。

## User-observable outcome

- <用户完成什么、看到什么或可以直接采取什么动作>

## Non-goals and locked contracts

- <明确非目标>
- <不得重新决策的公共/内部接口、枚举、migration编号和失败语义>

## Dependencies, inputs and outputs

- Dependencies：<已冻结commit、schema、接口和上游包>
- Inputs：<本包读取的数据/合同>
- Outputs：<本包提供给下游的代码、类型或行为>

不得扩大公共API、Provider、数据权限、依赖或产品范围。

## Acceptance and targeted verification

- Acceptance：<逐条可观察验收标准>
- Required command：`<exact command>`
- Success condition：<退出码、测试数量或明确断言>
- Unrun checks：必须在完成回报中列为`NOT_RUN`，不得推断通过。

## Git and authority restrictions

```yaml
must_not_merge: true
must_not_modify_goal_or_registry: true
must_not_create_numbered_migration: true
must_not_modify_shared_openapi_or_lockfiles: true
```

只可commit/push当前工作包分支。不得修改Goal、binding、registry状态或官方Gate，不得自行合并、rebase、squash、force-push。完成时必须提供远端`remote readback`。

## Subagent boundary

```yaml
subagent_read_only: true
```

子Agent只可做短期标注、独立复核、反方审查或故障诊断，不得拥有功能分支、提交产品代码或改变工作包状态。子Agent发现的问题由本功能对话修复。

## Completion report

严格返回：

```text
package: <id>
branch: <local branch>
remote_branch: <remote branch>
worktree: <absolute path>
baseline: <40-hex>
registry_activation_commit: <40-hex>
final_commit: <40-hex or NONE>
remote_readback: <40-hex and PASS, or exact failure>
changed_paths: <actual paths>
commands_and_results: <commands, exit status, counts>
checks_not_run: <NOT_RUN items>
known_risks_and_remaining: <items or NONE>
subagent_usage: <purpose, output hash/evidence level, or NONE>
status: READY_TO_MERGE | IN_PROGRESS | BLOCKED_EXTERNAL
status_reason: <why>
```

`READY_TO_MERGE`只是功能对话请求。只有主对话复核路径、commit、测试、clean worktree和remote readback后，才能写入官方状态与`ready_commit`。冻结后的任何额外提交或脏文件都会使其失效。
