# 现有公网服务的升级和恢复

本工具不复跑首发部署，不删除旧库，不把预发测试数据提升为生产数据。以下步骤只有在产品验收完成、资源检查满足后才实际执行；省略 `--execute` 均为只读计划。

## 当前限制

- 2026-09-08只读现网：现有发布 `first-public-20260906`、生产库 `breeze_live_20260906`，迁移至035；已有上线后用户数据。
- 2026-09-08只读主机：2 CPU、总内存3499 MiB、当时可用约1658 MiB，无swap。现网API/Web/Yjs主进程RSS约279/61/23 MiB，自9月6日启动后的RSS峰值约346/94/59 MiB；cgroup峰值约342/79/17 MiB，OOM计数均0。RSS与cgroup的共享内存/文件计费不同，各服务峰值不代表同时峰值，也不是负载容量保证。
- 首发保留的 `breeze-first-builder-final` 在1400 MiB、1 CPU限额下完整构建退出0，14页生成齐、OOMKilled=false。前一次构建失败是npm缓存缺TypeScript（ENOTCACHED），不是内存不足；已退出容器没有可读的历史内存峰值，不能编造。
- Windows同一冻结源码的两个独立副本实测：单静态worker、原webpack方式峰值1317.4 MiB/23.08秒；启用独立webpack worker后783.0 MiB/25.09秒，均完整构建且生成standalone，没有跳过类型检查。改善是内存占用，不是构建速度。更早两轮测得1373.6/768.2 MiB，但其间结果页有修改，不用那两轮推算改动效果。现有自定义webpack配置会关闭Next默认构建worker，这是可定位的降耗来源。当前配置显式启用 `experimental: { cpus: 1, webpackBuildWorker: true }`。
- CLI采用**Linux试跑限额**：构建1024 MiB；API640/Web192/Yjs128 MiB，均禁用额外swap，另保留384 MiB主机可用内存。限额高于已观察值，仍须实际受限构建和真实预发流程验证。开始检查用限额加余量保护旧服务，不把其总和说成应用实际内存需求；现有数据不支持必须扩服务器的结论。
- 本机无Docker、WSL未安装；服务器a7c047c已在独立目录完成1024 MiB限额前端构建，峰值648,237,056字节、OOM事件均0，旧服务健康且未重启。其后本地改动尚未进入该目录，最新源码Linux构建与新应用启动仍为 **NOT_RUN**。后续仍按明确检查点构建；试跑失败须区分依赖/构建缺陷/OOM，不能直接降低检查或宣告需要扩容。
- 现网前端和Yjs的package/lock与当前内容一致；旧发布目录保留完整Linux前端node_modules及npm/Linux缓存，web运行镜像本身没有TypeScript。CLI只在两个manifest相同时复制旧node_modules到新目录，执行 `npm ls --depth=0 --include=dev --offline` 后构建；只读旧依赖，不在旧发布目录安装。依赖有变化才在新目录执行有网络的 `npm ci`。现有API镜像所有requirements-base固定版本已核对一致，仍须新代码实际导入/启动验证。
- 首发旧代码已实际无法读取新公共结果字段，禁止切回首发代码。更晚的 `68fd0db` 也已判定不能安全回退：独立PG样本中，已完成协同行程的2主线+1全局备选可读/编辑/撤销，但新语义字段校验失败，待处理initial_plan被旧worker忽略并重新推断；旧版采纳午餐会保留原缺口而多加一卡（9→10）。`1f7c892`未通过这些新操作的兼容验证，不能视为可用恢复包。

## 访问与公共参数

现有私有连接文件在 `D:/CODEX/BreezeTravel-server-access-private-20260906`。使用其中 `breezetravel_ed25519` 和 `breezetravel_known_hosts`，不复制密钥进代码包。

```powershell
$releaseSsh = @('-i','D:/CODEX/BreezeTravel-server-access-private-20260906/breezetravel_ed25519',
  '-o','IdentitiesOnly=yes','-o','BatchMode=yes','-o','PasswordAuthentication=no',
  '-o','StrictHostKeyChecking=yes',
  '-o','UserKnownHostsFile=D:/CODEX/BreezeTravel-server-access-private-20260906/breezetravel_known_hosts',
 'root@218.244.142.170')
$releaseTargets = '--expected-host iZbp12kpho9obrs2n1564gZ --current-release /opt/breezetravel-releases/first-public-20260906 --current-db breeze_live_20260906 --current-api breeze-first-api --current-web breeze-first-web --current-yjs breeze-first-yjs --new-release /opt/breezetravel-releases/core-20260908 --new-db breeze_core_20260908 --rehearsal-db breeze_rehearsal_20260908 --api-port 8028 --web-port 3128 --yjs-port 1256 --redis-db 9 --preview-redis-db 10 --model-deadline-seconds 60 --model-max-output-tokens 4096'
Get-Content -Raw scripts/release_upgrade.py | ssh @releaseSsh "python3 - prepare $releaseTargets"
```

这些是本次明确目标。后续发布必须重新指定当前版本、当前库、容器及未使用的新目录/数据库/端口/Redis库。Redis仅缓存但也不清空未知内容。实际模型参数固定60秒/4096输出，不能沿用现网30秒假定等价。

## 当前可执行的Linux构建试跑（由主任务串行触发）

先完成源码检查点，再使用该检查点同时取源码和CLI，避免发送还在编辑的工作树。以下只允许 `prepare`，不会创建DB、停止旧writer、调用真实模型或切nginx；不要顺手执行下一节的rehearse。代码包只有现有运行文件，私有设置留在服务器。

```powershell
$releaseRef = (git rev-parse HEAD).Trim()
$releaseArchive = Join-Path (Get-Location) '.local-artifacts/release-core-20260908.tar'
git archive --format=tar --output=$releaseArchive $releaseRef backend/app scripts/experience.py scripts/experience_container.py frontend/src frontend/public frontend/package.json frontend/package-lock.json frontend/next.config.ts frontend/tsconfig.json frontend/tailwind.config.ts frontend/postcss.config.js y-websocket/server.js y-websocket/package.json y-websocket/package-lock.json
$releaseScp = $releaseSsh[0..($releaseSsh.Count - 2)]
ssh @releaseSsh 'test ! -e /opt/breezetravel-releases/core-20260908 && test ! -e /opt/breezetravel-releases/core-20260908-input'
# 只有上一条退出0才继续；失败先查明确路径，不覆盖旧目录。
ssh @releaseSsh 'umask 077 && mkdir /opt/breezetravel-releases/core-20260908-input'
scp @releaseScp $releaseArchive 'root@218.244.142.170:/opt/breezetravel-releases/core-20260908-input/source.tar'
git show "${releaseRef}:scripts/release_upgrade.py" | ssh @releaseSsh "python3 - prepare $releaseTargets --source-archive /opt/breezetravel-releases/core-20260908-input/source.tar --source-ref $releaseRef"
# 只读结果确认目标/资源/旧服务正确后，主任务触发一次独立构建：
git show "${releaseRef}:scripts/release_upgrade.py" | ssh @releaseSsh "python3 - prepare $releaseTargets --source-archive /opt/breezetravel-releases/core-20260908-input/source.tar --source-ref $releaseRef --execute"
ssh @releaseSsh 'cat /opt/breezetravel-releases/core-20260908/src/frontend/.release-build-memory.json'
ssh @releaseSsh 'docker ps --filter name=breeze-first --format "{{.Names}} {{.Status}}"'
```

构建成功依据：编译、类型检查、14页生成及standalone均完成，退出0，cgroup `oom_kill 0`，旧三服务仍运行。CLI验证Next真实加载的两个worker设置，安装和构建都限制1024 MiB/1 CPU；编译容器 `--network none` 且只挂载新源码/浏览器公开配置，无法触发旧业务。`PREPARED`仅代表前端Linux构建成功，不等于新API、数据恢复或用户任务已通过。Windows测量位于本次忽略目录 `.local-artifacts/resource-check`，不是Linux通过证据。

后续在独立PG副本、Yjs副本、Redis10及私有端口下运行API640/Web192/Yjs128的完整预发流程；同时读各容器memory.peak、memory.events、RestartCount，确认新旧账号/保存编辑撤销/协同/真实模型与地点服务，并以14天160项固定外部输入检查应用上界。固定输入与真实模型分别报告。任何OOM、重启或用户结果丢失都不允许进入activate；先定位并重新测量，不能只看健康接口。预发所需的一致性备份涉及旧writer短暂停止，必须作为后续单独串行步骤执行，不包含在上述构建授权中。

## 执行顺序与每步实际含义

1. **prepare**：使用已审阅本地检查点的普通 `git archive` 源码包。代码包不得包含私有env或数据；上传路径需明确。执行时追加 `--source-archive <服务器上的代码tar绝对路径> --source-ref <实际Git检查点> --execute`。创建新发布目录并受限构建正式前端，不动旧业务；实际退出和资源测量才决定本步是否成功。
2. **rehearse**：追加 `--execute`。短暂停止旧API/Yjs写入，备份当前PG、Yjs、身份配置及nginx，然后恢复旧服务。旧API和Yjs各最多等待40秒，必须实际健康后才继续克隆与迁移；超时明确指出失败服务，保留备份及其路径，不能报告 `COPY_RESTORED`。备份异常时仍先尝试恢复并检查旧服务。仅对独立副本恢复、追加迁移和核对原有业务行数，状态为 `COPY_RESTORED`。**此时没有启动应用，也没有用户流程验收。**

   本健康恢复修复尚未传入服务器已构建的 `a7c047c` 目录；必须进入下一份明确源码检查点与准备版本。a7构建成功不代表本工具改动已在Linux验证。本轮没有执行远程停写或副本演练。
3. **preview**：追加 `--execute`。单独构建预发前端，API走新容器的同源转发，协同地址编译为 `ws://127.0.0.1:1256`，避免连接正式Yjs。只在副本将继承的待处理/运行中识别、地图、住宿和餐饮任务终止，清除副本租约；正式库与正式租约不变。随后以现有真实密钥、真实worker启动副本API、独立Yjs和Redis10。状态为 `PREVIEW_SERVING_UNVERIFIED`，仅表示入口健康。
4. **真实预发操作**：建立下面的SSH转发，浏览器打开 `http://127.0.0.1:3128`。从新原文开始，等真实识别/地点核验，修改地点或时间、撤销、账号保存、刷新重开、路线、餐饮、住宿、协同等按本次目标实际验收。不得把复制过来的旧记录或健康接口当作新流程成功。复制的未完成任务不会自动重试，预发应新建测试记录。保留实际预发新记录的public resource id，仅用于操作时输入，不写进公共报告。

```powershell
ssh @releaseSsh -N -L 3128:127.0.0.1:3128 -L 8028:127.0.0.1:8028 -L 1256:127.0.0.1:1256
```

   浏览器高德SDK必须实际加载；现有密钥存在、后端CORS正确均不能证明控制台白名单正确。若本地预发域名受限，要如实记录并解决合法预发访问方式，不能把地图测试改成模拟后声称真实通过。正式域名仍需再次验收SDK。

5. **activate**：全部范围的真实验收通过后，追加 `--preview-trip-id <预发中新建、账号保存并实际编辑的记录> --execute`。工具读取副本当前记录，要求新创建、与本次配置一致的模型及外部响应记录、完整结果、账号所有权及真实用户编辑版本；不硬编码Qwen供应商标签，也不自动更换现有模型。现有配置变量仍沿用Qwen旧命名。该窄路径检查不能代替全部产品验收。随后停止预发，停止旧writer，重新备份当前正式数据，另建新的live库和Yjs目录，再显式迁移、启动、切nginx。预发数据库/Yjs/Redis10均不提升为生产；正式新进程用Redis9。只在切换后将PG/Redis的restart设置为unless-stopped；旧writer保持停止。
6. **公网验证**：正式域名桌面/手机检查全部核心路径、现有账号和旧记录、新记录、地图SDK、协同，以及服务/主机重启后的恢复。健康接口或该脚本退出0不能代表交付。

## 失败、恢复与重跑

- 失败stderr保存在新发布目录 `private-failure-*.log`，以0600创建并过滤已有密钥/连接/Bearer；终端仅阶段、退出原因和日志路径。只读计划不写日志。由执行者读取私有日志定位，不公开整份运行配置。
- `prepare`完成后重复执行不重复构建；构建失败停在SOURCE_READY可用同source-ref继续。恢复副本成功、预发健康、正式切换成功的重复执行不会重做数据快照或覆写数据。
- 恢复/克隆中途失败时不删除或覆盖部分数据库；先读完整错误。若数据库已部分创建且无法确认完整性，保留它，使用新的明确发布目录和库名重做，不能借助DROP/旧库覆盖消除失败。
- 预发启动失败可重跑preview，保留预发已产生的新数据和任务，不再次清理其队列。复制来的任务仅在第一次启动worker之前隔离；租约与幂等均归各自PG库，没有跨库共享。
- 一旦记录“流量可能已切换”，即使reload响应丢失也不能重启旧库writer。执行 `recover --execute`，备份当前新库/Yjs并用本发布代码重新启动，保留可能发生的全部新写入。恢复不会回旧库、不会尝试旧首发代码；业务代码缺陷需创建新的前向修复发布。
- 新协同计划与午餐缺口的最小前向恢复已在独立PG验证：用当前新repository/worker读取已产生的数据，未处理协同队列无需模型重解释，处理后2主线+1全局备选及编辑/撤销均保留；先前采纳的午餐保留，继续采纳后总9卡、剩1个缺口。该项为固定外部响应+真实PG/仓储/服务/worker检查，没有启动完整旧HTTP服务，不替代Linux恢复演练。私有复现入口为本次 `.local-artifacts/compatibility_probe.py`，结果在 `.local-artifacts/compatibility-68fd0db/result.json`。
- 下一份完整餐宿/协同代码检查点可作为后续模型调整的兼容恢复候选；必须用最终新版的新写入与在途队列，在候选实际API/worker上读取、继续操作，再由新程序读回。未测或失败均不能宣称可回退，也不允许换回旧库隐藏新写入。
- 发布使用主机文件锁，容器须有本发布标签才允许停止/移除；不删除DB、卷、Yjs数据或其他应用。正式新应用启动前检查旧writer停止。切换前出错先停止部分新writer再恢复旧writer。

## 已有本地验证入口

```powershell
& D:/CODEX/BreezeTravel/.venv/Scripts/python.exe scripts/tests/test_release_upgrade.py
& D:/CODEX/BreezeTravel/.venv/Scripts/python.exe scripts/tests/test_release_upgrade.py --local-env .local-artifacts/experience/experience.env --pg-bin D:/CODEX/BreezeTravel-G07-Tools/postgres16-pgvector/bin
```

第二条读取当前本地数据库快照，在三个自己创建的临时DB验证真实恢复、加密原文、旧住宿选择、035→039重复迁移、保留新写入、副本任务租约隔离和拒绝假预发结果；结束只清理自己的临时库。它不验证Linux镜像、真实供应商或生产部署。
