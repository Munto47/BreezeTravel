# 产品与接口入口

当前用户行为以 [产品定义](PROJECT_CHARTER.md) 为准，接口实施顺序及验收以 [实施计划](../governance/IMPLEMENTATION_PLAN.md) 为准，完成情况见 [当前状态](../governance/CURRENT_GOAL.md)。

继续扩展 `/api/v3/trip-understandings`，服务端 Pydantic 模型和共享客户端类型须随同一切片更新。计划中的字段在实现和验证前不得声明已可用。

[旧合同原件](../governance/archive/pre-convergence-20260905/docs/product/TRIP_CHECK_API_CONTRACT.md)仅供兼容和考古。

## 本轮照片兼容字段

ActivityCardView 追加可空的 photo_url，不改变命令、ETag 或旧字段语义。照片来自同一次地点搜索中通过原有城市、类别和名称核验的最终 POI；只允许无凭据、无查询参数的高德图片 CDN HTTPS 地址。待确认、替换地点和修改城市会清除照片；照片失败退回装饰插画，不触发浏览器地点搜索或路线请求。没有照片不会阻塞行程。

搜索参数使用高德 [POI 2.0 官方字段](https://lbs.amap.com/api/webservice/guide/api-advanced/newpoisearch) business,photos；不新增 Provider 调用或照片服务。
