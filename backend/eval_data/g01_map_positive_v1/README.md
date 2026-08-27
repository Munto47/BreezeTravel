# G01 地图正例 fixture v1

本目录独立保存 Text Card Gate 的地图正例输入：北京、上海、杭州各 10 份行程，每份 5 个已映射地点和 4 条相邻边，共 30 份行程、120 条边。

所有路线数字都是 `CONTROLLED_FIXTURE / NON_LIVE_SYNTHETIC_ROUTE_FACTS`，用于确定性验证 worker、walking/transit 双模式、10 分钟步行优先策略、覆盖率和性能边界。它们不是高德回执、当前路线事实、真人证据或生产证据，不得进入用户结果或用于路线事实展示。

`dataset_contract.json` 绑定 fixture 和生成器字节。当前真实高德持久化仍为 `BLOCKED_PENDING_WRITTEN_PERMISSION`，所以本资产只能满足 fixture 子门禁，不能单独使 Text Card Gate 通过。
