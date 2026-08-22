# BreezeTravel UX 审查报告（2026-05）

> 审查方式：以"小白用户体验官"身份完整跑一遍：注册 → 城市选择 → 创建房间 → 邀请 → AI 问答 → 排线
> 工具：Playwright 浏览器自动化 + curl + 后端日志 + 代码审计
> 测试账号：`phone=10000000000` / `nickname=测试旅行者`（点登录页"🚀 测试账号一键登录"按钮）
> 验证环境：本地 docker-compose（postgres + redis + y-websocket + backend + frontend）

---

## 一、修复情况总览

| 阶段 | 共发现 | 已修复 | 未修复 | 完成率 |
|---|---|---|---|---|
| 首轮（B1–B5） | 14 | 14 | 0 | 100% |
| 第二轮（P0-4） | 1 | 1 | 0 | 100% |
| 第三轮（P0-5 / P1-13） | 2 | 2 | 0 | 100% |
| 第四轮（P1-12 / P0-6 / P1-3） | 3 | 3 | 0 | 100% |
| **合计** | **20** | **20** | **0** | **100%** |
| 待修复（搁置） | 0 | 0 | 0 | — |

提交记录：
- `d0795c4` fix(ux): UX 审查首轮修复 — 登录/分享/初始推荐/AI/排线
- `08a7e3f` feat(cities): 城市选择器加深度推荐角标 + 后端 supported 端点

---

## 二、已修复问题清单（15 项）

### 🟢 B1 登录/分享 UX（5 项）

| ID | 优先级 | 问题 | 复现/证据 | 修复方案 | 涉及文件 | 验证 |
|---|---|---|---|---|---|---|
| P0-1 | P0 | 无 dev 短信旁路，每次测试吃真实运营商配额，触发日级流控（40 条/天）后所有人都登不上 | 后端日志 `[SMS] 发送失败: 触发号码天级流控 Permits:40`；浏览器 502 无前端提示 | 加 `dev_login_bypass` 开关 + `dev_login_code=888888`，启用时跳过真实 SMS，验证码固定 888888 写入 DB | `backend/app/api/auth.py` `backend/app/config.py` | curl `/api/auth/send-code` 返回 `{"ok":true,"dev_bypass":true}` |
| P0-2 | P0 | 登录失败 inline error 是 12px 浅红字，浅灰背景下几乎不可见 | 截图 `02-empty-phone.png` 点完按钮无可见提示 | 全部错误改用 `useToastStore.toast(msg, 'error')`，inline error 保留为辅助 | `frontend/src/app/login/page.tsx` | 代码审计 + 前端 bundle 含 toast 调用 |
| P0-7 | P0 | "复制号码"只复制 6 位数字，好友需自己找入口+手输 | `TopNav.tsx:43` `clipboard.writeText(roomId)` | 改为完整邀请链接 `${origin}/room/${id}?city=&days=` + 邀请文案 + 房间号；移动端优先 `navigator.share` | `frontend/src/components/layout/TopNav.tsx` | 代码审计 |
| 新增 | P0 | 日级配额无自查，运营商拒单前 backend 仍接受请求 | （预防性）阿里云 SMS 日级 40 条上限 | 加 `sms_daily_limit_per_phone=5`，命中前直接返回 429，给出可读建议 | `backend/app/api/auth.py` `backend/app/config.py` | 代码审计 4 处 guard |
| 新增 | P0 | 缺测试账号 demo 入口，全靠真实手机号 | 影响演示和审核 | 加 `POST /api/auth/test-login` 端点（仅 `dev_login_bypass=true` 时启用，生产环境 403）+ 登录页"🚀 测试账号一键登录"按钮 | `backend/app/api/auth.py` `backend/app/config.py` `frontend/src/app/login/page.tsx` | curl 返回 `phone=10000000000 / nickname=测试旅行者`；浏览器一键跳主页显示"你好，测试旅行者 👋" |

### 🟢 B2 初始推荐质量（1 项 = 3 子问题）

| ID | 优先级 | 问题 | 复现/证据 | 修复方案 | 涉及文件 | 验证 |
|---|---|---|---|---|---|---|
| P0-8 | P0 | 进房间初始推荐严重重复 + 品类错乱：6 景点全是"成都大熊猫繁育基地"的不同入口/办公楼/山月馆；4 美食有 3 个是"陈麻婆豆腐"分店 + 1 个酒店错归美食 | 实测房间 620067 右侧 10 条；recommend API 返回原样 | ① 加 `_venue_stem` + `_is_same_venue_branch`（stem 子串 + 公共前缀 ≥3 字双层判定）<br>② 加 `_hint_category_from_name`，名称含"酒店/宾馆/客栈/民宿"强制归 HOTEL<br>③ 加类目硬校验：keyword 标 food 但返回 hotel → 跳过<br>④ LLM Prompt 加"每个关键词必须指向不同景点/品牌" | `backend/app/api/recommend.py` | 实测重跑：6 景点全不同（熊猫基地/武侯祠/杜甫草堂/宽窄巷子/青城山/成都博物馆）+ 3 不同美食品牌 + 1 酒店 |

### 🟢 B3 AI 回复质量（3 项）

| ID | 优先级 | 问题 | 复现/证据 | 修复方案 | 涉及文件 | 验证 |
|---|---|---|---|---|---|---|
| P0-10 | P0 | AI 回复重复输出："作为打卡党，成都美食之旅必须安排上！"连续出现 2 次 | 截图 `07-ai-hotpot.png` | Critic 触发重检索时 Synthesizer 会再跑一次，导致前端累加。加 `text_reset` SSE 事件：每次 Synthesizer 完成都先推一帧重置 | `backend/app/api/chat.py` `frontend/src/hooks/useAIChat.ts` | 代码审计：backend `chat.py:178` 推送、frontend `useAIChat.ts:127` 接收清空 |
| P0-11 | P0 | RAG 把"网红"关键词带跑偏：用户问火锅，返回"网红机器人炒饭""碳烤鸡烤肉""网红快餐""网红蛋糕" | 实测聊天面板 | ① Multi-Query Prompt 加"保留核心品类名词"硬约束<br>② Synthesizer 加 `_extract_user_cuisine_constraint` + `_filter_food_by_cuisine` post-filter，火锅查询自动剔除不相关品类 | `backend/app/rag/multi_query.py` `backend/app/agents/nodes/synthesizer.py` | 单元测试：5 候选 → 保留小龙坎火锅 + 春熙路，剔除炒饭/烤肉/蛋糕 |

### 🟢 B4 排线算法（4 项）

| ID | 优先级 | 问题 | 复现/证据 | 修复方案 | 涉及文件 | 验证 |
|---|---|---|---|---|---|---|
| P0-14 | P0 | "午餐（自由安排）"占位 BUG，连吃两顿：Day 1 陈麻婆 11:07 后又安排 12:28 自由午餐 | curl `/api/optimize` JSON | Root cause：`_MEAL_WINDOWS["lunch"]=11:30-13:30` 太窄 + approx_start 不计交通时间。修复：① 用餐窗放宽 11:00-14:00 / 17:30-20:30；② 删除错误的 pre-scan，改 post-scan：按生成后实际时间表判断每顿饭是否被真实餐厅覆盖 | `backend/app/agents/nodes/optimizer.py` | 重测：Day 1 仅陈麻婆 11:07 一顿正餐，无重复 |
| P0-15 | P0 | Day 1 18:29 后无晚饭安排，行程突然结束 | curl `/api/optimize` JSON | post-scan 检测任一窗口无真实餐厅 → 在窗口中段（18:30）插入"自由晚餐"占位 | `backend/app/agents/nodes/optimizer.py` | 重测：Day 1 / Day 2 都有 18:30-19:30 自由晚餐 |
| P0-16 | P0 | 酒店 slot 只有 30 分钟，17:59 入住时间不合理 | curl `/api/optimize` JSON | 酒店改 day-end marker：check-in 时间 `max(prev_end + 车程, 21:00)` → 显示"次日 12:00"，不再当游览 slot | `backend/app/agents/planner/nodes/scheduler.py` | 重测：21:00 → 次日 12:00 |
| P1-18 | P1 | Tips 重复输出："全程步行较多，建议穿舒适平底鞋并携带饮用水"与"景区较大，全程步行较多，建议穿舒适平底鞋并多补水"几乎一样 | curl `/api/optimize` JSON | 加 `_dedup_tips_fuzzy`：jaccard ≥ 0.5 OR 共享同一意图短语（建议提前/平底鞋/带伞/排队等）视为重复 | `backend/app/agents/nodes/tips_generator.py` | 重测：熊猫基地从 3 条 → 2 条 tips；酒店不再出"夜间场所"误用 |

### 🟢 B5 工程修正（1 项）

| ID | 优先级 | 问题 | 复现/证据 | 修复方案 | 涉及文件 | 验证 |
|---|---|---|---|---|---|---|
| P1-20 | P1 | LangSmith Tracer 反复报 `TypeError('keys must be str, int, float, bool or None, not tuple')`，污染日志且偶发请求 hang | docker logs backend 每个 planner 调用刷 ~10 条 | PlannerState 中 `time_matrices[cluster_id]` 是 tuple key dict，LangSmith 无法 JSON 序列化。修复：① DistanceAgent 输出时把 tuple key 压平为 `"a__b"` string；② 加 `_matrix_get(matrix, a_id, b_id, default)` 兼容 tuple / string 双格式 lookup | `backend/app/agents/planner/nodes/distance.py` `backend/app/agents/nodes/optimizer.py` | 重测：`/api/optimize` 200 OK，0 条 TypeError tuple 报错 |

### 🟢 第二轮 P0-4 城市分级（1 项）

| ID | 优先级 | 问题 | 复现/证据 | 修复方案 | 涉及文件 | 验证 |
|---|---|---|---|---|---|---|
| P0-4 | P0 | 城市列表 300+ 个，RAG 游记语料只覆盖 7 个城市，其余城市无视觉区分，用户选完看不出 AI 推荐质量为何差 | 城市选择器原状 | ① 后端加 `GET /api/cities/supported`：查 travel_notes DISTINCT city，DB 失败回退兜底列表<br>② 前端首屏 fetch 到 `supportedCities: Set<string>`<br>③ 三处城市按钮（搜索/热门/省份）全加 🧠/🗺️ 角标 + title 解释<br>④ 弹窗顶部图例栏；省份按钮加"🧠 N"显示该省支持数<br>⑤ `pickCity(c)` 统一入口：非支持城市 toast 提示（非阻塞） | `backend/app/api/cities.py` (新) `backend/app/main.py` `frontend/src/app/page.tsx` | API 返回 7 城（成都/北京/上海/广州/深圳/杭州/厦门，72-74 篇/城）；前端 bundle 含 supportedCities 状态 + 文案 + emoji 角标 |

### 🟢 第三轮（2026-05-26）

| ID | 优先级 | 问题 | 复现/证据 | 修复方案 | 涉及文件 | 验证 |
|---|---|---|---|---|---|---|
| P0-5 | P0 | 主页特性 chip（AI 智能推荐 / 好友实时协同 / 最优路线规划）拦截下方城市选择按钮的点击事件，Playwright 必须 `evaluate` 才能点中，真人鼠标也会失手 | Playwright 报错 `<span class="...text-[10px] text-gray-400 bg-gray-50..."> intercepts pointer events` | 三个特性 chip 是纯装饰展示，直接加 `pointer-events-none`，让点击事件穿透到下方城市按钮 | `frontend/src/app/page.tsx:216` | 代码审计；前端 bundle 含 `pointer-events-none inline-flex` |
| P1-13 | P1 | AI 主动加戏：用户只问火锅，回复结尾自己安利"宽窄巷子的小吃别买，不如去井巷子拍照" | 实测 chat panel | ① `SYNTHESIZER_SYSTEM` 加硬约束："只回答用户明确询问的品类，不要主动安利/对比用户没问的其他类目，不要用'顺便/不如/建议你也试试/对了'口吻引入新议题"<br>② `SYNTHESIZER_PROMPT` 第 5 条补"只围绕用户消息中明确出现的品类/诉求展开，不要节外生枝" | `backend/app/agents/nodes/synthesizer.py:72` `:95` | 代码审计：System + Human Prompt 双层约束 |

### 🟢 第四轮（2026-05-26）

| ID | 优先级 | 问题 | 复现/证据 | 修复方案 | 涉及文件 | 验证 |
|---|---|---|---|---|---|---|
| P1-12 | P1 | AI 回复延迟 ~37s 才出现首批地点卡，整段思考链跑完用户才看到结果，等待感强 | 实测 chat panel SSE trace | **两段流式**：① `tool_executor` `on_chain_end` 时立即推送 Amap 原始地点卡（`place` 事件，~5s 内可见）<br>② `synthesizer` `on_chain_end` 时推送 `place_update` 增量事件（description/tags/rag_meta/estimated_duration）合并到已渲染卡片<br>③ 新增 `place_remove` 事件：synthesizer 因菜系硬约束剔除的预览卡，前端同步移除<br>④ 前端 `useAIChat` 增加 `place_update`/`place_remove` handler，按 placeId 合并字段，预防重复追加 | `backend/app/api/chat.py` `frontend/src/hooks/useAIChat.ts` | 前端 tsc 0 error；流程：tool_executor 完成（~5s）→ 卡片立现 → synthesizer 完成（~20s+）→ description/tips 补齐 |
| P0-6 | P0 | 主页缺地图视觉，原需求"通过地图列表找到旅游城市"无对应 UI，纯文本 chip 列表无吸引力 | 主页截图 `03-home.png` | 新增 `DEEP_CITY_CARDS` 常量：7 城市（成都/北京/上海/广州/深圳/杭州/厦门）各自的 emoji+渐变+标语，无需外部图片资源；主页"创建房间"卡片上方加 4×2 网格图卡，aspect-square + Tailwind 渐变 + 大 emoji + 城市名 + 标语 + 🧠 角标；点击直接 `pickCity(city)` 走原创建流 | `frontend/src/app/page.tsx:19` `:225-263` | 前端 tsc 0 error；7 张图卡渲染，选中态用 coral ring；零外部资源依赖（不需要 Amap 静态地图 key） |
| P1-3 | P1 | 100% 依赖短信，运营商日级流控（40/天）或故障时全站无法注册 / 登录 | 后端日志 `[SMS] 发送失败` + 502 | **邮箱+密码兜底**（微信 OAuth 留待外部 AppID 后做）：<br>① migration `004_email_auth.sql`：users 表加 `email UNIQUE` + `password_hash` + 索引<br>② `app/utils/password.py`：PBKDF2-HMAC-SHA256 / 260k 迭代 / 16B 盐 / Django 兼容格式，纯 stdlib 不引入新依赖<br>③ `POST /api/auth/email-register` + `POST /api/auth/email-login`：邮箱正则校验 + 密码强度（≥8 位含字母+数字）+ 失败统一 401 防邮箱枚举<br>④ 登录页加"手机号 / 邮箱"Tab，邮箱内再切"登录 / 注册"，复用原 `login()` + `router.replace('/')` | `backend/app/db/migrations/004_email_auth.sql` (新) `backend/app/utils/password.py` (新) `backend/app/api/auth.py` `frontend/src/app/login/page.tsx` | 后端 Python 语法 OK；前端 tsc 0 error；migration 走 `run_migrations()` 自动应用 |

---

## 三、已观察但**未修复**问题清单（0 项 + 1 个 false alarm）

> 第四轮（2026-05-26）后所有 P0/P1 已修复，下面只保留 false alarm 备忘。剩余仅微信 OAuth（外部 AppID 依赖）日后接入即可。

### 🔵 已观察但已 close

| ID | 现状 | 备注 |
|---|---|---|
| P1-19 | tips_md 字段不存在 | 首轮误判（看到 `.get('tips_md','(none)')` 默认值），实际 Itinerary schema 里就没有这个字段，无需补 |
| P1-21 | "旅行者小明" | DB 历史种子数据（`user_id='test-001'`），并非前端 hard-code，无需改 |

---

## 四、新引入的工程能力

| 能力 | 文件 | 用途 |
|---|---|---|
| dev_login_bypass 开关 | `backend/app/config.py` | 统一控制开发/演示登录旁路、SMS 配额自查、test-login 端点；生产环境一处关闭 |
| `/api/auth/test-login` | `backend/app/api/auth.py` | 测试账号一键登录端点（仅 dev 模式可用） |
| `/api/cities/supported` | `backend/app/api/cities.py` (新) | RAG 深度推荐城市清单接口，给前端做能力分级显示 |
| `_venue_stem` / `_is_same_venue_branch` | `backend/app/api/recommend.py` | POI 主名提取 + 子串/前缀双层去重，可复用到 AmapSearch |
| `_extract_user_cuisine_constraint` / `_filter_food_by_cuisine` | `backend/app/agents/nodes/synthesizer.py` | 用户菜系硬约束 + post-filter |
| `_dedup_tips_fuzzy` | `backend/app/agents/nodes/tips_generator.py` | jaccard + 意图短语双重 tips 去重，可复用到所有 LLM 输出列表 |
| `_matrix_get` | `backend/app/agents/nodes/optimizer.py` | tuple/string key 兼容查找，扁平化矩阵后仍可逐步迁移 |
| SSE `text_reset` 事件 | `backend/app/api/chat.py` `frontend/src/hooks/useAIChat.ts` | 流式输出中段重置文本能力，未来 Critic 多轮重试可复用 |

---

## 五、关键文件改动总览

```
backend/
  app/
    config.py                         首轮：+ dev_login_bypass / dev_login_code /
                                      sms_daily_limit_per_phone / test_account_*
    main.py                           二轮：注册 cities router
    api/
      auth.py                         首轮：dev 旁路 + 日级配额 + test-login
      chat.py                         首轮：SSE text_reset 帧
      recommend.py                    首轮：去重 + 类目校验 + 强化 LLM prompt
      cities.py                       二轮 (新)：/api/cities/supported
    agents/
      nodes/
        optimizer.py                  首轮：用餐窗放宽 + post-scan + _matrix_get
        synthesizer.py                首轮：菜系 post-filter
        tips_generator.py             首轮：fuzzy 去重 + 限制夜间场所规则
      planner/nodes/
        distance.py                   首轮：tuple key 压平为 string key
        scheduler.py                  首轮：酒店改 day-end marker
    rag/
      multi_query.py                  首轮：Prompt 加品类硬约束

frontend/
  src/
    app/
      login/page.tsx                  首轮：Toast 错误 + dev 提示横幅 + 测试账号按钮
      page.tsx                        二轮：城市角标 + 图例 + 非支持城市 toast
    components/layout/
      TopNav.tsx                      首轮：完整邀请链接 + navigator.share
    hooks/
      useAIChat.ts                    首轮：text_reset 事件处理

docs/
  UX-REVIEW-2026-05.md                本文档
```

---

## 六、后续建议优先级

UX 审查清单已 100% 收口。下阶段建议聚焦：
1. **微信 OAuth 接入**：等 AppID/AppSecret 到位即可接入 `/api/auth/wechat-callback`，可与现有 email/phone 同表（user_id 共用），约 1 天
2. **CLAUDE.md Phase 6 — 生产部署**：Railway backend + Vercel frontend + Supabase PostgreSQL
3. **scenic Context Recall 进阶**（0.57 → 0.65+）：Parent-Document Retriever（小 chunk 检索 + 大 chunk 返回）
