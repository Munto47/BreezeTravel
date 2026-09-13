# 后端约定
继承根AGENTS和产品定义。真实入口app/experience_main.py；worker使用ExperienceQwenProvider，旧实验不代表运行时。
- 语义、身份、路线、建议分层；资料命中不能升级原文角色。
- 保留引用和正确片段，有限局部恢复，保护更正、分支和再访。
- 城市核验共用city_scope；成功结果按许可期限缓存，服务失败不能缓存为无地点。
- 推荐从权威活动构建每日/逐晚上下文：D末站→酒店→D+1首站。
- API追加兼容字段，旧快照给默认值，migration仅追加，公共投影不泄漏内部记录。
- 定向单测、真实PostgreSQL、API分别核对；以实际HTTP次数记成本。当前共同入口scripts.run_product_checks。
