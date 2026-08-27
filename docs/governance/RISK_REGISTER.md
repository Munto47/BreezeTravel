# TC-VNEXT 风险登记

> 状态：`ACTIVE`
>
> Program：`TC-VNEXT-2026`
>
> 日期：2026-08-27

| ID | 风险 | 用户影响 | 预防/缓解 | 监测与Gate | 当前状态 |
|---|---|---|---|---|---|
| R-01 | 描述句、URL或错城结果成为地点 | 行程卡片失真，用户立即流失 | 原子ActivityMention、只搜索PLANNED、城市/类别fail-closed | Text Card零容忍 + sealed blind | OPEN，G01 |
| R-02 | 公共API或页面泄漏原文映射和内部术语 | 体验差、隐私与安全风险 | UserFacing投影allowlist、序列化/DOM禁止字段扫描 | G01用户投影Gate | OPEN，G01 |
| R-03 | Qwen schema/model漂移或blind被用于选模 | 抽取不稳定、blind失效 | exact model binding、服务端编译；只在dev/validation选唯一候选后blind一次 | model receipt + custodian + one-shot blind | OPEN，G01 |
| R-04 | 卡片编辑后地图被旧任务覆盖 | 用户看到错误路线 | PlanRevisionRef、freshness、迟到写保护、逻辑唯一任务 | Map迟到/并发故障矩阵 | OPEN，G01/G02 |
| R-05 | 隐藏实时路线重算导致延迟/成本 | 编辑卡顿、Provider滥用 | 编辑只标stale，只有手动按钮触发 | 编辑后Provider调用必须为0 | OPEN，G02 |
| R-06 | 酒店建议导致频繁换店或虚构信息 | 行程更折腾、消费误导 | 整程同店、区域路线评分、不显示价格/房态/星级 | Stay零容忍与路线矩阵 | OPEN，G02 |
| R-07 | 高德数据缓存/展示超出许可 | 合规和商业化风险 | 最小持久化、geometry短期缓存、生产前书面澄清 | Provider Admission Gate | OPEN，阻断生产 |
| R-08 | 知识/RAG来源过期或未授权 | 建议误导、版权/条款风险 | KnowledgeClaim来源/时效/许可，禁抓小红书 | Knowledge Admission Gate | OPEN，G05 |
| R-09 | 用户记忆过度留存 | 隐私与信任损失 | 默认关闭、结构化、可删、训练consent分离 | Consent/Delete Gate | OPEN，G06 |
| R-10 | 新蓝图被历史Candidate证据错误晋级 | 对外声明超过真实能力 | 历史状态只读，新commit重跑全部Gate | Manifest绑定与完成措辞检查 | ACTIVE |
| R-11 | G00蓝图膨胀为代码重构 | 未规划先开发、范围失控 | G00产品代码/migration/依赖diff为0 | Blueprint Gate | ACTIVE |
| R-12 | 其他城市被宣传为三城同等级 | 全国能力误导 | BASIC_ONLY投影和独立Provider证据 | 三城/其他城市case Gate | OPEN，G01 |
| R-13 | 无日期仍给具体天气/闭馆结论 | 虚构时效事实 | date=null时禁用日期相关HARD | Audit零容忍 | OPEN，G03 |
| R-14 | Top-3隐藏真正硬冲突 | 安全问题漏报 | 内部全量队列、公共前三项、剩余数量和解决后补位 | HARD漏检0 + 未解决队列 | OPEN，G03 |
| R-15 | 输入放大或Provider费用无界增长 | 延迟、成本和拒绝服务 | 文本/活动/并发/模型/POI/路线任务预算、账本、LIMITED降级 | 每Goal预算与超限矩阵 | ACTIVE |
| R-16 | understanding与itinerary revision错接 | 地图/住宿/编辑绑定错误版本 | PlanRevisionRef、materialization lineage、ETag跨kind拒绝 | G02/G03 lineage与并发矩阵 | OPEN，G02/G03 |
| R-17 | 无日期计划被伪造日期才能核验 | 天气/营业结论失真 | DAY_INDEX_ONLY桥接、nullable calendar、日期规则UNKNOWN | G03 migration与Audit矩阵 | OPEN，G03 |
| R-18 | 匿名资源越权、遗留或claim后双重所有权 | 隐私泄漏 | HttpOnly session、资源授权、24h TTL、一次性原子claim、删除回执 | G01 auth/privacy Gate | OPEN，G01 |
| R-19 | 理解job或地图job只在内存、重启后丢失 | 用户等待后无结果或重复调用 | PostgreSQL lease/event、逻辑唯一键、接管与事件重放 | G01 process termination矩阵 | OPEN，G01 |
| R-20 | Goal过渡把PENDING归档或同时激活两个Goal | 长任务失去唯一指挥文件 | subject A + 原子transition B协议、远端readback | Blueprint/每Goal结构检查 | ACTIVE |
| R-21 | README、旧ADR或历史证据继续自称当前权威 | 后续Agent回到旧产品 | superseded banner、权威索引、漂移扫描 | Blueprint文档检查 | ACTIVE |

风险状态只能通过对应Goal的实际证据关闭。文档、计划或单次测试不能把OPEN改为CLOSED。
