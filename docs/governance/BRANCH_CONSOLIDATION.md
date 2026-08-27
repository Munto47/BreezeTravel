# BreezeTravel 分支与重要文件收口记录

> 日期：2026-08-27
>
> 状态：`CONSOLIDATION_SUBJECT_PREPARED / DEVELOP_READBACK_PENDING`
>
> 本文件只证明Git资产收口，不证明V0.1产品、Provider、浏览器、真人或生产能力。

## 1. 统一含义

本项目的“统一分支”定义为：

1. `origin/develop`保存所有已完成、仍适用于当前产品方向的重要资产；
2. 当前实现分支从该基线创建，并携带相同的根`AGENTS.md`和`docs/governance/CURRENT_GOAL.md`；
3. 旧实验、失败评测、过期Goal和未提交草稿保留为历史，但不得继续驱动开发；
4. 不force-push、不重写历史、不为表面统一删除远端历史分支；
5. `main`不在本次操作范围内。

## 2. 合并前基线

- 本地根`develop`：`d51d78fd004d46b105f05134c61d5fbee385c974`，只有根`miniapp/`生成物和`tests/`未跟踪；
- 远端`origin/develop`：`1c3adf3e61964501b670f68ca2fd1071918699d4`；
- Blueprint分支：`origin/codex/trip-check-product-reset@9c877c84026be3d84f9fdebfe06f557626c5cd04`；
- `origin/develop`是Blueprint分支祖先，可使用fast-forward，不需要冲突合并或历史改写。

## 3. 已在 develop 的重要资产

下列完成分支在审计时均为`origin/develop`祖先，patch缺口为0，不需要重复merge：

| 资产 | 已包含的分支/commit | 处置 |
|---|---|---|
| P0～P6可靠性与Candidate历史 | `codex/trip-check-p6-candidate-evidence`、`codex/trip-check-p6-governance-archive` | `KEEP_HISTORY / ALREADY_MERGED` |
| Taro小程序与共享客户端 | `codex/mobile-app@8ca445d` | `KEEP / ALREADY_MERGED` |
| Trip Intake v2 | `codex/trip-intake-v2@d967e77` | `KEEP_AS_REUSABLE_ASSET / ALREADY_MERGED` |
| Trip NLU v2 | `codex/trip-nlu-v2-optimization@c06f191` | `KEEP_AS_FROZEN_BASELINE / ALREADY_MERGED` |
| DeepSeek intake稳定性修复 | `codex/trip-intake-deepseek-stability@ec7b087` | `KEEP_AS_FROZEN_BASELINE / ALREADY_MERGED` |
| 本地旅行文本提交 | `1c3adf3` | 根`tests/`按项目所有者新指令删除，不再作为当前数据资产 |

Blueprint分支的三个提交是本次唯一需要快进到`develop`的已完成新增历史：

- `3fb3d05`：建立Blueprint 1.0；
- `f3b5f3e`：归档G00并激活G01；
- `9c877c8`：记录Blueprint transition回读。

本收口subject将在上述历史之后追加仓库基线规则、根`tests/`删除和最终审计记录。

## 4. 重要文件清单

- `backend/`：FastAPI、领域模型、migration、Provider适配、评测与自动化测试；
- `frontend/`：Next.js产品界面与浏览器测试；
- `miniapp/`：33个已跟踪Taro源码、配置、锁文件和`__test__`测试文件；
- `packages/trip-check-client/`：共享API合同与生成类型；
- `docs/`与根`AGENTS.md`：当前产品、架构和治理权威；
- `y-websocket/`及旧Planner/RAG等：仅作冻结兼容资产。

以下不是应提交的重要文件：

- `miniapp/node_modules/`；
- `miniapp/dist/`；
- `miniapp/.swc/`；
- 本地日志、缓存、密钥、数据库和浏览器产物。

它们由`.gitignore`排除。`miniapp/`被纳入Git指源码、配置、锁文件和测试，而不是依赖目录或构建输出。

## 5. 明确不合并的历史内容

| 分支/worktree | 审计结论 | 原因 |
|---|---|---|
| `origin/codex/test-itinerary-content-answers@23488ac` | `SUPERSEDED / DO_NOT_MERGE` | 唯一patch只是把旧测试答案Goal设为Current，会覆盖已批准G01；对应根`tests/`已由所有者要求删除 |
| `agentTravel-p5-v5-contract` | `UNCOMMITTED_HISTORICAL_WIP / FREEZE` | 10个未提交P5 v5草稿，旧Program已取代，不能冒充完成资产 |
| `agentTravel-p5-v5-runner-gate` | `UNCOMMITTED_HISTORICAL_WIP / FREEZE` | 27个未提交P5 v5草稿，历史Gate受阻，G07会按新合同重建 |
| 其余P5 v2/v3/v4与旧feature分支 | `HISTORICAL_READ_ONLY` | 已被后续合并历史或Blueprint取代；不作为新开发起点 |

上述worktree不在本次操作中删除或改写，避免损坏历史草稿；但它们不属于统一开发基线。

## 6. 根 tests 删除边界

只删除仓库根`tests/`旅行文本草稿：19个城市文本、两个README（远端版本共21个文件；本地根目录有20个文件）。

不删除：

- `backend/tests/`；
- `miniapp/__test__/`；
- frontend/Playwright或其他包内自动化测试；
- sealed数据、已提交历史receipt或completed Goal。

仓库版本可以从Git历史恢复；本地根`tests/答案/README.md`存在未提交差异，按项目所有者明确删除指令不做保留。

## 7. 合并与回读合同

1. 在`codex/trip-check-product-reset`提交本收口subject；
2. 验证所有变更、根`tests/`删除、miniapp追踪清单、单一Goal和相关构建；
3. push并回读subject；
4. 仅当`origin/develop`仍是subject祖先时执行fast-forward；
5. 回读`origin/develop`的tree、`AGENTS.md`、`CURRENT_GOAL.md`、miniapp清单和根`tests/`不存在；
6. 将本地根`develop`安全fast-forward，删除本地根`tests/`，确认生成物被忽略；
7. 追加exact commit/readback receipt，使`develop`与当前实现分支树一致。

## 8. 合并前验证与已知基线失败

已通过：

- backend Ruff；
- frontend生产构建；
- `packages/trip-check-client` typecheck与build；
- miniapp typecheck、4个suite/7个test和微信构建；
- dual-entry结构校验：`structurally_valid=true`，同时明确`release_ready=false`；
- 12项历史manifest/migration兼容检查；
- Goal字段、单一active Goal、文档链接、根tests删除清单和miniapp 33文件清单。

backend全量结果为`2017 passed, 32 skipped, 3 failed`。三个失败都位于未被本次改动触及的旧Trip NLU冻结资产：

1. remediation JSONL缺少独立LF属性，在Windows checkout发生字节漂移；本subject补充`.gitattributes`规则，不改数据内容或期望hash；
2. 另外两项是旧candidate manifest绑定的validator/scorer/gate代码hash与当前历史代码不一致；LF隔离checkout仍为`21 passed, 3 skipped, 2 failed`；
3. `git diff origin/develop -- backend`为0，证明这些不是本次分支/文件收口引入的产品代码回归。

本次不更新candidate manifest、不修改sealed blind/oracle，也不把失败标成PASS。该绑定漂移作为G01首个preflight的历史基线风险处理；若修复必须修改冻结manifest/oracle，仍按HITL请求批准。

## 9. 完成记录

- Consolidation subject commit：`PENDING`；
- Canonical `origin/develop`：`PENDING`；
- Active implementation branch：`origin/codex/trip-check-product-reset`；
- Remote tree equality：`PENDING`；
- Root `tests/` tracked count：目标`0`；
- Root `tests/` filesystem count：目标`0`；
- `miniapp/` tracked source/config/test count：目标`33`；
- Single active Goal：目标`TC-VNEXT-G01-TEXT-CARDS / APPROVED`；
- `main`：`NOT_TOUCHED`；
- Product/runtime capability：`NOT_CHANGED`。
