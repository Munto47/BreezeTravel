# 本地完整体验运行

产品目标、执行顺序和进度分别以 [项目章程](product/PROJECT_CHARTER.md)、[实施计划](governance/IMPLEMENTATION_PLAN.md)、[当前状态](governance/CURRENT_GOAL.md) 为准。本文只说明运行和恢复。

## 本机启动

在仓库根目录运行：

```powershell
.\scripts\experience.ps1 start
```

网页为 `http://127.0.0.1:3106`，API 健康检查为 `http://127.0.0.1:8006/health`。命令依次初始化独立数据库、应用现有迁移、启动缓存、API、后台整理与地图任务、隐私维护，完成前端生产构建后启动网页，并检查 API 和首页的 HTTP 响应。首次需要现有后端 Python 环境与已安装的前端 `node_modules`。

每次 `start` 或 `restart` 都先停止本工具管理的网页进程，用当前源码和当前浏览器配置运行 `next build`，成功后才运行 `next start`，不会悄悄沿用旧构建。构建失败时保留数据库和日志，不启动旧网页产物。已有数据库、缓存和 API 在重复 `start` 时继续运行；修改后端后使用 `restart`。

本机自动发现相邻 `BreezeTravel/.venv`、`BreezeTravel-G07-Tools/postgres16-pgvector/bin` 和 `BreezeTravel-G07-Tools/memurai-4.1.2-portable/Memurai/memurai.exe`。其他位置可设置 `EXPERIENCE_PYTHON`、`EXPERIENCE_POSTGRES_BIN`、`EXPERIENCE_REDIS_BIN` 和 `EXPERIENCE_NODE`。窗口在后台隐藏运行。

PostgreSQL 使用 `127.0.0.1:55439`，Redis 兼容服务使用 `127.0.0.1:56389`。数据库文件、生成的独立凭据、缓存配置、进程记录和日志只放在被 Git 忽略的 `.local-artifacts/experience/`，不使用或修改旧数据库。Memurai 是本地开发运行时，不用于生产。

```powershell
.\scripts\experience.ps1 status
.\scripts\experience.ps1 restart
.\scripts\experience.ps1 stop
```

停止与重启按已记录的进程创建时间核对进程身份；不会按进程名批量杀死其他服务。停止保留数据库和凭据。只调试后端可加 `-NoWeb`。

需要热更新时显式使用 `.\scripts\experience.ps1 restart -Dev`（Python 入口为 `--dev`），开发产物位于 `.next-dev`，生产产物位于 `.next`。开发模式曾出现冷启动首屏脚本语法错误而无法激活按钮；后续带调试捕获的冷启未复现，原因尚未确认。默认生产模式不依赖开发时编译；生产模式的验证不能表述为修复了该开发模式问题。

## 配置与真实调用

首次从相邻 `BreezeTravel/.env`、`BreezeTravel-G07-Tools/g07-live.env`、当前根目录 `.env` 按顺序读取已有授权的 Provider 配置；同名进程环境变量优先。只复制模型和地图所需字段，不回显值。私有副本为 `.local-artifacts/experience/experience.env`。已有副本后续保持稳定，可直接编辑该文件后重启。

真实输入使用 `QWEN_API_KEY`、`QWEN_API_URL`、`TRIP_UNDERSTANDING_QWEN_MODEL` 和 `AMAP_API_KEY`。没有显式模型名时，复用现有版本化比较文件的 `selected_model`，不自动尝试其他模型。当前默认调用期限 30 秒、最多输出 4096 token。可选价格字段为空时费用为未知，不伪造为零。

前端通过 `BACKEND_INTERNAL_URL` 同源转发 API；地图使用 `NEXT_PUBLIC_AMAP_KEY` 和 `NEXT_PUBLIC_AMAP_SECURITY_CODE`。浏览器地图配置与后端 Web 服务密钥分开。构建与启动共用同一份环境白名单，只接收这些浏览器配置、运行所需的系统变量及固定本地构建标志；后端 Provider、数据库、JWT 和原文加密密钥不会传入前端进程。修改浏览器配置后重启会重新构建。

固定演示以明确的 `FIXED_DEMO` 来源进入独立样例链，路线与住宿使用演示数据；高德底图仍联网加载，在示例中主动搜索地点也会查询真实候选。自定义文字直接调用真实模型和高德，不静默用规则或固定样例替代失败。测试若需要规则样例，必须显式传入测试 pipeline 或同时设置 `RUNTIME_PROFILE=test/local_fixture`、`TRIP_UNDERSTANDING_PROVIDER_MODE=fixture`。已派发而结果未知的任务接管不自动重复外部调用。

## 运行边界

新入口是 `backend/app/experience_main.py`；旧 `main.py` 和历史代码保留。新入口只注册文本 v3、邮箱账户及资料接口，启动整理、地图/住宿和隐私维护。房间、Planner、短信模拟登录、OCR 路由与模型、签名 Broker 不进入启动流程；兼容数据模型仍由现有仓储使用。共享 API 内已保留的账户、保存、删除等能力继续工作。

匿名权限继续使用 HttpOnly Cookie；写请求保留来源检查和 Redis 限流；验证和未知错误不回显原文、数据库连接串或堆栈；API 访问日志关闭。所有健康和拒绝响应只返回脱敏状态。原始攻略不应放入共享日志、截图或提交。

## 备份与恢复

```powershell
.\scripts\experience.ps1 backup
.\scripts\experience.ps1 restore -BackupFile '完整路径\backup-时间.dump'
```

备份是私有数据库文件，可能包含用户数据，留在忽略目录。恢复先停止应用，再创建全新的数据库导入备份、补齐迁移、切换私有配置并启动。旧数据库保留；恢复失败继续使用原数据库，不覆盖、不删除旧数据。恢复后如要切回，可将私有配置中的 `EXPERIENCE_DATABASE` 改回原数据库名并重启。凭据和加密密钥必须与备份一起私密保留，否则已有密文无法读取。

## 可选 Docker Compose

本机已通过原生运行时运行；本机未安装 Docker，Compose 实际启动尚未验证。具备 Docker 的机器先填写独立私有配置，再执行：

```powershell
.\scripts\experience.ps1 configure
docker compose --env-file .local-artifacts/experience/experience.env -f compose.experience.yml up --build -d
```

Compose 使用相同端口，启动前先停止原生服务。容器使用独立命名卷，API 镜像只安装该入口需要的 Python 库，前端运行本地体验服务。不要运行删除卷的清理命令；本文不包含发布或生产部署步骤。

## 行程保留期限与历史语义

旧实现的 30 天仅指原文和可还原引用，到期保留结构化行程；匿名过期会拒绝访问，但未完整清理派生数据。这不是新版行程保留验证。原 `purge_expired_private_data` 继续维持只清理原文的兼容语义。

新体验入口另外执行 `expire_retained_trips`：每份匿名行程自创建起保留 24 小时，账户行程自创建或领取起默认保留 30 天；使用既有 `source_expires_at` 作为资源截止时间。新建草稿只延长会话可用时间，不延长旧稿；幂等回放不续期。授权先校验所有权，再拒绝已到期资源；后台按当前真实时间、有限批次复用整程级联删除并留下不可恢复业务内容的过期记录。手动删除原文不会提前删除未到期行程，也不会延长其期限。未来时间测试只在临时数据库运行。
