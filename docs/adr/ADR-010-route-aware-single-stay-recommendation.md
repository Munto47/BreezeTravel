# ADR-010：路线感知的整程同店住宿推荐

- 状态：Accepted
- 日期：2026-08-27
- Program：`TC-VNEXT-2026`

## 背景

用户输入很可能没有酒店。把“每天末尾缺少酒店”判成错误会制造红色噪声；按每天最后一站推荐不同酒店又可能导致频繁搬运行李。只看某一天或几何距离也不能代表整个行程的往返成本。

## 决策

缺少酒店时生成非阻断的“住宿待选择”，不自动插入行程。

`StayAreaPlanner` 使用冻结的 `StayScoringPolicyVersion`。N日计划默认过夜日为Day 1…Day N-1；只有原文明确最后一日继续住宿时才包含Day N。每个过夜日计算 `STAY_TO_FIRST` 与 `LAST_TO_STAY` 两条有方向通勤边：

1. 将GCJ-02锚点局部等距投影后计算几何中位区域；
2. 2 km搜索；
3. 合格候选少于12家则扩到4 km；
4. 仍少于12家则扩到8 km；
5. 仍少于12家时同城搜索；达到12家即停止扩圈。

候选必须匹配版本化 `HotelBrandRegistry`、酒店类别和城市。最多12家进入walking/transit路线矩阵，评分：

```text
total_best_minutes
+ 0.5 * max_single_leg_minutes
+ 8 * total_transfers
+ evidence_penalty
```

公共结果最多显示3家。用户选择后，同一家酒店成为所有过夜日的共享 `StayAnchor` 并产生新revision，地图变STALE。

策略版本必须固定坐标缺失、单向失败、双模式失败的 `evidence_penalty` 数值和上限。未能得到任何方向路线的候选不能因低几何距离排名第一；同分依次按缺失边更少、最差单程更短、canonical place ID排序。公共卡片解释区域、通勤摘要、最差单程、换乘、证据缺口与简短理由，不展示内部总分。

materialize前选择住宿创建understanding revision，materialize后创建itinerary revision。下一次地图任务同时加入酒店→第一站和最后一站→酒店的隐式边；最后一天默认不追加酒店。内部freshness为STALE时公共状态为 `NEEDS_UPDATE`。

第一版不展示或推断价格、房态、星级和服务质量。后续档次/预算必须另立Goal和来源合同。

## 后果

- 用户得到“整个行程少折腾”的住宿选择。
- 无酒店不再是错误。
- 需要Route Matrix、brand registry和冻结StayRecommendationSnapshot。
- 选择住宿会增加每晚酒店卡片，但它们共享同一住宿实体。

## 不采用

- 每天就近换店：增加行李成本。
- 只看第一晚：可能让后续往返极差。
- 自动插入第一候选：把推荐伪装成用户计划。
- 用评分/价格作质量承诺：Provider字段不足以证明。

## 验证

错城、非酒店、非连锁为0；扩圈顺序和评分可重放；无候选中性返回；同店物化和地图stale完整回读。
