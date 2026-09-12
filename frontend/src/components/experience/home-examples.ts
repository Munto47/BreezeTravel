// These are editable text examples. Choosing one never starts a request or a replay.
export const HOME_EXAMPLES = [
  {id: 'beijing', label: '北京 3 天示例', text: `北京三日游，按下面的先后顺序安排。
第1天：先去故宫博物院，再去景山公园，最后到什刹海散步。
第2天：先逛天坛公园，再去前门大街，最后到大栅栏。
第3天：先去颐和园，再去圆明园。
如果体力允许，南锣鼓巷作为第1天的备选，不加入主线。`},
  {id: 'shanghai', label: '上海 Citywalk', text: `上海两天城市漫步，地点按列出的顺序游览。
第1天：武康大楼、安福路、静安寺。
第2天：先到豫园，再逛豫园商城，最后沿外滩散步。
如果有余力，南京路步行街作为第2天备选。`},
  {id: 'family', label: '亲子游', text: `杭州两天亲子游，想带孩子轻松逛逛。
第1天：先去浙江自然博物院杭州馆，再去西湖断桥。
第2天：先去杭州动物园，再去太子湾公园。
每天中途需要用餐，餐厅还没有选，希望有适合孩子的清淡菜。`},
  {id: 'food', label: '美食之旅', text: `广州两天美食之旅，按照这个顺序走。
第1天：先去陈家祠，再到永庆坊，最后去沙面。中途想找一家普通对外营业的餐厅吃粤菜，门店还没选。
第2天：先到越秀公园，再去北京路步行街。想尝肠粉和双皮奶，具体店铺待选。
广州塔仅作第2天备选，不加入主线。`},
] as const

export type HomeExample = typeof HOME_EXAMPLES[number]
