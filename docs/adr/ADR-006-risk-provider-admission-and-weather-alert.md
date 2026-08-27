# ADR-006：风险 Provider 准入与天气预警主链

## Status

Partially superseded by ADR-011 / TC-VNEXT Provider Admission，2026-08-23。

> 当前保留Provider必须证明存储权/成本/来源/回执、搜索候选不得直接成为权威事实、天气无预警不等于无旅行风险。旧P3、三城18次live matrix和既有凭据足够的结论只属历史，不授权TC-VNEXT调用或持久化。

## Context

P3 原实现把 Brave News Search 作为每城一次的必需 live 风险查询，并把返回的标题、URL、日期和摘要持久化到 `EvidenceSnapshot`。Brave 当前标准 Search API 订阅需要邮箱验证与信用卡；标准条款还禁止在运行所需的瞬时存储之外保存 Search Results，结果留存需要另行取得明确的 storage rights。

因此，获得普通 API key 既不能自动完成，也不能使当前持久化证据链合规。搜索结果本身还是候选来源，不是景区关闭、拥挤或交通管制的权威事实。

## Decision

- P3 每城第六次 live 调用改为现有和风天气项目下的实时天气预警：`GET /weatheralert/v1/current/{latitude}/{longitude}`；连同四种高德路线和一次天气预报，仍保持三城各 6 次、总计 18 次。
- 天气预警保存发布机构、时间、严重程度、结构化内容和 Provider 归因。`zeroResult=true` 只记录 `NONE_REPORTED + ACTIVE_WEATHER_ALERTS_ONLY`，不得得出“无旅行风险”。
- Brave 从 P3 必需 Provider 中移除。普通 Search key 即使存在也不得激活持久化 EvidenceSnapshot 路径。
- 非天气风险由通过准入的 `RiskDiscoveryAdapter` 发现候选来源，最终 Evidence 必须来自原始官方、政府、交通或运营方页面。Provider 准入必须证明数据留存权、成本上限、来源边界和可重放 receipt。

## Consequences

- P3 不再要求用户注册 Brave、接受条款或绑定信用卡；现有高德和和风凭据足以运行固定 18 次 live matrix。
- G4 证明的是路线、天气预报和天气预警 Provider 完整性，不证明三城所有旅行风险均被发现。
- 若未来重新评估 Brave，必须先取得明确的结果存储权和消费硬限制，并只把它作为瞬时候选来源发现层；不得直接持久化搜索摘要作为权威事实。

## References

- [Brave Search API Terms of Use](https://api-dashboard.search.brave.com/documentation/resources/terms-of-service)
- [Brave Search API plans and storage note](https://brave.com/search/api/)
- [QWeather Weather Alert](https://dev.qweather.com/docs/api/warning/weather-alert/)
- [QWeather pricing](https://dev.qweather.com/en/docs/finance/pricing/)
