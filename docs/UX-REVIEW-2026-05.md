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
| **合计** | **15** | **15** | **0** | **100%** |
| 待修复（搁置） | 5 | 0 | 5 | — |

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

---

## 三、已观察但**未修复**问题清单（5 项 + 1 个 false alarm）

### 🟡 已搁置到下一轮

| ID | 优先级 | 问题 | 现状 | 待办方案（建议） | 估时 |
|---|---|---|---|---|---|
| **P0-5** | P0 | 城市选择弹窗中"成都"等热门按钮被顶部 chip 遮挡，Playwright 必须用 `evaluate` 才能点中，真实用户鼠标也会点不中 | Playwright 报错 `<span class="...inline-flex items-center gap-1 text-[10px] text-gray-400 bg-gray-50 px-2 py-1 rounded-full border border-gray-100"> intercepts pointer events` | 找到 chip（在 `frontend/src/app/page.tsx` 城市弹窗顶部 chip 行）加 `pointer-events-none`，或重新调整 z-index/层级 | 30 min |
| **P0-6** | P0 | 主页缺"地图视觉/城市图片"，与原需求"通过地图列表找到旅游城市"不符 | 当前是纯文本 chip 列表，没有任何缩略图/封面 | 7 个深度城市改成 4×4 图卡（用 Amap 静态地图 API 或本地封面图），点击直接进创建流；其他城市保留 chip | 1-2 day（含图片素材） |
| **P1-3** | P1 | 100% 依赖短信，缺微信 OAuth / 邮箱兜底，运营商一故障全站不可用 | 当前只有手机+短信验证码（开发模式 888888） | 加微信扫码 OAuth 或邮箱+密码登录 | 2-3 day |
| **P1-12** | P1 | AI 回复延迟 ~37 秒（26 步思考链），用户等待感强 | 实测 chat panel 流式 trace | Synthesizer 改纯 streaming：首批 4-5 个地点卡 10s 内可见，剩余慢慢补；考虑用 deepseek-chat-fast 或缓存中间结果 | 1 day |
| **P1-13** | P1 | AI 主动加戏：用户只问火锅，结尾自己安利"宽窄巷子的小吃别买，不如去井巷子拍照" | 实测 chat panel | Synthesizer prompt 加"仅回答用户明确询问的品类，不主动推荐其他类目" | 1 hour |

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

立即可做（≤ 2 小时）：
1. **P1-13**：Synthesizer prompt 加约束句，避免 AI 加戏
2. **P0-5**：城市弹窗 chip pointer-events 修复

下个 Sprint：
3. **P1-12**：Synthesizer 真流式（用户体感最重要）
4. **P0-6**：城市卡片化 + 封面图（视觉吸引力）
5. **P1-3**：登录兜底通道
