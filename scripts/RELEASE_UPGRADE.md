# 本次桌面版本发布与恢复

2026-09-13用户已明确授权收敛并发布最新版。本次是带已知限制的阶段发布，不等待五城识别等整体目标全部达标，也不宣称这些目标已完成。停止新增功能和模型实验；只推进数据保护、隔离预发、正式切换和电脑网页验收。手机及小屏专项验收暂停。

用户随后明确允许丢弃旧版本项目、数据库等内容，旧数据不再是必须长期保留的业务要求。但本次CLI仍依赖旧容器/镜像、配置和切换前的源库，不能提前删除这些对象。本次先完成既定切换与恢复验证，不扩大旧环境清理；新版上线后的数据仍须保留。已完成的副本验证无需重做。

## 当前已完成与未完成

本次产品源码固定取自 `2bafa52068429bd81dbbed384d9cf64ca35ff5fe`；后续发布工具修复为 `a65dd85`，仅预建只读挂载目录，不改变产品产物。run_action.py 已区分产品引用与工具引用。已完成：

- Linux受限构建退出0，峰值777,502,720字节、OOM事件0，构建期间旧服务未动。
- `rehearse` 已短暂停止旧API/Yjs写入，取得生产PG、Yjs、身份配置及nginx的一致备份；旧API/Yjs恢复并健康检查通过。
- 独立预发副本已完成035→041迁移，状态 `COPY_RESTORED`。
- 副本只读核对全部通过：原业务行内容、主键、迁移默认值、原文解密、旧结果解析、身份配置，以及备份tar与Yjs副本的路径和文件内容一致。本次核对没有业务写入或供应商调用。

**已正式切换为LIVE，预发和正式域名各一次两日四地点的真实整理、账号保存、移动撤销、重开、PNG及匿名分享通过。** 地图SDK已加载，交通衔接未核实仍为LIMITED；不宣称五城和全范围餐宿质量通过。空房间同账号双连接/刷新已通过；同版本恢复命令已成功结束为RECOVERED_KEEPING_CURRENT_DATA。用户随后要求停止，恢复后的用户流程复验NOT_RUN。

本次入口及私有输出保留在 `D:/CODEX/BreezeTravel-local-archive-20260913/latest-local-artifacts/evaluation/desktop-stage-release-20260913/`；副本核对结果为 `restored-data-readonly.json`。此前v13构建仅作历史记录，见 `.local-artifacts/evaluation/linux-v13-prepare-v1/`，不再沿用其命令或覆盖其目录。

## 固定目标与运行边界

|用途|本次目标|
|---|---|
|正式域名|`https://www.breezetravel.cn/`|
|原发布目录|`/opt/breezetravel-releases/first-public-20260906`|
|原生产库|`breeze_live_20260906`，迁移035；当前切换流程仍以它为源库，尚不删除|
|原API/Web/Yjs容器|`breeze-first-api` / `breeze-first-web` / `breeze-first-yjs`|
|本次发布目录|`/opt/breezetravel-releases/desktop-v14-2bafa52-20260913`|
|预发库|`breeze_preview_v14_20260913`，已迁移041|
|新正式库|`breeze_v14_20260913`，仅在正式切换步骤复制最新生产数据后使用|
|新API/Web/Yjs端口|`127.0.0.1:8038` / `127.0.0.1:3138` / `127.0.0.1:1266`|
|Redis|新正式11，预发12；仅作缓存，禁止清空未知空间|
|模型边界|沿用现有默认模型，总期限60秒、每答4096输出；compact和focused默认关闭|

预发和正式阶段复用本次新容器名及端口，但使用不同PG、Yjs目录、Redis空间和网页构建；`activate`先停止预发容器。预发Yjs目录是发布目录下的 `rehearsal-yjs-data`，正式为 `yjs-data`。预发数据不能提升为生产数据。

CLI执行前检查当前主机、挂载、数据库绑定、容器、端口、独立Redis及可用内存/磁盘。构建限额1024MiB/1CPU；API/Web/Yjs限额640/192/128MiB，禁止额外swap并保留主机余量。限额是保护边界，不是实际容量保证。任务、租约、旧服务健康及必要写入者停止状态须按实际阶段核对，不凭旧快照假定空闲。

## 实际操作入口

清理工作树后以下私有辅助脚本仅作历史入口，部分内部ROOT仍指旧工作树，不能原样再次运行。正式源码和scripts/release_upgrade.py现位于D:/CODEX/BreezeTravel；需要后续运维时按本表核实实际环境再配置入口。用户当前已要求停止，不安排额外执行。

由主任务串行执行；同一步仍在运行时不要再次触发。以下本机入口读取已经核实的服务器连接和固定参数，并从上述Git版本取得CLI，不向服务器发送正在编辑的工作树。

```powershell
$releasePython = 'D:/CODEX/BreezeTravel/.venv/Scripts/python.exe'
$releaseAction = 'D:/CODEX/BreezeTravel-local-archive-20260913/latest-local-artifacts/evaluation/desktop-stage-release-20260913/run_action.py'

# 只读计划：查看当前阶段、目标和下一步动作，不改变服务或数据。
& $releasePython -X utf8 $releaseAction preview

# 仅在确认当前无同一步操作运行后，执行该步骤。
& $releasePython -X utf8 $releaseAction preview --execute
```

`run_action.py`接受 `rehearse`、`preview`、`activate`、`recover`；省略 `--execute` 是只读计划。`prepare`和`rehearse`已完成，不重复构建、复制或创建另一套备份。若需查看恢复副本阶段，可使用 `rehearse`只读计划。

### 预发

`preview`单独构建预发网页，API同源转发至8038，协同连接 `ws://127.0.0.1:1266`。第一次启动worker之前，只在副本隔离继承的识别、地图、餐饮、住宿待处理任务和租约，防止重放旧生产调用；正式任务不变。预发后再运行副本完整性脚本会因状态变化拒绝，不能把已隔离任务与启动前的严格副本混作同一状态。

入口健康只表示 `PREVIEW_SERVING_UNVERIFIED`。使用 `run_prepare.py`内已有的SSH身份和主机校验设置，将本机3138、8038、1266分别转发到服务器对应localhost端口，浏览器打开 `http://127.0.0.1:3138`；不要公开这些私有服务端口或关闭主机校验。

用新的窄文字输入经过真实worker、模型及地点核验，检查最终地点、日序、角色和未完成提示，再实际编辑、撤销/重做、账号保存、刷新重开，并检查地图、餐宿和协同必要桌面路径。旧副本兼容核对已完成，不新增旧数据必须长期保留的门槛。仅使用相对Day和先后，不验收日历日期、时刻或停留编辑；真实路程和交通耗时保留。真实供应商与固定回放分别记录，不把200、完整状态或一条成功样本当整体质量达标。

浏览器高德SDK须实际加载；若预发地址受供应商白名单限制，记录并处理合法访问方式，不用模拟结果冒充真实通过。正式域名仍须复验。

### 正式切换

预发必要用户路径与数据保护通过后，将实际在本次预发中新建、由账号保存并编辑的资源ID赋给本地变量 `$previewTripId`，不写入公共报告，再运行：

```powershell
& $releasePython -X utf8 $releaseAction activate --preview-trip-id $previewTripId
& $releasePython -X utf8 $releaseAction activate --preview-trip-id $previewTripId --execute
```

CLI会验证该记录确实创建于本次预发、保存到账号、有持久化编辑、当前结果完整，且有与当前配置一致的实际模型调用记录。此窄检查不代表全部产品验收，不能手改数据库或补造完整标记来通过。

`activate`会停止预发和旧写入者，**在切换当时重新备份当前生产PG、Yjs及配置**，恢复到新正式库和独立Yjs目录，显式追加迁移，启动本次配套API/Web/Yjs，健康检查后切换nginx。不使用预发库、预发新记录或旧演练备份替代最新生产数据；旧写入者保持停止。

切换后在正式域名电脑浏览器核对账号、新生成与编辑保存、刷新重开、地图SDK、餐宿和协同；随后按串行安排检查新服务重启后仍能读回并继续操作新版记录。容器或CLI退出0不能代替这些操作。实际结果和剩余限制由主任务补入当前状态。

## 失败与保留新数据的恢复

- 不复跑 `first_release`。旧代码和旧库虽已获准丢弃，本次尚不执行清理：CLI仍需旧镜像、容器信息、配置及切换源数据，须先完成当前切换与恢复。失败时也不擅自删除部分恢复库。新版源码、PG、Yjs及身份配置须配套保留；新版使用的加密原文根密钥、JWT和cookie签名设置不能随意更换。
- 036/038/041为追加迁移，旧行保留legacy/代次0等兼容值。**041改动地图任务唯一约束：仍使用旧双列 `ON CONFLICT(plan_ref_id, route_config_hash)` 的旧应用不能在041库继续写入。** 不删除代次、不恢复旧唯一约束来迁就旧写入者。
- 新公共结果包含备选、餐位、详情、未完成、重做及新版协同字段。旧前端与旧API不能直接作为恢复包；禁止只回退网页或API的一半。发布代码缺陷应准备保留当前数据的前向修复。
- 在尚未记录可能切流的阶段出错，工具先停止部分新写入者，再尝试恢复原API/Yjs并检查健康。原库仍为原结构；未能确认新写入者停止时，不允许两边同时写。克隆中途失败保留部分库和错误，不能覆写或删除来消除失败。
- **一旦 `traffic_may_have_switched=true`，即使nginx重载回执丢失，也必须假定新库已有用户写入。** 不能重启旧库写入者、切回旧数据库或恢复切换前备份。

此时先读计划，再执行本版本恢复：

```powershell
& $releasePython -X utf8 $releaseAction recover
& $releasePython -X utf8 $releaseAction recover --execute
```

`recover`停止新写入者，先备份**当前新正式PG和Yjs（含切换后的新写入）**，然后用同一发布源码、同一新库和数据目录重建服务及路由。它不是旧版本回滚，也不是自动恢复SQL备份；不能修复任意业务代码缺陷。成功状态为 `RECOVERED_KEEPING_CURRENT_DATA`，仍需正式网页读回和继续操作验证。

重复动作按CLI记录的阶段恢复，不覆盖已存在数据库或已保存的新记录。预发重启保留已产生的预发数据，继承任务只在首次启动worker前隔离。失败后先读 `upgrade-state.json` 和该发布目录的私有失败日志；只公开阶段、错误类别和脱敏结论，不贴运行配置、原文、令牌或密钥。发布文件锁和容器归属校验必须保留，不并行执行升级动作。

## 已有验证记录

本次Linux构建、复制迁移和只读副本核对见上述 `desktop-stage-release-20260913`目录。原业务行在内存比较，来源仅在进程内解密，Yjs直接比较文件内容；没有新增签名、摘要或治理平台。源生产备份后的变化单独报告，不能自动算作恢复丢失，也不能静默忽略。

既有本地035→041及保留新写入的恢复演练见 `.local-artifacts/verification/release-restore-041-final.json`，对应维护入口为 `scripts/tests/test_release_upgrade.py`。其固定/本地结果不替代本次Linux预发和正式域名验收；不因本文整理重复全套测试。
