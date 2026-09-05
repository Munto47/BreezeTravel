# BreezeTravel 工作区交接检查点（2026-09-02）

状态：`REMOTE_HANDOFF_CHECKPOINT`

本文件只用于跨电脑恢复开发上下文，不是候选通过、发布、部署或`main`合并证明。

## 1. 权威远端位置

- 仓库：`https://github.com/Munto47/BreezeTravel.git`
- 交接分支：`codex/workspace-handoff-20260902`
- 交接父提交：`71b8513d4dcdc61e585e1bee6c02ce004a6ee0ac`
- 原候选分支：`origin/codex/g07-candidate@71b8513d4dcdc61e585e1bee6c02ce004a6ee0ac`
- 集成基线：`origin/develop@ff36a10ecae98088742e9722da3f4bf3676f6d04`
- 当前Goal：`TC-VNEXT-G07-CANDIDATE / IN_PROGRESS`

交接分支以原候选停止点为父提交，只增加交接文档、修正README的过期状态说明，并把根工作区中用户保留的“真人门禁”约束合并到最新`AGENTS.md`。它没有修改产品运行时代码，也没有使既有候选证据升级或失效声明变为PASS。

## 2. 当前真实状态

- G01～G06已经归档并进入`develop`。
- G07自动、真实Provider、浏览器和性能组件曾分别取得证据，但第三轮final fresh panel在`252f43d`接受4个当前范围P1，候选结论为FAIL。
- `71b8513`已把该失败、fail-closed拒绝和停止条件写入`CURRENT_GOAL.md`、工作包登记表和耐久回执。
- 当前停止条件禁止自行启动第四轮产品修复、第四轮复审、sealed blind或最终候选PASS生成。
- H1、公网、生产、商业、发布、部署、release和`main`合并仍为`NOT_RUN`或未请求。
- 如果项目所有者要继续候选修复，必须先明确批准新的候选周期或修改停止规则；交接本身不构成该批准。

## 3. 另一台 Windows 电脑的精确恢复步骤

```powershell
git clone https://github.com/Munto47/BreezeTravel.git
Set-Location BreezeTravel
git fetch --all --prune
git switch -c codex/workspace-handoff-20260902 --track origin/codex/workspace-handoff-20260902
git status --short --branch
git log -1 --format='%H %s'
git ls-remote origin refs/heads/codex/workspace-handoff-20260902 refs/heads/codex/g07-candidate refs/heads/develop
```

恢复后应满足：工作树为空；本地`HEAD`与`refs/heads/codex/workspace-handoff-20260902`的远端回读一致；原候选分支仍精确指向`71b8513`，`develop`仍精确指向`ff36a10`。

继续任何修改前，依次阅读：

1. `AGENTS.md`
2. `docs/product/PROJECT_CHARTER.md`
3. `docs/governance/CURRENT_GOAL.md`
4. `docs/governance/current_goal_binding.json`
5. `docs/governance/current_work_packages.json`
6. `docs/governance/gate-results/G07.final-panel-stop-checkpoint.json`

交接分支是运输和阅读入口。若项目所有者批准新的候选周期，应按批准后的Goal合同从fresh远端基线建立新的`codex/`实现分支，不要把交接分支当作已经通过Gate的候选subject。

## 4. 本地环境恢复

仓库不保存真实密钥。请通过安全渠道重新取得原电脑的开发凭据，或在新电脑上重新填写；不要把真实值提交到Git。

```powershell
Copy-Item .env.example .env
Copy-Item frontend/.env.local.example frontend/.env.local
docker compose up -d --build

Push-Location frontend
npm ci
npm run build
Pop-Location
```

默认`AMAP_MOCK=true`和fixture路径只用于本地演示，不能当作真实Provider或候选Gate证据。需要真实Provider验证时，仍须遵守当前Goal的授权、固定配置和证据绑定。

## 5. 明确没有上传的本机内容

- `.env`和任何API密钥、账号令牌或真实凭据；
- `.venv/`、`node_modules/`、`.local-artifacts/`、`.playwright-mcp/`及缓存；
- 两个历史P5-v5工作树中的未跟踪blind/evidence-custody草稿。

最后一项属于旧P5实验，不是当前G07续工依赖，并且仓库是公开仓库；为避免泄露blind材料或把未验证草稿伪装成正式项目资产，本交接没有上传这些文件。

## 6. 本交接的验证边界

- 原候选停止点的远端存在性与哈希：需要fresh `ls-remote`回读；
- 交接提交的差异审查：只允许`AGENTS.md`、`README.md`和本文件；
- 产品运行时测试：`NOT_RUN`，因为本交接不改运行时代码；
- 原候选自动/live/browser/panel状态：只按`71b8513`已有回执陈述，不迁移成新交接提交的PASS。
