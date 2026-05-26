# BreezeTravel 智能化升级 SPEC（2026-05-26）

> 目标：把 BreezeTravel 从「炫酷玩具」打磨成「能实际用」的协同旅行规划产品。
> 主线：**先修排线脱离现实的硬问题，再补推荐堆砌问题**。MVP 起点产品仅覆盖出发前规划阶段（不做导出 / 路上 / 现场动态调整）。

---

## 0. 关键决策摘要（Why this spec exists）

| # | 决策 | 选项 |
|---|------|------|
| D1 | 核心痛点 | 全部四项：堆砌、脱离现实、不懂用户、协同流于形式 |
| D2 | 目标场景 | 2–4 人朋友/情侣短途 2–3 天（MVP 首焦） |
| D3 | 排线硬约束 | 营业时间 + 实际游玩时长 + 用餐时段强制 + 天气&体力曲线 |
| D4 | 推荐不堆砌 | 主打「对比 + 替代方案」 |
| D5 | 时长/营业数据源 | 高德 POI 详情 + 游记 RAG 推断（混合补全） |
| D6 | 品类分隔 | 鱼骨节奏模板 + 二级 tag 硬约束 + diversity_penalty 三者组合 |
| D7 | 投票仲裁 | 默认取高票 + 帮助文案（最小侵入） |
| D8 | 增量调整 | 局部重规划（按天切片重跑） |
| D9 | 增量调整实现 | EditorAgent 调度 + 规则快路径混合 |
| D10 | 成本期望 | 质量 >> 成本（不计较 LLM 量） |
| D11 | 理由可信 | 自产高质量游记 + LLM 强制引用游记内容 |
| D12 | 偏好冲突 | 群体偏好并集 + AI 明示「选三保二」 |
| D13 | 游记来源 | 三者混合：爬取骨架 + DeepSeek 多 persona 改写 + 人工抽检 |
| D14 | 数据质量 | place_meta 表「置信度」字段 + 交叉验证 + 运行时回填 |
| D15 | MVP 范围 | **先排线后推荐**（Phase A 排线落地，Phase B 推荐升级） |
| D16 | 节奏模板 | 预设多套模板，按 persona/偏好选一套 |
| D17 | 引用 UI | 地点卡背面「为什么推荐」面 |
| D18 | 天气适配 | 生成时就应用，面板明示原因 |
| D19 | 出品能力 | MVP 仅出发前规划阶段（导出/路上/现场调整后续） |
| D20 | 过载处理 | AI 自主取舍 + 备选列表可交换 |
| D21 | 质量防线 | Critic 升级「结果检验」（硬规则） |
| D22 | 状态隔离 | PlannerState 按天切片 |
| D23 | 上线衡量 | RAGAS + agent_eval 为主，不新增 |
| D24 | 取舍评分 | 投票权重优先 |
| D25 | LLM 抹黑 | Schema + 重试 + Critic 报警 + 降级路径 |
| D26 | 反馈机制 | MVP 不做 |

---

## 1. 范围

### In scope（本 SPEC 覆盖）
- 排线引擎升级：营业时间 / 实际 dwell / 用餐时段 / 天气 / 体力曲线
- 品类分隔（鱼骨模板 + 二级 tag + diversity_penalty）
- 推荐升级：主题打包 + 对比替代 + 引用游记原文
- 多人协同：偏好并集 + 高票优先 + 「选三保二」提示
- 增量对话：EditorAgent 局部重规划 + diff 应用
- 数据基建：`place_meta` 置信度表 + 多来源交叉
- 自产高质量游记 pipeline（爬骨架 + DeepSeek 多 persona 改写 + 人工抽检）
- Critic 结果检验硬规则
- LLM 抹黑：Schema + 重试 + 降级

### Out of scope（本期不做）
- 路上导航面板 / 现场动态调整 / 一键导出第三方地图
- 用户反馈收集（隐式/显式）
- 商业化、登录鉴权、计费
- 路线代价（交通费/门票预算）智能优化

---

## 2. 数据基建

### 2.1 `place_meta` 表（新增）

```sql
CREATE TABLE place_meta (
  place_id           TEXT PRIMARY KEY,         -- 高德 POI ID
  name               TEXT NOT NULL,
  city               TEXT NOT NULL,
  category_l1        TEXT,                     -- 景点/餐饮/住宿/购物/...
  category_l2        TEXT,                     -- 火锅/博物馆/咖啡馆/主题乐园/...
  open_hours_json    JSONB,                    -- {mon:[[9,17]], tue:[...], ...}
  open_hours_conf    TEXT CHECK (open_hours_conf IN ('high','medium','low')),
  dwell_minutes      INT,
  dwell_conf         TEXT CHECK (dwell_conf IN ('high','medium','low')),
  need_reservation   BOOLEAN DEFAULT FALSE,
  reservation_conf   TEXT,
  peak_hours_json    JSONB,                    -- 高峰时段，软提示
  source_breakdown   JSONB,                    -- {amap:..., rag:..., llm_inferred:...}
  updated_at         TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_place_meta_city_cat ON place_meta(city, category_l1, category_l2);
```

**置信度规则：**
- `high`：高德 API 返回 / 人工核对
- `medium`：LLM 从游记原文抽取（有 chunk_id 引用）
- `low`：LLM 仅按品类默认值推断（博物馆 120min / 咖啡馆 60min / 餐厅 75min / 主题乐园 360min / 街区 90min）

**Scheduler 使用置信度：**
- `high` 直用 → `medium` 直用但 ±10 min buffer → `low` ±20 min buffer 并不强制硬规则
- 多来源冲突时 Critic 报警，写入 `data_warnings` 日志，运行时取 `high > medium > low`

### 2.2 数据填充 Pipeline

| 阶段 | 内容 | 频率 |
|------|------|------|
| 预热 | 7 城市 top 200 POI 走高德 API 拉 business_hours/dwell（部分字段缺失） | 一次性 |
| RAG 补强 | `scripts/extract_place_meta_from_rag.py`：扫描每个 POI 在 RAG chunk 中的提及，LLM 抽取「营业到 X 点 / 玩 Y 小时 / 需预约」 | 入库后一次 |
| 运行时回填 | Synthesizer 触发 Place 时，若 place_meta 缺字段 → 异步 LLM 推断 → 写回 DB（confidence=low） | 实时 |
| 人工抽检 | 每周抽 20 条 high/medium 项核对，纠错并升级 conf | 周 |

### 2.3 游记语料三路混合

| 路径 | 数量 | 用途 |
|------|------|------|
| 少量爬取（马蜂窝/小红书） | 每城 30–50 篇 | 提供真实「营业时间 / 门票 / 避坑」事实骨架（合规风险自担，仅本地） |
| DeepSeek 多 persona 改写 + 多轮反思 | 每城 100+ 篇 | 改写真实骨架为不同视角文本，扩大语料；多轮反思查事实冲突 |
| 人工抽检 top 20% 高检索分语料 | 每月 | 防垃圾文本污染 RAG |

---

## 3. 排线升级（Phase A — MVP 焦点）

### 3.1 PlannerState 按天切片

```python
class PlannerState(TypedDict):
    # 全局只读
    places: list[Place]                    # 输入
    trip_days: int
    trip_city: str
    weather_forecast: dict[int, WeatherDay] # day_index -> 天气
    user_prefs: GroupPreferences           # 群体偏好（并集 + must/nice/no-go）

    # 全局产出
    hotels: list[Place]                    # 酒店挂载，全局
    centroid: tuple[float, float]

    # 按天切片
    day_states: dict[int, DayPlannerState] # day_index -> 该天 state

    # 调试 / 可观测
    trace: list[dict]
```

```python
class DayPlannerState(TypedDict):
    day_index: int
    template_id: str                       # 选用的鱼骨模板 id
    slots: list[Slot]                      # 鱼骨槽位 + 已填 place_id
    locked: bool                           # EditorAgent 标记为不可重排
    rationale: str                         # 该天为什么这样安排（LLM 短摘要）
```

### 3.2 鱼骨节奏模板

**预设模板（5 套，按 persona/偏好选一套）：**

| Template | 适用 | 槽位序列 |
|----------|------|----------|
| `T_DEEP_EXPLORE` | 深度文化型 | 上午-重头景点(180m) → 午餐(75m) → 街区漫步(90m) → 咖啡(60m) → 二级景点(90m) → 晚餐(90m) → 自由 |
| `T_NIGHTLIFE` | 夜生活型 | 中午起 → 午餐 → 景点 → 咖啡 → 晚餐 → 夜景 → 酒吧(120m) |
| `T_FAMILY_LIGHT` | 亲子轻松型 | 早午餐 → 亲子景点(120m) → 午休回酒店(90m) → 公园(90m) → 早晚餐(17:30) → 自由 |
| `T_ARRIVAL` | 抵达日 | 抵达 → 酒店 check-in → 周边晚餐 → 短散步 → 早休 |
| `T_DEPARTURE` | 离开日 | 早起 → 早午餐 → 1 个轻量景点 → 离开 |

**模板选择逻辑（router 阶段产出）：**
- Day 1 → 强制 `T_ARRIVAL`
- Day N（末日）→ 强制 `T_DEPARTURE`
- 中间天 → 根据 `user_prefs.style`（提问时收集）+ `user_prefs.has_kids` 选一套
- 模板**只是槽位序列**，每个槽位有：`category_l2 候选集合`, `预期时长 buffer`, `必须 / 可选`

### 3.3 Scheduler 升级算法

```
for day_index in range(trip_days):
    1. 选模板 → DayPlannerState.template_id
    2. 候选筛选：剔除当天闭馆 / 超出营业时间窗 / 不符合天气（雨天剔户外）
    3. 槽位填充（贪心 + 回溯）：
        - 必须槽（餐厅、重头景点）优先填
        - 同二级 tag 在同一天 ≤ 3（餐饮拆 lunch/dinner 分类）
        - 相邻两个槽不能同二级 tag
        - 槽位时长按 place_meta.dwell_minutes（缺则按品类默认）
        - 通勤时间走高德矩阵 API
    4. 时间链推算：
        - 9:00 起步 / 抵达日按高铁到时
        - 用餐窗 12:00–13:30 / 18:00–20:00 强制有餐厅 slot
        - 体力曲线：上午硬度上限 1.0 / 下午 0.7 / 晚间 0.4
    5. 天气适配（生成时）：
        - 雨天 → 户外槽 → 切换为室内候选
        - 夏季 11:30–14:00 → 户外槽改室内 / 餐厅
        - 冬季日落 16:30 后 → 后置槽切换室内 / 夜景
    6. 落不下的 place → 入 backup_pool（D20）
    7. 产出 DayPlannerState.rationale（LLM 1 句话总结这天为什么这样）
```

### 3.4 过载处理（D20）

- Scheduler 判定无法在 `trip_days` 内排进所有 must-have，标记 `overflow=True`
- AI 在 chat 里说明：
  > 你们选了 12 个必去点，但 3 天最多排进 8 个。我已按投票数保留：X、Y、Z…；剩下 A、B、C 我放进了「备选」，你们可以一键交换或加一天。
- 备选池在前端 itinerary 右侧抽屉「备选」展示，每项可点「换入第 N 天」

### 3.5 取舍评分（D24）

```
score(place) =
  + 100 if place in must_have                 # 锁定
  - ∞   if place in no_go                     # 剔除
  + votes(place) * 10                         # 投票主导
  + popularity_from_rag(place) * 3            # RAG 人气分（提及次数 + 评分）
  - route_cost_increment(place) * 1           # 路线代价（公里数+时间）
  + diversity_bonus(place, current_day) * 5   # 当天品类多样性奖励
```

### 3.6 Critic 结果检验升级（D21）

新增硬规则集（违反则触发 Planner 重跑，最多 1 次）：

| 规则 | 含义 |
|------|------|
| `R_OPEN_HOURS` | place 安排时间窗 ∉ open_hours_json |
| `R_NO_BACKTOBACK_L2` | 同一天相邻 slot 同 category_l2 |
| `R_MEAL_SLOT_FILLED` | 12:00–13:30 / 18:00–20:00 无餐饮 slot |
| `R_DAILY_FOOD_CAP` | 当天餐厅类 ≥ 4（≥ 4 顿饭不合理）|
| `R_ZERO_FOOD_DAY` | 全天无餐厅 |
| `R_WEATHER_MISMATCH` | 雨天大于 5mm 仍排 ≥ 2 个户外 |
| `R_BUFFER_DEFICIT` | 高德通勤时间 > slot 间间隔 → 时间链断裂 |

实现：`backend/app/agents/planner/nodes/critic.py`（新增），规则函数纯 Python，违规返回 `{rule, day_index, place_id, message}` 触发 Planner 重跑被影响的 day。

---

## 4. EditorAgent + 局部重规划（D8 / D9 / D22）

### 4.1 触发场景

用户在 chat 中说：
- 「换掉故宫」
- 「第二天太累，调轻松点」
- 「加个亲子项目」
- 「不想中午吃太晚，提早」
- 「换个咖啡馆」

### 4.2 双路径架构

```
chat 消息 → IntentClassifier (FT Router)
   ├─ 简单意图（删除 X / 互换 A↔B / 锁定某天） → Rule Fast Path
   │      直接对 day_states[N].slots 应用 patch，写回 itinerary
   │
   └─ 复杂意图（加亲子 / 调节奏 / 替换品类） → EditorAgent
          1. EditorAgent 接收 itinerary + user_msg
          2. LLM tool calling 输出 structured patch：
             - op: replace_place | add_place | remove_place | swap_days | rebuild_day
             - day_index, slot_index, place_id, constraints
          3. 后端 PatchApplier 应用 patch
          4. 触发 Planner 仅重跑 patch.day_index 那天
          5. 验证器（同 Critic 硬规则）跑一遍，失败则提示用户
```

### 4.3 Patch Schema

```python
class ItineraryPatch(BaseModel):
    op: Literal["replace_place", "add_place", "remove_place", "swap_days", "rebuild_day"]
    day_index: int
    slot_index: int | None = None
    target_place_id: str | None = None
    new_place_query: str | None = None        # 用于 RAG 检索新候选
    new_template_id: str | None = None        # rebuild_day 用
    rationale: str
```

### 4.4 前端 diff 高亮

- 应用 patch 后，新增/替换的 slot 用淡黄底高亮 2s
- 右侧抽屉显示「本次变更」摘要（why + 影响范围）

---

## 5. 推荐升级（Phase B）

### 5.1 输出 Schema

每个 Place 必须有：

```python
class PlaceRecommendation(BaseModel):
    place_id: str
    name: str
    category_l1: str
    category_l2: str
    reason: str                            # 为什么推荐（强制 ≥ 1 句）
    suitable_for: list[str]                # 适合谁：["情侣","摄影","深度文化"]
    avoid_tips: list[str]                  # 避坑：["周一闭馆","周末排队2h+"]
    source_chunk_ids: list[str]            # 引用游记 chunk，前端可点
    alternatives: list[Alternative]        # 1–2 个替代方案
    confidence: Literal["high","medium","low"]

class Alternative(BaseModel):
    place_id: str
    name: str
    why_alternative: str                   # "比 A 更适合带娃 / 更便宜 / 排队少"
```

### 5.2 引用强制（D11）

- Synthesizer Prompt 强约束：「reason 和 avoid_tips 必须引自下方 RAG 上下文，每条注明 chunk_id；如 RAG 未命中则不出该字段，宁缺勿瞎编」
- Critic 验证 `source_chunk_ids` 在本次检索 context 中存在，否则 reason 字段被剥离
- 前端地点卡正面按钮 → 翻到背面：reason / avoid_tips / 引用游记原文片段（可展开）

### 5.3 主题打包（D4 的延伸）

可选：用户首屏除「自由 chat」外提供「主题模板入口」：
- Citywalk 老城线
- 博物馆深度线
- 美食扫街线
- 亲子轻松线

点击后 Router 直接走预设 query，Synthesizer 按主题聚合 8–12 个 places。

---

## 6. 多人协同（D7 / D12）

### 6.1 偏好收集（D12 - 选三保二）

进房间后弹一次 onboarding（每人填一次）：
- **Must-have**（最多 3 项）：必须做的事/类型（"看博物馆", "吃辣火锅"）
- **Nice-to-have**（不限）：希望有的
- **No-go**（不限）：拒绝的（"避免坐车久", "不要排队景点"）

后端合并到 `GroupPreferences`：
- `must_have_union`：所有人 must-have 并集 → 必排（如冲突报警）
- `nice_to_have_union`：并集，参与评分加分
- `no_go_union`：并集，硬剔除

### 6.2 冲突明示（D12）

Synthesizer 检测到 must_have 间冲突（如 A 要"步行少"，B 要"必去 4 个景点"）→ chat 输出：
> 你们偏好难同时满足：A 想少走路、B 想多看景点。我会优先 4 个景点（B 票数更多），改成早晚分散打车，A 中午回酒店休息。如要反过来请告诉我。

### 6.3 投票仲裁（D7 - 最小侵入）

- AI 不主动仲裁
- Yjs 票数变化触发 Scheduler 重排（防抖 5s）
- chat 仅在票数极度分化（标准差 > X）时一次性提示：「Y 和 Z 投票相近且地点分散，我现在按高票排，要不要保留两个 / 加一天 / 看替代方案？」

---

## 7. LLM 抹黑（D25）

| 层 | 措施 |
|---|------|
| 调用层 | 全部 LLM 调用走 structured output（DeepSeek function calling / JSON mode） |
| 解析层 | Pydantic 校验失败 → 重试 2 次 → 降级用骨架默认值（不报错给用户） |
| Critic 层 | 验证 Place schema 完整性、引用 chunk_id 真实存在、硬规则违反则重跑 1 次 |
| 监控 | 每次 LLM 调用记 `success / retry_count / fallback_used` 到 `/metrics` |

---

## 8. 衡量与回归（D23）

- **现有 `RAGAS` 指标** 保持运行：Faithfulness / Answer Relevancy / Context Recall
- **现有 `agent_eval.py`**：跑前后对比 FT Router 准确率、Synthesizer 成功率
- 新增 `tests/test_planner_v2.py` golden test：
  - 6–10 个 typical 输入 → 人工核可接受 itinerary
  - CI 每次 PR 跑，diff > 阈值需人工 review
- 新增 `tests/test_scheduler_hard_rules.py`：构造必出 bug 的 fixture（同火锅连排、闭馆时段被排），验证 Critic 拦下来

---

## 9. 实施顺序（D15 - 先排线后推荐）

### Phase A：排线落地（4–5 周）

1. **A1** `place_meta` 表 + migration + 现有 fixture 灌入种子
2. **A2** PlannerState 按天切片重构（向后兼容 `/api/optimize` 签名）
3. **A3** 鱼骨模板系统 + 模板选择器
4. **A4** Scheduler v2：营业时间 / dwell / 用餐窗 / 二级 tag 硬约束
5. **A5** 天气适配（生成时应用）
6. **A6** Critic 硬规则集 + 局部重跑
7. **A7** 过载备选池 + 前端备选抽屉
8. **A8** golden test + 硬规则单测

### Phase B：推荐升级（3–4 周）

1. **B1** PlaceRecommendation schema + 引用 chunk_id 强制
2. **B2** 高质量游记语料 pipeline（爬骨架 + DeepSeek 多 persona + 抽检）
3. **B3** 重新入库 RAG，跑 RAGAS 验证
4. **B4** 替代方案生成（每 place 1–2 个）
5. **B5** 主题模板入口
6. **B6** 前端地点卡背面 UI（理由 + 引用 + 替代）

### Phase C：协同与对话（2–3 周）

1. **C1** Onboarding must/nice/no-go 收集 UI
2. **C2** GroupPreferences 合并 + 「选三保二」chat 提示
3. **C3** EditorAgent + Patch 应用层
4. **C4** Rule Fast Path（删除 / 互换）
5. **C5** 前端 diff 高亮 + 变更说明面板

---

## 10. 风险与已知妥协

| 风险 | 缓解 |
|------|------|
| 爬取游记合规 | 仅本地开发使用，不出现在公开部署；做数据脱敏 |
| LLM 抽取 dwell/open_hours 噪声 | 置信度分层 + buffer + 人工抽检 + 多源交叉 |
| 局部重跑可能破坏全局一致性（酒店位置变了但其他天还按旧质心排） | EditorAgent 输出包含 `affects_global` 标志，必要时走全图重跑 |
| 鱼骨模板僵化 | 模板槽位允许「可选 slot」，Scheduler 可省略；后期可补 LLM 动态调整 |
| 群体偏好并集太杂 | must/nice/no-go 分层（D12）+ AI 明示选三保二 |
| MVP 不收用户反馈，迭代靠什么？ | 靠 RAGAS+agent_eval+人工 golden test，加 itinerary eval 专项（D23 内） |

---

## 11. 不在本 SPEC 内（拒绝项）

- 一键导出第三方地图 / PDF 手册
- 路上导航面板 / 现场动态「锐路径」
- 用户登录 / 多 trip 历史
- 隐式或显式用户反馈收集
- 路线代价中加入门票预算优化
- 推荐多模板 LLM 实时生成模板骨架（保留预设 5 套）

---

End of SPEC.
