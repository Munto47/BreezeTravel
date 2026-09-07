# 首次服务器交付与恢复记录

正式入口：https://www.breezetravel.cn/ 。2026-09-06 已完成切换，公网两个真实流程通过；发布标记为 PUBLIC_VERIFIED。

## 部署形态

- 发布目录：`/opt/breezetravel-releases/first-public-20260906`，运行源码 `src`；私有 env 不进入仓库。
- 新容器：`breeze-first-web`（回环端口 3118）、`breeze-first-api`（8018）、`breeze-first-yjs`（1246），均配置 unless-stopped。
- 复用现有 PostgreSQL／Redis。新版库 `breeze_live_20260906` 来自停写后的最终旧库备份；Redis 使用独立 DB 8。旧库 `travel_agent` 不覆盖。
- 旧 backend 和 y-websocket 已停止但保留；旧 frontend／镜像／Yjs 原始数据保留。新 Yjs 使用独立副本。
- 原站点 nginx 转向新回环端口；旧 8080 入口重定向正式 HTTPS。SSE 与 WebSocket 保留；相关 URL 不写访问日志。
- 临时验证入口仅监听 `127.0.0.1:8443`；本地测试隧道已关闭。公网最终测试没有本地 DNS 覆盖。
- 服务器使用保留的依赖镜像承载新源码；前端在服务器执行锁定依赖离线安装和 Linux 生产构建。未升级依赖锁。Node 20.18.3 的工具链 engine 警告仍存在，构建通过，不外推供应链加固。

## 备份与恢复边界

备份目录：`/opt/breezetravel-backups/first-release-20260906`（仅 root）。其中初始 `travel_agent.dump`、`yjs.tar`、原站点配置、私有容器配置均保留；`cutover/travel_agent.dump` 是停止旧写入者后的最终备份，已经成功恢复到新库。`cutover/travel-nginx.conf` 为旧 8080 配置。初始备份清单有摘要，数据库归档读取通过。

首次切换脚本有启动前校验和失败恢复逻辑；已完成后禁止重复运行 `activate_first_release.py` 或重建 staging 覆盖私有运行配置。运维脚本是本次明确路径专用，不是可对任意服务器重复执行的安装器。

**尚未执行切换回旧站点的演练。** 新版已经允许写入，不能为了测试直接切回旧库而隐藏新增内容。将来如需回滚：

1. 先暂停新版写入，另存新版 PostgreSQL 和 Yjs 的带时间戳备份，并验证可读；不删除新库或新卷。
2. 评估上线后新增／修改数据如何保留或迁移。旧库不会自动获得新版数据，不能直接宣称无损回滚。
3. 在确认数据处理方案后，恢复备份中的 nginx 两份配置，启动保留的旧 backend／Yjs，验证旧服务健康后再 reload nginx。
4. 检查 HTTPS、账号与旧房间；保留所有新版目录、数据和日志供恢复使用。不得影响同机其他服务。

## 实际验证与限制

- 公网 `first-release-live.spec.js`：2 passed，涵盖匿名文字至删除和双账号协同至单向导入；源码生成的测试使用合成内容，不操作所有者存量行程。
- 完整前端组合 120 passed / 5 历史 skipped；后端组合 76 passed；文字定向 75 passed，40 样例中 37 精确、3 安全降级。
- 协同旧驾车耗时未具备可靠逐段来源，公开响应剔除该交通数据；无估算线、不冒充成功。协同追问有受限结果，不能把流程可恢复等同内容质量全部通过。
- 真实行程查步行结果通过；所有者三份真实攻略、正式真人测试、实体设备、完整候选与回滚切换演练未运行。
- 公网证据报告在本地忽略目录 `.local-artifacts/first-release-public-report.json`；截图和 PNG 在 `frontend/test-results-first-release-public`。凭据、私钥及 private env 不附入证据。

后续从当前源码检查点继续，以所有者真实反馈的阻断问题为优先；不自动合并 main 或进行下一轮发布。
