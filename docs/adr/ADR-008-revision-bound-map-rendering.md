# ADR-008：revision 绑定的后台地图与手动重绘

- 状态：Accepted
- 日期：2026-08-27
- Program：`TC-VNEXT-2026`

## 背景

现有workspace地图仅把已存坐标画成虚线，明确不是真实路线；旧房间地图又在浏览器中默认计算驾车路线。若卡片每次编辑都实时重算，会增加等待、Provider调用和竞态，并可能让旧任务覆盖新行程。

## 决策

拆分三个概念：

```text
MapRenderJob（可变）: QUEUED → BUILDING → READY / PARTIAL / UNAVAILABLE
MapRenderSnapshot（不可变）: terminal route facts + geometry refs
MapFreshness（计算值）: CURRENT | STALE
```

G01交付地图表、最小worker和walking/transit计算：卡片首次READY后自动为该`PlanRevisionRef`排队并真正计算一次。G02交付地图剧场、模式切换、住宿、旧图提示和手动重绘。后续卡片编辑只创建新revision并把旧地图投影为公共 `NEEDS_UPDATE`，不调用路线Provider。用户点击“重新渲染地图”时才为current `PlanRevisionRef`创建或复用任务。

任务使用PostgreSQL状态、lease、幂等键和迟到写保护，不新增消息队列。Redis只缓存短期路线geometry。

相邻地点同时计算walking/transit。选择更短方式；差值不超过10分钟时优先walking。驾车不作默认。每种方式独立保留成功、失败和观测时间。

## 一致性规则

- job和snapshot必须绑定完整 `PlanRevisionRef(kind, aggregate_id, revision, stop_set_hash)`、config hash和canonical coordinates。
- 迟到任务只能完成旧revision，不能更新current pointer。
- 请求幂等键防重放；逻辑唯一键 `(understanding_id, revision_kind, revision, stop_set_hash, route_config_hash)` 防止不同key重复Provider副作用。
- 打开stale地图可显示旧结果和提示，但不得标记为当前。
- 地图失败不影响卡片。
- 只有初次READY允许自动创建；编辑后自动路线调用必须为0。

## 后果

- 用户通常打开地图时已有结果。
- 编辑保持流畅且调用成本可控。
- 用户明确控制何时更新地图。
- 前端只处理用户状态 `PREPARING/AVAILABLE/NEEDS_UPDATE/LIMITED/UNAVAILABLE`，不能看到内部job状态或假设地图与卡片永远同步。
- Provider许可未解决前，geometry只能短期缓存。

## 不采用

- 每次编辑实时重绘：延迟、费用和竞态不可控。
- 只在浏览器临时算路线：无法恢复、回放和绑定revision。
- 延续几何虚线：不能代表真实可行路线。
- 默认驾车：不符合城市游览的主要步行/公交体验。

## 验证

使用编辑零调用、迟到任务、并发、幂等、重启、config漂移、walking/transit局部失败和stale浏览器矩阵。
