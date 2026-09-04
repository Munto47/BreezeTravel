# WP-G07-TEXT-CONVERGENCE：文字语义与40条兼容包

## Identity and exact baseline

```yaml
prompt_schema_version: work-package-prompt-v1
package_id: WP-G07-TEXT-CONVERGENCE
goal_id: TC-VNEXT-G07-CANDIDATE
role: CONTRIBUTOR
baseline_commit: 0b2f098d9209d2ccb31f3c4308ac3ab33ebc9671
registry_activation_commit: 97a09f41b7eacbb21210d62c148bb46e07385f2b
```

你是独立文字语义功能对话。主对话是唯一集成者；本对话只拥有这一项工作包，不得承担前端合流、正式候选执行或第二个功能包。`registry_binding_commit`由主对话在启动消息中提供，不写入本文件以避免提交自引用。

## Branch and isolated worktree

```yaml
branch: codex/g07-text-convergence-r1-fix
remote_branch: origin/codex/g07-text-convergence-r1-fix
worktree_path: C:/Users/18770/.codex/worktrees/d597/BreezeTravel
dialogue_ref: codex-task:01a06b47-9caf-7f83-a882-9dcff7bd51a9
```

开始写入前必须确认主对话发送的`registry_binding_commit`和`branch_point_commit`都已经成为当前HEAD历史，其中当前HEAD必须精确等于`branch_point_commit`；当前分支、上游、worktree与registry完全一致且工作树干净。随后在`backend`运行`python -m scripts.validate_work_packages --package-id WP-G07-TEXT-CONVERGENCE`，只有PASS才可写入。任一不一致时只读回报，不能自行rebase、改registry或换工作树。

## Owned and forbidden paths

### owned_paths

- `backend/app/trip_understanding/full_text.py`
- `backend/app/trip_understanding/pipeline.py`
- `backend/app/trip_understanding/qwen_provider.py`
- `backend/eval_data/g07_text_convergence_v1/compatibility_cases.schema.json`
- `backend/eval_data/g07_text_convergence_v1/compatibility_cases.json`
- `backend/evals/g07_text_convergence_v1/__init__.py`
- `backend/evals/g07_text_convergence_v1/runner.py`
- `backend/tests/test_g07_text_convergence.py`

### forbidden_paths

- `AGENTS.md`
- `CLAUDE.md`
- `docs/governance`
- `docs/product`
- `backend/app/db/migrations`
- `backend/app/trip_understanding/models.py`
- `backend/app/trip_understanding/repository.py`
- `backend/app/trip_understanding/map_repository.py`
- `backend/app/trip_understanding/stay_repository.py`
- `backend/app/trip_understanding/service.py`
- `backend/app/trip_understanding/worker.py`
- `backend/eval_data/trip_text_cards_v1/frozen_blind.inputs.jsonl`
- `backend/eval_data/agent_gate_v1`
- `frontend`
- `miniapp`
- `packages/trip-check-client`
- `frontend/package-lock.json`
- `miniapp/package-lock.json`
- `packages/trip-check-client/package-lock.json`
- `y-websocket/package-lock.json`

只允许修改owned_paths。若正确实现确实需要禁止路径、公共schema、migration或新增依赖，停止该部分并在回报中说明；不要扩大路径或用测试绕过。

## User-observable outcome

用户粘贴普通中文攻略后，系统保守地生成逐日地点卡片：时间、时长、讲解、拍照、旧名、入口出口、设施、URL和说明句不会冒充地点；下午/晚上时刻能正确显示；取消、恢复、备选和改期不会被过度自动确认。不确定内容可以遗漏或待确认，但不能制造地点。

## Versioned 40-case contract

新兼容包必须是公开、非blind、可读、版本化的JSON，并为每条记录固定`case_id`、`group`、`input_text`、`lane`、`safe_expectation`、`exact_expectation`、`severity`和可机器判定的期望字段。不得读取、枚举内容或派生任何sealed/frozen blind输入、真值或输出。40条编号和边界固定如下：

### BT-COMPAT-001～010：结构、角色与日期

1. `001`识别`第1天/第 2 天/第三天/Day 4/D5`并保持日内原文顺序。
2. `002`原文先写Day 2再写Day 1，仍按标签归日且输出日序升序。
3. `003`常见箭头`→/⇒/->/➜/⇨/＞/➔/➡/⟶`只作地点分隔。
4. `004`紧凑全角格式`城市；D1｜A→B／C；D2｜D、E`不丢节点或日序。
5. `005`区分`PLANNED/OPTIONAL/REFERENCE/EXCLUDED/PASS_THROUGH`；只有原子`PLANNED`进入地点搜索和公开卡片。
6. `006`“如果还有时间去X”保持OPTIONAL且不自动建卡。
7. `007`“A或B/二选一”保持待确认，不生成两张确定卡。
8. `008`URL、预约说明、电话、元指令和描述句不成地点。
9. `009`“故宫介绍，旧名/旧称/曾称紫禁城”不得生成“故宫介绍”或“紫禁城”假卡。
10. `010`“故宫讲解→景山公园拍照”只保留真实地点；裸入口、出口、公厕、停车场、充电站不成卡。

### BT-COMPAT-011～020：时间、取消与改期

11. `011`“上午去故宫”得到`time_hint=上午`。
12. `012`“14:00去故宫”得到`14:00`。
13. `013`“下午2点去故宫”得到`14:00`。
14. `014`“晚上7:30去故宫”得到`19:30`而非`7:30`。
15. `015`“去故宫10:00到达”得到地点“故宫”和时间`10:00`，不得生成“故宫10”。
16. `016`“故宫→下午2点到达景山”只把`14:00`归给景山。
17. `017`“故宫游览2小时”不得把“2小时”生成地点；本轮不得新增或伪造持久化时长字段。
18. `018`开放时间、排队时长和两地交通耗时不得冒充到访时间或游览时长。
19. `019`普通“后来改到/调整为”或多候选保持待确认；只有明确`最终/最后/到最后/末了`且唯一值才可自动采用。
20. `020`“不得不取消”是真取消；“并不取消/没有取消/不打算撤掉”不是取消；明确最终恢复原方案时恢复，重复到访保持独立且不能互相误删。额外固化两条反例：`Day 1 去故宫；Day 2 不去故宫，改去景山`不得删掉 Day 1；同日上午、下午两次同名到访后取消其中一次，只能影响有明确时间或次序归属的那一次；无法安全绑定时宁可保留原计划并待确认，不得按地点名全局撤销。

### BT-COMPAT-021～028：城市、地点与类别安全

21. `021`北京、上海、杭州只有同城唯一且类别一致才可自动匹配。
22. `022`深圳等非深度城市只生成基础卡片，不暗示同等级Provider核验。
23. `023`Provider返回错城，即使同名也必须待确认。
24. `024`city/province/adcode缺失、类型错误或矛盾时必须待确认。
25. `025`typecode或地点类别与语义不一致时不得自动匹配。
26. `026`配套厕所、停车场、充电设施不得冒充同名主景点；裸设施不得进入resolver。
27. `027`明确午餐/餐饮语境不得匹配同名酒店。
28. `028`多城文本只有一个城市唯一成功时才可采用；多解或不确定时保守待确认。

### BT-COMPAT-029～034：Revision、地图与Provider状态

29. `029`首次卡片完成后只创建并执行一个后台地图job，相邻点同时比较walking/transit。
30. `030`编辑创建新revision、旧地图变`NEEDS_UPDATE`且路线Provider调用为0。
31. `031`只有手动更新才重算地图，请求幂等键与逻辑键均防重复调用。
32. `032`迟到旧revision或旧lease结果不能覆盖当前地图。
33. `033`inference/place/route的UNKNOWN或UNAVAILABLE不得显示成功，并保留真实已成功部分。
34. `034`walking与transit差值不超过10分钟选walking；不得产生默认driving。

### BT-COMPAT-035～040：隐私与普通用户体验

35. `035`公共API投影无span、offset、UID、hash、revision、receipt、模型或Provider内部字段。
36. `036`ETag不透明、匿名secret只在HttpOnly cookie、capability和真实路径不进日志。
37. `037`删除原文后卡片仍可读且原文不可恢复。
38. `038`删除行程或账户后资源与分享能力失效，未完成不得显示成功。
39. `039`待确认、未找到和Provider不可用使用中性非红状态且不显示底层错误。
40. `040`首页直接粘贴文字、无前置表单；软假设可编辑；先出卡片、地图后台准备并只手动更新。

`029～040`主要是把既有合同纳入统一runner，不迁移TripCheck实现，也不为通过而重写现有地图、权限、删除或前端逻辑。`017～020`在无新字段/migration条件下无法精确表达的部分必须记为安全降级，不能把时长塞入`time_hint`、路线时长或建议字段。

## 文字兼容子门与整体R0边界

- `TEXT_COMPAT_R0`：40/40均无危险输出；至少28/40自动精确符合；所有P0安全断言通过。
- `TEXT_COMPAT_R1`：至少36/40自动精确符合；其余只能是明确记录的安全降级。
- `TEXT_COMPAT_R2`：40/40精确通过；本工作包不宣称整体R0、R1、R2或G07通过。
- 项目整体`R0`还必须由后续UI工作包和集成者验证桌面/手机主路径，以及“卡片→编辑→地图需要更新→手动更新→Top-3”闭环；本runner的`overall_delivery_level`固定输出`NOT_RUN`。
- 危险输出包括：错城/错类别/描述句/URL/动作/时间/时长/设施自动确认成地点，泄漏内部字段，把UNKNOWN/UNAVAILABLE算成功，编辑触发路线Provider，或用伪事实补齐结果。
- 召回与边缘语法可以在一次初测和最多两轮不同策略后降级；precision与上述安全底线不得降低。两轮后若仅有可安全遗漏项未达标，诚实回报实际`TEXT_COMPAT_R0/R1`；不得继续堆叠通用正则或治理设施。

## Implementation constraints

- 先固化40条兼容包和可复现初测，再改运行时代码；不得先改期望追绿。
- 旧提交`51a49ca8d610bba73b6dd27fede9704c6f7b3525`只作被集成者拒绝的诊断资产。可在新分支用`git cherry-pick --no-commit`带入其改动，但必须修复跨日/重复到访误撤销后形成一个新的唯一功能提交；不得修改、覆盖或强推旧远程分支。
- 三层防线都要验证：deterministic fallback候选切分、Qwen输出后处理、`is_atomic_planned_place()`最终入口。
- 时间解析只更新现有`time_hint`，支持上下午/晚上换算和前后置本地归属；不改变Qwen私有wire schema。
- 不复制TripCheck的`parser.py`、`qwen.py`或大段正则；只迁移已验证行为和测试意图。
- 不新增时长字段、公共API、OpenAPI、命令、ETag、migration、共享客户端类型、依赖、Provider或城市深度能力。
- 不调用真实Qwen、高德或外部Provider；正式live、blind、浏览器与H1均由集成者后续处理。
- 不涉及OCR、截图上传、图片理解、RAG、Yjs、旧房间、旧Planner或运行时多Agent。

## Acceptance and targeted verification

按以下顺序验证：

1. 新compat runner必须输出40条逐例状态、`exact_pass_count`、`safe_pass_count`、`hard_safety_failure_count`、文字兼容子门等级与安全降级清单；`TEXT_COMPAT_R0`条件为`40 / >=28 / 0`，`overall_delivery_level`必须保持`NOT_RUN`。
2. 精确回归：`python -m pytest -q tests/test_g07_text_convergence.py`。
3. 现有语义/地点/地图与公开边界回归：`python -m pytest -q tests/test_g03r_trip_semantics.py tests/test_qwen_trip_understanding.py tests/test_trip_understanding_v3.py tests/test_g03r_place_resolution.py tests/test_amap_trip_understanding.py tests/test_g02_map_stay.py tests/test_trip_understanding_v3_api.py --disable-warnings`。
4. 代码检查：`python -m ruff check app/trip_understanding/full_text.py app/trip_understanding/pipeline.py app/trip_understanding/qwen_provider.py evals/g07_text_convergence_v1 tests/test_g07_text_convergence.py`。
5. 主线与差异：在`backend`运行`python -m scripts.validate_core_mainline`，再运行`git diff --check`与`git status --short`。

目标是尽量达到`TEXT_COMPAT_R1`，但只要`TEXT_COMPAT_R0`真实通过且最多两轮策略已用尽，可请求`READY_TO_MERGE`并如实列出未达文字子门R1的项目。不得宣称项目整体R0。任何P0安全失败、少于28条精确通过、现有定向回归失败、越界路径或脏工作树均不能请求合并。

## Git and authority restrictions

```yaml
must_not_merge: true
must_not_modify_goal_or_registry: true
must_not_create_numbered_migration: true
must_not_modify_shared_openapi_or_lockfiles: true
```

只可commit/push`codex/g07-text-convergence-r1-fix`。`branch_point_commit`之后必须恰好形成一个最终功能commit，且该commit的直接父提交必须等于主对话发送的`branch_point_commit`；实现期间不要创建中间commit。不得合并、rebase、squash、amend已发送提交、force-push、改变Goal/registry/Gate或删除历史证据。完成时提供`git ls-remote --heads origin refs/heads/codex/g07-text-convergence-r1-fix`的远端精确readback。

## Subagent boundary

```yaml
subagent_read_only: true
```

子Agent只可做短期标注、独立复核、反方审查或故障诊断，不得提交产品代码或改变工作包状态。

## Completion report

严格返回：

```text
package: WP-G07-TEXT-CONVERGENCE
branch: codex/g07-text-convergence-r1-fix
remote_branch: origin/codex/g07-text-convergence-r1-fix
worktree: C:/Users/18770/.codex/worktrees/d597/BreezeTravel
baseline: 0b2f098d9209d2ccb31f3c4308ac3ab33ebc9671
registry_activation_commit: 97a09f41b7eacbb21210d62c148bb46e07385f2b
registry_binding_commit: <received from integrator>
branch_point_commit: <received from integrator>
final_commit: <40-hex or NONE>
remote_readback: <40-hex and PASS, or exact failure>
compatibility_result: <safe/40, exact/40, hard safety failures, TEXT_COMPAT_R0/R1; overall_delivery_level=NOT_RUN>
repair_strategies_used: <initial measurement plus at most two distinct strategies>
changed_paths: <actual paths>
commands_and_results: <commands, exit status, counts>
checks_not_run: <NOT_RUN items>
known_risks_and_remaining: <items or NONE>
subagent_usage: <purpose and evidence, or NONE>
status: READY_TO_MERGE | IN_PROGRESS | BLOCKED_EXTERNAL
status_reason: <why>
```

`READY_TO_MERGE`只是请求。只有主对话复核路径、commit、测试、clean worktree和remote readback后，才能登记官方状态。
