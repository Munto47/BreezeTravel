# 五城改造效果记录（持续更新）

## 本次检查点的范围
这是开发中可回退的整体检查点，不是交付验收。保留原产品卡片、地图、撤销、权限、协作与worker，追加城市资料、局部恢复、每日餐饮和逐晚住宿。真实失败继续留在报告中。

## 分开记账
|验证|当前证据|解释|
|---|---|---|
|固定代码回归|backend清单612通过；PostgreSQL清单114通过|存在重叠，不能合并成准确率|
|受控页面回归|确认卡片、图片、补全、每日餐饮和跨城住宿23通过|包含真实浏览器和受控API，不算真实推荐质量|
|真实平台输入|五城200+普通/跨城30份，231次实际生成调用|有一份截断重采，原attempt保留；费用未知|
|独立语义标注|首批50逐份核对中|地点含义与主线/备选/交通/内部关系分层，原文家族不跨数据组|
|真实语义比较|相同首五份开发输入v2联合召回63.72%、精确80%；v3召回78.76%、精确81.65%|旧标签快照113条，角色口径正在独立修订；不能视为最终主站验收|
|真实餐宿短例|30主站23确认；五城都返回住宿、15日返回餐饮|发现实质问题，因此不计为五城质量通过|
|真人体验|NOT_RUN|不由自动测试代替|

## 必须继续修复的失败
1. 主线角色：有条件/区域介绍被收为主站，明确园内到访反被当介绍；先区分模型原始草稿与后处理责任，再修相应环节。
2. 长文完整性：逐日模型提取提高召回但调用从7增到25（相同五份）；继续报告全部请求与token，不只报召回。
3. 住宿边界：不能先过滤未匹配站再取首末站；真实顺序末站缺失时必须显式缺失，不能拿前一站替代并声称通勤完整。
4. 午餐质量：餐饮类别过宽，真实结果混入咖啡/奶茶/茶馆/公司/食物银行；必须结合真实类型筛选。5日缺商圈信息，不能虚构。
5. 身份资料：仅名称/地址独立核对到具体门店才有集团背书；五城短例只有杭州命中官网门店。资料需继续扩充，未知价格/营业/房态不填假值。

## 重跑与证据位置
- 本地和CI共用product_test_manifest.json：后端目录运行python -m scripts.run_product_checks backend/postgres；浏览器组为browser-core、browser-refinement、browser-confirmed。
- 私有隔离数据库：根目录python scripts/run_private_checks.py --suite postgres。环境和秘密不会打印。
- 真实输入：.local-artifacts/corpus/five-city-20260907/manifest.json、responses、各attempt。开发/验证/留出按原文家族预分，当前只对开发输入迭代。
- 语义报告：同目录pilot-semantic-five-v2.json、pilot-semantic-five-v3.json及对应固定标签；若指纹变化中断，标明中断，不与完整同版结果混合。
- 餐宿报告：.local-artifacts/evaluation/five-city-recommendations.json，含全部候选/锚点/路段和实际HTTP计数。只读复核，不公开账号原文或私有配置。
- 来源：backend/app/trip_understanding/city_knowledge_data/sources.json；酒店具体门店来源在hotel_brand_registry_v1.json。高德餐饮商圈字段参见[POI 2.0文档](https://lbs.amap.com/api/webservice/guide/api/newpoisearch)。

正式验收必须依照FIVE_CITY_EVALUATION.md报告分城分子分母、所有失败、费用未知项和三次重复；不从以上短例外推普遍零错误。
