# Blueprint 1.0 只读验证合同

> Goal：`TC-BP-G00-BLUEPRINT`
>
> 状态：`ACTIVE`
>
> 说明：这些检查只证明文档结构和治理一致，不证明V0.1产品代码、Provider、浏览器、真人或生产能力。

## 输入集合

- `AGENTS.md`、`README.md`、`CLAUDE.md`；
- `docs/product/*.md`、`docs/ARCHITECTURE.md`；
- ADR-001～ADR-012；旧ADR必须标明Superseded/Partially Superseded并列出仍保留的不变量；
- `docs/governance/{PROGRAM,ROADMAP,RELEASE_GATES,CURRENT_GOAL,PROVIDER_ADMISSION,RISK_REGISTER}.md`；
- `docs/governance/goals/planned/TC-VNEXT-G01...G07`；
- 历史Current Goal与`origin/develop`；
- 旧证据入口的`NOT_VNEXT_AUTHORITY` banner。

## 必须执行的只读检查

在仓库根目录运行：

```powershell
git diff --check
git status --short
git diff --name-only origin/develop...HEAD
```

预期：差异只在`AGENTS.md`、`README.md`、`CLAUDE.md`和`docs/**`；产品代码、migration、测试、依赖锁文件为0。

结构检查：

```powershell
$required = @(
  'Dependencies','User Outcome','Scope','Non-goals','Authority','Baseline',
  'Invariants','Acceptance','Verification','Budget','Pre-approved actions',
  'HITL','Checkpoint ledger','Auto-advance','Stop conditions','Completion record'
)
Get-ChildItem docs/governance/goals/planned -File | ForEach-Object {
  $file = $_
  $text = [IO.File]::ReadAllText($file.FullName)
  $missing = @($required | Where-Object {
    $text -notmatch "(?m)^## $([regex]::Escape($_))(?:\s|$)"
  })
  if ($missing) { throw "$($file.Name): $($missing -join ', ')" }
}
$current = [IO.File]::ReadAllText((Resolve-Path docs/governance/CURRENT_GOAL.md))
if (([regex]::Matches($current,'(?m)^Status:\s*(APPROVED|IN_PROGRESS)\s*$')).Count -ne 1) {
  throw 'CURRENT_GOAL must contain exactly one active status'
}
```

历史完整性检查：

```powershell
$original = ((git show origin/develop:docs/governance/CURRENT_GOAL.md) -join "`n").TrimEnd("`n","`r")
$archive = ((Get-Content docs/governance/goals/completed/TC-INTAKE-CONFIRM-E2E-HOTFIX.md) -join "`n").TrimEnd("`n","`r")
if ($original -cne $archive) { throw 'historical Current Goal archive drifted' }
```

迁移/职责漂移检查至少确认：

- 029只属于G01，G02不得再次声称创建029；
- 031是G03必需的`day_index_trip_bridge`；
- `MapRenderSnapshot(QUEUED)`、公共`map_status=STALE`和HTTP `206 PARTIAL_RESULT`不存在；
- sealed blind不用于Max/Plus/Flash选择；
- 普通公共状态只使用用户语义枚举；
- 历史入口包含`NOT_VNEXT_AUTHORITY`或等价冻结标记。
- ADR-001～ADR-006不得以Accepted旧范围、旧阶段或旧Provider matrix覆盖Blueprint。

链接检查遍历上述Markdown中的相对链接，排除HTTP、mailto和页内anchor；解析目标必须存在。

## 历史兼容检查

```powershell
cd backend
python -m pytest tests/test_dual_entry_release_manifest.py `
  tests/test_trip_intake_migration_contract.py `
  tests/test_trip_check_migration_contract.py -q
```

这些测试只证明旧manifest关键词与001～027 migration兼容，必须记录为`LEGACY_COMPATIBILITY_PASS`，不能标记`BLUEPRINT_READY`。G07才更新新版manifest生成器并运行候选证据。

## 最终结果记录

| 检查 | 结果 | 绑定 |
|---|---|---|
| 文档only diff | PASS（产品代码、migration、测试、锁文件差异0） | pre-subject worktree |
| planned Goal完整字段 | PASS（G01～G07缺失字段0） | pre-subject worktree |
| 单一active Goal | PASS（G00=`IN_PROGRESS`） | pre-subject worktree |
| 历史Goal规范化一致 | PASS | `origin/develop@1c3adf3` |
| 相对链接 | PASS（34个权威/Goal文件，断链0） | pre-subject worktree |
| 旧目标/状态/migration漂移 | PASS（漂移失败0） | pre-subject worktree |
| 历史兼容pytest | `LEGACY_COMPATIBILITY_PASS`（12 passed） | pre-subject worktree |
| 独立二次复审 | PASS（产品、架构、反方/治理、商业P0=0、P1=0） | `BLUEPRINT_REVIEW_RESOLUTION.md` |
| subject push/readback | PENDING | remote branch |
| transition push/readback | `EXTERNAL_POST_COMMIT_CHECK` | remote branch |

Backend全量、Frontend build、PostgreSQL、Qwen、高德、天气、浏览器、H1、公网、生产、商业全部保持`NOT_RUN`，因为G00禁止产品实现与外部调用。
