# BreezeTravel P1 90 秒演示脚本

证据绑定 commit：`dd70870a817b84f6364804a5701950c754728f4e`。演示只使用受控 fixture，不表示 live Provider、公网或真人证据。

- **0–15 秒：文本与边界。** 在“行程查”的既有 Import 页面粘贴北京、上海或杭州 2 天文本。指出范围固定为单城市、2–5 人、2–5 天。
- **15–30 秒：Brief 与地点确认。** 展示 TripBrief 的人数、日期、偏好来源；未确认 Brief 不能运行。切到 BJ-02，展示歧义“博物馆”不会自动绑定，也不能创建权威 revision。
- **30–50 秒：Evidence → Audit → Advice。** 在三城任一 `01` 案例确认 Brief 并启动 Run，展示持久阶段事件、受控路线 receipt、`ROUTE_GAP_INSUFFICIENT` Finding，以及绑定 Evidence 与不确定性的 Advice。
- **50–70 秒：Repair 与新 Revision。** 对比“顺延后一站”和“缩短前一站”，采纳一个已有 EditCommand 方案；展示旧报告 stale、revision 1 → 2，以及新 revision 的完整 postcheck。
- **70–82 秒：恢复与去重。** 刷新页面，展示 revision 2、`SUCCEEDED · POSTCHECK` 和 SSE 从 `Last-Event-ID` 继续。打开故障矩阵，指出 Evidence 后终止恢复前后 snapshot/receipt/revision 数量不变。
- **82–90 秒：证据边界。** 展示 D1 manifest：18/18 pilot、三城浏览器、PostgreSQL 和自动回归分别记录；明确 fixture、浏览器、PostgreSQL、live Provider、公网和真人证据不能互相替代。
