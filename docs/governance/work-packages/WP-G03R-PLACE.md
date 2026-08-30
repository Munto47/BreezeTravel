# Work Package Prompt v1

本包只实施所有者已批准的“三城900地点词典 + 保守高德解析”P1返修。主对话是唯一集成者；本包不得修改Goal、binding、registry或其他治理状态，不得自行合并。任何产品写入都必须发生在本提示词及其SHA-256已由binding commit登记、且本包分支/工作树从该提交创建之后。

## Identity and exact baseline

```yaml
prompt_schema_version: work-package-prompt-v1
package_id: WP-G03R-PLACE
goal_id: TC-VNEXT-G03-TOP3-AUDIT
role: CONTRIBUTOR
baseline_commit: 8a33a4b22a405135f310376d8766d9170d80097d
registry_activation_commit: d4ce966de55b6d72e0b5daced9764223d7a6913a
```

产品差异始终与上述现场`origin/develop`基线比较。功能分支必须从包含本提示词及其SHA-256登记的binding commit创建；binding commit之前不得写入任何`owned_paths`。

## Branch and isolated worktree

```yaml
branch: codex/g03r-place-resolution
remote_branch: origin/codex/g03r-place-resolution
worktree_path: D:/munto/code/claudeProject/agentTravel-g03r-place-resolution
dialogue_ref: codex-task:01a0509e-f5d1-71c1-9118-597e2c0d5d35
```

写入前确认当前HEAD包含registry binding commit、工作树干净、分支和路径完全一致。不得复用根工作树、控制面工作树或语义工作树。

## Owned and forbidden paths

### owned_paths

- backend/app/trip_understanding/three_city_place_lexicon_v1.jsonl
- backend/app/trip_understanding/_three_city_place_lexicon.py
- backend/app/trip_understanding/amap_place.py
- backend/app/constraints/amap_types.py
- backend/tests/test_g03r_place_lexicon.py
- backend/tests/test_g03r_place_resolution.py
- backend/tests/test_amap_trip_understanding.py

### forbidden_paths

- AGENTS.md
- CLAUDE.md
- docs/governance
- backend/app/api
- backend/app/db
- backend/app/config.py
- backend/app/trip_understanding/models.py
- backend/app/trip_understanding/worker.py
- backend/app/trip_understanding/qwen_provider.py
- backend/app/trip_understanding/full_text.py
- backend/app/trip_understanding/pipeline.py
- backend/eval_data/trip_text_cards_agent_v2/qwen_inference_prompt.md
- backend/eval_data/trip_text_cards_agent_v2/qwen_inference_config.json
- packages/trip-check-client/openapi.json
- packages/trip-check-client/openapi.current.json
- packages/trip-check-client/src/generated
- frontend
- frontend/package-lock.json
- miniapp/package-lock.json
- packages/trip-check-client/package-lock.json
- y-websocket/package-lock.json
- backend/eval_data/agent_gate_v1/automation_runner_requirements.lock
- backend/eval_data/agent_gate_v1/automation_runner_browser_package-lock.json

只允许修改`owned_paths`。公共API、数据库、模型配置和语义文件均在本包权限之外；任何必要改动若落入禁止路径，停止该改动并在完成回报中交给主对话。

## User-observable outcome

- 北京、上海、杭州攻略中的常见景点、场馆、公园、街区、交通枢纽、餐饮和酒店名称能以更准确的检索词进入高德解析。
- 地名别称、同名地点、分店、航站楼、校区、数字、方向和括号限定不会因过度归一化而误配。
- 错城、错类别、行政区矛盾、同层多候选、Provider不可用或字段不足时保留“地点待确认”，绝不为了覆盖率自动选择错误地点。
- 词典只帮助形成检索词和约束；普通用户最终看到的地点身份仍必须由实时高德结果确认。

## Non-goals and locked contracts

- 不修改公共API、OpenAPI、数据库、migration、模型schema、模型配置、语义提示词、依赖或锁文件。
- 不把词典变成POI事实库；不得保存或提交高德POI ID、坐标、地址、电话、营业时间、价格、评分、房态或原始响应。
- 不使用高德搜索结果反向构造词典，不使用模型生成未经来源核验的别名，不抓取小红书。
- 不新增Provider、账号、费用、生产调用、公共数据服务、运行时多Agent或新架构组件。
- 不读取、修改或运行sealed blind/oracle；不激活G04、FUX-03、H1、公网、生产、商业、发布、部署或`main`。

## Dependencies, inputs and outputs

- Dependencies：`origin/develop@8a33a4b22a405135f310376d8766d9170d80097d`、现有高德地点搜索客户端、现有`PlaceResolution`和`amap_types`内部契约。
- Candidate sources：Wikidata的CC0结构化记录只作候选发现；每条地点名称、别名、类别与城市归属还须由政府、景区/场馆、交通运营方或品牌官方网站等权威页面核验。
- Lexicon output：版本化JSONL共900条，北京/上海/杭州各300条；每城固定为210条景点/场馆/公园/街区、60条交通枢纽、15条餐饮、15条酒店。
- Record fields：仅允许稳定内部ID、城市、规范名称、安全别名、内部类别、可选行政区、来源和核验日期。不得加入本节Non-goals中列出的高德事实字段。
- Runtime output：私有加载器、保守候选选择逻辑、内部类型和独立离线回归；不产生公共schema变化。

词典按文件版本管理，至少按季度以新版本审查更新；不得原地静默替换已发布版本。缺失、损坏或不合法的词典必须退化为现有严格实时搜索并保持待确认边界，不得让服务启动失败或错误自动匹配。

## Two serial commits

本包只能按以下顺序形成两个可独立回滚的产品提交，不得把两步压缩、squash或倒序：

1. `feat(g03): add versioned three-city place lexicon`
   - 只修改`three_city_place_lexicon_v1.jsonl`、`_three_city_place_lexicon.py`和`test_g03r_place_lexicon.py`。
   - 完成900条配额、字段白名单、来源、稳定排序、唯一ID、词典加载、城市内零/一/多候选和缺失/损坏安全退化测试。
2. `fix(g03): make amap place resolution conservative`
   - 只修改`amap_place.py`、`amap_types.py`、`test_g03r_place_resolution.py`和必要的既有`test_amap_trip_understanding.py`回归。
   - 接入只读私有加载器，实现分层候选、城市/行政区/类别交叉校验和唯一候选选择；不改变第一提交的词典事实。

每个提交分别运行其定向测试并回读提交路径；第二提交再运行本包全部离线测试与G01/G03受影响回归。

## Lexicon and normalization contract

- 加载器只在输入城市内检索，返回零、一个或多个候选；不得跨城自动消歧。
- 归一化只允许Unicode NFKC、大小写统一、空白和常见分隔符整理；必须保留数字、方向、分店、航站楼、校区和括号限定。
- 仅在完整后缀等价比较中允许去除以下白名单后缀：`博物馆、博物院、美术馆、纪念馆、科技馆、图书馆、展览馆、艺术馆、体育馆、体育场、风景区、景区`。
- 绝不单独删除`馆、院、店、站、园`等单字，也不得以模糊包含、编辑距离或模型猜测扩展别名。
- 候选层级严格为：规范名称精确匹配 > 安全别名精确匹配 > 完整白名单后缀等价。只评估最高有效层；低层不得打破同层冲突。

## Conservative AMap resolution contract

- 词典最多重写高德文本检索词，并提供内部类别及可选行政区一致性约束；词典本身永远不能生成“已解析”身份。
- 每一层候选按高德POI ID去重；只有最高有效层恰好一个通过全部校验的候选时才可自动选择。
- 北京、上海按直辖市行政层级处理；省、市、区或adcode与输入城市/词典约束矛盾时拒绝候选。词典行政区仅作返回结果一致性检查，不作为高德请求参数。
- 高德typecode映射与文本类别映射必须分别判断；两者冲突、语义类别与词典/Provider类别冲突、类别未知或字段不足时均为`UNKNOWN`并保留待确认。
- Provider不可用、超时、无结果、响应缺字段、跨城、错类别、行政区矛盾或同层多候选，均不得自动解析。
- 词典外地点继续走严格实时高德搜索；词典命中但未被高德确认的地点也不得标记为已解析。

## Acceptance and targeted verification

离线验收必须覆盖：

- 900条总量与三城/四类配额精确匹配，JSONL schema、字段白名单、来源、核验日期、稳定ID、稳定排序和禁止字段全部通过。
- 归一化正例与反例覆盖数字、方向、分店、航站楼、校区、括号限定、白名单完整后缀和禁止删除单字。
- 规范名/别名/后缀三层优先级、同层冲突、POI ID去重和低层不得破坏高层冲突全部通过。
- 错城、错类别、行政区/adcode矛盾、typecode与文本类别冲突、Provider失败/无结果/缺字段全部保持待确认。
- 词典外地点仍调用实时搜索；词典命中但Provider未确认不得解析；既有G01文本卡片和G03地点流程不回退。

Required commands（从`backend`执行）：

- `python -m pytest tests/test_g03r_place_lexicon.py -q`
- `python -m pytest tests/test_g03r_place_resolution.py tests/test_amap_trip_understanding.py -q`
- `python -m pytest tests/test_g03r_place_lexicon.py tests/test_g03r_place_resolution.py tests/test_amap_trip_understanding.py tests/test_g01_delivery_samples.py tests/test_trip_understanding_v3.py tests/test_trip_understanding_v3_api.py tests/test_amap_route_trip_understanding.py -q`
- `python -m ruff check app/trip_understanding/_three_city_place_lexicon.py app/trip_understanding/amap_place.py app/constraints/amap_types.py tests/test_g03r_place_lexicon.py tests/test_g03r_place_resolution.py tests/test_amap_trip_understanding.py`
- `python -m scripts.validate_core_mainline --base-ref 8a33a4b22a405135f310376d8766d9170d80097d --head-ref HEAD`

本贡献包不得运行正式live AMap矩阵。贡献包达到全部离线门槛后可报告`READY_TO_MERGE`，并把live AMap明确列为`NOT_RUN / INTEGRATOR_ONLY`。主对话只在clean且已提交、推送并完成remote readback的精确候选上运行一次live矩阵；仅在已证明账户配额内零增量费用时执行，最多2500次调用。后续live门槛固定为严重错误`0`、precision `>=99%`、三城coverage `>=80%`，并按城市、类别、UNKNOWN、失败和延迟披露。

## Git and authority restrictions

```yaml
must_not_merge: true
must_not_modify_goal_or_registry: true
must_not_create_numbered_migration: true
must_not_modify_shared_openapi_or_lockfiles: true
```

只可commit/push当前`codex/g03r-place-resolution`分支。不得修改Goal、binding、registry、官方Gate，不得rebase、squash、force-push或自行合并。完成时必须提供远端`remote readback`；任何未运行检查必须明确列为`NOT_RUN`，外部权限/配额/服务失败必须列为`BLOCKED_EXTERNAL`而不是成功。

## Subagent boundary

```yaml
subagent_read_only: true
```

子Agent只可做来源候选整理、独立复核、反方审查或故障诊断，不得拥有功能分支、提交产品代码、修改词典事实或改变工作包状态。最终900条来源与所有产品改动由当前地点开发对话负责复核、提交和说明。

## Completion report

严格返回：

```text
package: WP-G03R-PLACE
branch: codex/g03r-place-resolution
remote_branch: origin/codex/g03r-place-resolution
worktree: D:/munto/code/claudeProject/agentTravel-g03r-place-resolution
baseline: 8a33a4b22a405135f310376d8766d9170d80097d
registry_activation_commit: d4ce966de55b6d72e0b5daced9764223d7a6913a
binding_commit: <40-hex supplied by integrator>
commit_1: <40-hex or NONE>
commit_2: <40-hex or NONE>
remote_readback: <40-hex and PASS, or exact failure>
changed_paths: <actual paths>
commands_and_results: <commands, exit status, counts>
checks_not_run: <NOT_RUN items>
known_risks_and_remaining: <items or NONE>
subagent_usage: <read-only use or NONE>
status: READY_TO_MERGE | IN_PROGRESS | BLOCKED_EXTERNAL
status_reason: <why>
```

`READY_TO_MERGE`只表示向主对话请求验收。主对话复核两个串行提交、路径、900条来源与配额、测试、clean worktree和remote readback后才能串行集成；贡献包不得用live AMap未运行作为虚假PASS，也不得自行改变registry状态。
