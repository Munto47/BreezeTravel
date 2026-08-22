# BreezeTravel 阿里云部署文档

> **状态说明：历史部署 Runbook。** 使用前必须重新核对当前 Compose、环境变量、服务器备份与回滚方案；本文存在不等于当前公网部署已经验证。

记录从零部署 BreezeTravel 到阿里云 ECS（Ubuntu 24.04）的完整流程，涵盖 Docker 编排、Nginx 反向代理、HTTPS 证书、日常更新操作。

---

## 1. 服务器环境要求

| 组件 | 最低版本 | 用途 |
|------|---------|------|
| Ubuntu | 22.04 / 24.04 | 操作系统（CentOS / Alibaba Linux 也可，包管理器命令略有不同） |
| Docker | 24.0+ | 容器运行时 |
| Docker Compose | v2.x | 编排工具（随 Docker Desktop / docker-ce 自带 `docker compose` 子命令） |
| Nginx | 1.24+ | 反向代理 + HTTPS 终止 |
| Certbot | 任意 | Let's Encrypt 免费证书 |
| Git | 2.30+ | 拉代码 |

ECS 规格建议：**2 vCPU / 4 GB 内存 / 40 GB 系统盘**（最低）。Embedding 加载 + Postgres + Redis + 前端 build 内存会到 3 GB+。

安全组放行：

| 协议 | 端口 | 用途 |
|------|------|------|
| TCP | 22 | SSH |
| TCP | 80 | HTTP（Certbot 续期 + 跳转 HTTPS） |
| TCP | 443 | HTTPS |

> 后端 8000、前端 3000、Postgres 5432、Redis 6379、y-websocket 1234 都通过 docker-compose 绑定到 `127.0.0.1`，**不对外暴露**，所有公网流量经 nginx 转发。

---

## 2. 一键准备命令

```bash
# Ubuntu / Debian
sudo apt update && sudo apt install -y git nginx certbot python3-certbot-nginx
curl -fsSL https://get.docker.com | sudo sh -s -- --mirror Aliyun
sudo systemctl enable --now docker

# 版本确认
docker --version
docker compose version
nginx -v
```

---

## 3. 项目部署

### 3.1 克隆仓库

```bash
sudo mkdir -p /opt && cd /opt
sudo git clone https://github.com/Munto47/BreezeTravel.git breezetravel
cd breezetravel
```

### 3.2 配置环境变量

```bash
sudo cp .env.example .env
sudo vim .env       # 或 nano .env
```

`.env` 关键字段（按用途分组）：

```bash
# ===== 主 LLM =====
DEEPSEEK_API_KEY=sk-...
DEEPSEEK_API_URL=https://api.deepseek.com/v1

# ===== 备用 LLM / Embedding（SiliconFlow 兼容 OpenAI 接口）=====
OPENAI_API_KEY=sk-...
OPENAI_API_URL=https://api.siliconflow.cn/v1

# ===== 高德地图 =====
AMAP_API_KEY=...           # 后端 REST API Key（Web 服务类型）
AMAP_JS_KEY=...            # 前端 JS SDK Key（Web 端 JS API 类型）
AMAP_MOCK=false

# ===== 和风天气 =====
QWEATHER_API_KEY=...
QWEATHER_API_HOST=...      # 你的专属 host
QWEATHER_AUTH_TYPE=jwt
QWEATHER_PRIVATE_KEY=...
QWEATHER_KEY_ID=...
QWEATHER_PROJECT_ID=...

# ===== JWT 鉴权 =====
JWT_SECRET_KEY=...         # 32 字节随机串

# ===== 阿里云短信（可选）=====
ALIBABA_CLOUD_ACCESS_KEY_ID=...
ALIBABA_CLOUD_ACCESS_KEY_SECRET=...
ALIBABA_CLOUD_SMS_SIGN_NAME=...
ALIBABA_CLOUD_SMS_TEMPLATE_CODE=...

# ===== LangSmith 可观测性（可选）=====
LANGSMITH_API_KEY=...
LANGSMITH_PROJECT=BreezeTravel

# ===== LoRA 微调 Router（可选）=====
# 注意：模型权重默认未推到服务器，启动会自动降级到 DeepSeek 在线 Router
FT_ROUTER_ENABLED=true
FT_ROUTER_MODEL_PATH=backend/models/router_lora

# ===== 数据库 / 缓存（docker-compose 会覆盖 host）=====
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/travel_agent
REDIS_URL=redis://localhost:6379

# ===== Demo 模式 =====
DEMO_MODE=false

# ===== 前端构建变量（NEXT_PUBLIC_* 是 build 时 bake，必须用生产 URL）=====
# 注意：URL 末尾**不要**加 /api，前端代码内部 fetch 路径已经带 /api/ 前缀，
# 多加会导致 https://www.breezetravel.cn/api/api/... 双前缀 502。
NEXT_PUBLIC_API_URL=https://www.breezetravel.cn
NEXT_PUBLIC_Y_WEBSOCKET_URL=wss://www.breezetravel.cn/yjs
NEXT_PUBLIC_AMAP_JS_KEY=...
NEXT_PUBLIC_AMAP_SECURITY_CODE=...

# ===== CORS（允许带 www 和不带 www 访问后端）=====
CORS_ORIGIN_REGEX=^https://(www\.)?breezetravel\.cn$
```

> **关键提醒**
> 1. `.env` 必须 LF 行尾（不是 CRLF）。如果是从 Windows 复制过来，跑一次 `sed -i 's/\r$//' .env`。
> 2. `NEXT_PUBLIC_*` 在 frontend Dockerfile 的 build 阶段就被 bake 进 JS bundle，**改了必须 rebuild frontend 才生效**。
> 3. 写完用 heredoc 而不是 vi 粘贴，避免 SSH 客户端字符转换。

### 3.3 首次启动

```bash
docker compose up -d --build
docker compose ps                       # 应看到 5 个服务全部 Up
docker compose logs --tail 30 backend   # 看到 "Application startup complete" 即成功
curl -s http://localhost:8000/health    # 返回 {"status":"ok",...}
```

### 3.4 RAG 知识库入库

新部署的 Postgres 是空库。需要生成游记并入库：

```bash
docker compose exec backend python -m scripts.ingest_notes
```

约 10-20 分钟。完成后数据库会有约 4000 条 chunk。

可在另一个 SSH 窗口监控进度：
```bash
docker compose exec postgres psql -U postgres -d travel_agent \
    -c "SELECT COUNT(*) FROM travel_chunks;"
```

---

## 4. Nginx 反向代理

### 4.1 域名解析

在域名服务商处加 A 记录：
- `breezetravel.cn` → ECS 公网 IP
- `www.breezetravel.cn` → ECS 公网 IP

### 4.2 用 Certbot 自动配置 HTTPS

```bash
sudo certbot --nginx -d breezetravel.cn -d www.breezetravel.cn
```

按提示填邮箱、同意条款。Certbot 会自动：
1. 申请 Let's Encrypt 证书
2. 修改 nginx 配置加入 SSL 段
3. 配置 80 → 443 跳转

### 4.3 最终 nginx 配置

文件路径：`/etc/nginx/sites-available/breezetravel.cn`（symlink 到 `sites-enabled/`）

```nginx
server {
    server_name breezetravel.cn www.breezetravel.cn;

    # 前端 Next.js
    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # 后端 FastAPI（SSE 长连接 + 普通 REST）
    location /api/ {
        proxy_pass http://127.0.0.1:8000;   # ⚠️ 末尾不能有斜杠！有斜杠会剥离 /api 前缀导致全部 404
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # SSE 流式响应必须的设置
        proxy_read_timeout 300s;
        proxy_connect_timeout 60s;
        proxy_buffering off;
    }

    # Yjs 协同 WebSocket（必须开 Upgrade）
    location /yjs/ {
        proxy_pass http://127.0.0.1:1234/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 86400s;
    }

    listen 443 ssl; # managed by Certbot
    ssl_certificate /etc/letsencrypt/live/breezetravel.cn/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/breezetravel.cn/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;
}

server {
    if ($host = www.breezetravel.cn) {
        return 301 https://$host$request_uri;
    }
    if ($host = breezetravel.cn) {
        return 301 https://$host$request_uri;
    }
    listen 80;
    server_name breezetravel.cn www.breezetravel.cn;
    return 404;
}
```

测试 + 重载：
```bash
sudo nginx -t && sudo systemctl reload nginx
```

### 4.4 路径映射一览

| 公网 URL | nginx 转发到 | 容器服务 |
|---------|------------|---------|
| `https://www.breezetravel.cn/` | `127.0.0.1:3000/` | frontend (Next.js) |
| `https://www.breezetravel.cn/api/*` | `127.0.0.1:8000/api/*` | backend (FastAPI) |
| `wss://www.breezetravel.cn/yjs/*` | `127.0.0.1:1234/yjs/*` | y-websocket (Yjs) |

> **重要**：`/api/` 的 `proxy_pass` **不加末尾斜杠**（`http://127.0.0.1:8000`），nginx 才会把完整路径 `/api/chat` 原样转给 FastAPI。FastAPI 的路由前缀本身就是 `/api`（`main.py` 里 `include_router(..., prefix="/api")`），加了斜杠会剥离 `/api` 导致全部端点 404。

---

## 5. HTTPS 证书自动续期

Certbot 装好后会自动加 systemd timer。验证：

```bash
sudo systemctl status certbot.timer
sudo certbot renew --dry-run
```

证书 90 天到期，timer 会在 30 天内自动续期 + reload nginx。无需人工介入。

---

## 6. 日常更新部署

代码改完推到 GitHub 后，**服务器上只需要两条命令**：

```bash
cd /opt/breezetravel
git pull && docker compose up -d --build
```

- 后端只改 Python 源码 ≈ 30s（依赖未变，pip install 走缓存）
- 前端改 .tsx ≈ 1-2 min（Next.js 重 build）
- 改 `requirements.txt` 或 `package.json` ≈ 3-5 min（重装依赖）

更新过程中**不停服**：旧容器继续服务直到新容器 ready 才切换。

### 验证更新成功

```bash
docker compose ps                                       # 全 Up
docker compose logs --tail 20 backend                   # 无 ERROR
curl -s https://www.breezetravel.cn/api/health          # ok
```

---

## 7. 数据持久化与备份

Docker volume 数据**不会**被 `docker compose down` 删除：

| Volume | 内容 |
|--------|------|
| `breezetravel_pg-data` | PostgreSQL（用户/房间/RAG chunks/checkpoints） |
| `breezetravel_yjs-data` | Yjs 协同状态持久化 |

### 备份 PostgreSQL

```bash
docker compose exec postgres pg_dump -U postgres travel_agent \
    > /opt/backup/pg-$(date +%Y%m%d).sql
```

建议加 crontab 每日自动备份 + 定期 scp 到本地。

### 恢复

```bash
docker compose exec -T postgres psql -U postgres travel_agent \
    < /opt/backup/pg-20260524.sql
```

---

## 8. 常用运维命令

```bash
cd /opt/breezetravel

# 看所有服务状态
docker compose ps

# 跟踪某个服务实时日志
docker compose logs -f backend
docker compose logs -f frontend
docker compose logs -f y-websocket

# 重启单个服务（不 rebuild）
docker compose restart backend

# 只 rebuild + 重启某个服务（其他不动）
docker compose up -d --build backend

# 完全停止（不删数据）
docker compose down

# 完全停止 + 清空数据（⚠️ 危险，volume 一起删）
docker compose down -v

# 进入容器 shell 调试
docker compose exec backend bash
docker compose exec postgres psql -U postgres -d travel_agent

# 看资源占用
docker stats --no-stream
```

---

## 9. 已知降级行为（不是 bug）

| 现象 | 原因 | 影响 |
|------|------|------|
| LoRA Router 没加载，降级到 DeepSeek | `backend/models/router_lora` 未推送到服务器（500MB 权重未入库） | 仅 +1-2s Router 延迟，准确率从 91% → 88%，可接受 |
| 高德 API 返回 `USER_DAILY_QUERY_OVER_LIMIT` | 免费版 Web 服务 Key 日额度 100 次用尽 | 自动降级到本地 fixture mock 数据，第二天 0 点自动重置 |
| 和风天气 API 调用失败 | JWT 凭据过期或网络问题 | 跳过天气富集，行程其他部分正常 |

### 提升高德配额方案

- A. 控制台个人开发者实名认证 → 配额提升到 5000-30000/日
- B. 加 Redis 缓存层复用相同查询结果（已在 `Optimizer` 模块实现，TTL 24h）

---

## 10. 故障排查

### 后端启动失败 / 反复重启
```bash
docker compose logs backend | tail -50
```
常见原因：
- `.env` 里某个 key 写错（DEEPSEEK / OPENAI / 高德）
- Postgres 还没 healthy 但 backend 已启动 — compose 已配 `depends_on: condition: service_healthy`，正常不会发生
- `requirements.txt` 改了但没 `--build`

### 公网域名打不开
按顺序检查：
1. `curl http://localhost:3000` — 前端容器内部是否正常
2. `curl https://www.breezetravel.cn` — nginx 转发是否正常
3. `sudo nginx -t` — nginx 配置语法
4. `sudo systemctl status nginx` — nginx 是否在跑
5. 安全组 80/443 是否放行

### SSE 流式对话卡顿 / 中断
- nginx `/api/` 必须设 `proxy_buffering off` + `proxy_read_timeout 300s`
- 检查后端日志看是不是 LLM API 超时

### WebSocket 连接失败
- 浏览器 F12 Console 看具体错误
- `wss://...failed` → 多半是 nginx `/yjs/` 配置缺 `proxy_set_header Upgrade` 和 `Connection "upgrade"`
- 用 curl 测：`curl -sI -H "Upgrade: websocket" -H "Connection: Upgrade" https://www.breezetravel.cn/yjs/test` — 拿到 `405` 或 `101` 都说明 nginx 转发到了，拿到 `404/502` 说明转发挂了

### RAG 检索 0 命中
- 检查是否入库：`docker compose exec postgres psql -U postgres travel_agent -c "SELECT COUNT(*) FROM travel_chunks;"`
- 若为 0，重跑 `docker compose exec backend python -m scripts.ingest_notes`

### `/api/*` 所有端点 404（含 auth、chat、cities）

根因：nginx `/api/` 的 `proxy_pass` 末尾多了斜杠（`http://127.0.0.1:8000/`），导致路径前缀被剥离，FastAPI 收到的是 `/auth/email-register` 而非 `/api/auth/email-register`。

诊断：
```bash
# 直连后端（绕过 nginx）— 应该 200
curl -s -X POST http://localhost:8000/api/auth/email-register \
  -H "Content-Type: application/json" -d '{"email":"x@x.com","password":"Test1234"}'

# 经 nginx — 看日志里路径有没有 /api 前缀
docker compose logs backend --tail 20 | grep "POST\|GET"
```

修复：
```bash
sed -i 's|proxy_pass http://127.0.0.1:8000/;|proxy_pass http://127.0.0.1:8000;|' \
    /etc/nginx/sites-available/breezetravel.cn
nginx -t && systemctl reload nginx
```

### docker-compose 1.29.2 报 `KeyError: 'ContainerConfig'`

原因：旧版 Python 版 docker-compose（1.29.2）不兼容新版 Docker 构建出的镜像格式（缺少 `ContainerConfig` 字段）。

```bash
# 临时解决：删旧容器再重建
docker-compose rm -f backend
docker-compose up -d --no-deps backend
```

根本解决：升级到 Docker Compose V2（`docker compose`，Go 实现）：
```bash
mkdir -p ~/.docker/cli-plugins
curl -SL https://github.com/docker/compose/releases/download/v2.27.0/docker-compose-linux-x86_64 \
    -o ~/.docker/cli-plugins/docker-compose
chmod +x ~/.docker/cli-plugins/docker-compose
docker compose version   # 验证
```

### `docker compose build` 耗时数小时 / pip 依赖解析卡住

原因：`requirements.txt` 里 `mcp>=1.0.0` 等松散约束触发 pip resolver 遍历 20+ 版本；国内 ECS 直连 PyPI 下载速度 20-35 kB/s。

`backend/Dockerfile` 已修复，pip install 行加了阿里云镜像：
```dockerfile
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt
```

`requirements.txt` 所有依赖已固定为精确版本，消除 resolver backtracking。重新 build 约 2-4 分钟。

---

## 11. 发布与部署边界

仓库不启用 GitHub Actions 自动部署。代码推送与服务器发布是两个独立动作：推送到 GitHub 不会自动修改服务器。

服务器继续使用手动、可审计的部署流程：

```bash
cd /root/breezetravel
git pull --ff-only
docker compose up -d --build --remove-orphans
curl -f http://localhost:8000/health
```

执行前应确认目标分支、环境变量、数据库迁移和回滚点；健康检查成功只证明服务存活，不等同于完整业务验收。

---

## 12. 部署架构示意

```
                          ┌──────────────────┐
                          │   用户浏览器      │
                          └────────┬─────────┘
                                   │ https / wss
                                   ▼
                ┌──────────────────────────────────────┐
                │  阿里云 ECS (公网 IP)                 │
                │  ┌────────────────────────────────┐  │
                │  │  Nginx 1.24                    │  │
                │  │  443 ssl (Let's Encrypt)       │  │
                │  │                                │  │
                │  │  / → :3000 (frontend)          │  │
                │  │  /api/ → :8000 (backend)       │  │
                │  │  /yjs/ → :1234 (y-websocket)   │  │
                │  └────────────────────────────────┘  │
                │              │                       │
                │  ┌───────────▼──────────────────┐    │
                │  │  Docker Compose 编排          │    │
                │  │  ┌────────┐ ┌──────────┐     │    │
                │  │  │frontend│ │ backend  │     │    │
                │  │  │ :3000  │ │  :8000   │     │    │
                │  │  └────────┘ └────┬─────┘     │    │
                │  │  ┌──────────────┘            │    │
                │  │  ▼                           │    │
                │  │  ┌─────────┐ ┌─────────┐     │    │
                │  │  │postgres │ │  redis  │     │    │
                │  │  │ +vector │ │         │     │    │
                │  │  │  :5432  │ │  :6379  │     │    │
                │  │  └─────────┘ └─────────┘     │    │
                │  │  ┌─────────────────────┐     │    │
                │  │  │ y-websocket  :1234  │     │    │
                │  │  └─────────────────────┘     │    │
                │  └──────────────────────────────┘    │
                └──────────────────────────────────────┘
                                   │
                                   ▼
                ┌──────────────────────────────────────┐
                │  外部服务                             │
                │  - DeepSeek API（主 LLM）             │
                │  - SiliconFlow（Embedding / 备用 LLM） │
                │  - 高德地图 REST                      │
                │  - 和风天气                           │
                │  - LangSmith（可观测，可选）          │
                └──────────────────────────────────────┘
```
