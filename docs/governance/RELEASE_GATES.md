# 「行程查」Blueprint 1.0 Release Gates

> 状态：`ACCEPTED`
>
> Program：`TC-VNEXT-2026`
>
> 日期：2026-08-27

## 0. 证据状态分层

新版状态：

- `BLUEPRINT_READY`：权威文档、ADR、Program、Goal和风险/Provider合同一致；不含产品代码。
- `TEXT_CARDS_READY`：V0.1文本到可信卡片开发门禁通过。
- `MAP_STAY_READY`：V0.2地图与住宿门禁通过。
- `TOP3_AUDIT_READY`：V0.3核心核验门禁通过。
- `SCREENSHOT_PARITY_READY`：V0.4截图一致性门禁通过。
- `CITY_KNOWLEDGE_READY`：V0.5知识层准入通过。
- `MEMORY_SHARE_READY`：V0.6 consent与分享门禁通过。
- `VNEXT_CANDIDATE_READY`：G07同绑定Candidate Gate通过。
- `HUMAN_USABILITY_READY`：经人工批准的H1通过。

历史状态 `INTAKE_V2_DEVELOPMENT_READY` 和 `V1_CANDIDATE_READY` 保持只读，前者不得改写或替代后者，也都不能升级新版。历史 PASS、REJECT和NOT_RUN不得复制到新commit。

## 1. Blueprint Gate

G00必须同时满足：

- AGENTS、Charter、Spec、API、Architecture、Program、Roadmap、Release Gates和ADR无权威冲突；
- 当前Goal只有一个 `APPROVED/IN_PROGRESS`；
- G01～G07均有完整预定义合同、Dependencies、Authority、Baseline占位、Invariants、Budget、HITL、Checkpoint、Auto-advance、Completion和Stop conditions；
- 旧用户入口、强制确认、默认驾车、卡片原文和内部术语不再是目标要求；
- 新v3 API明确为 `NOT_IMPLEMENTED`，没有伪造当前能力；
- 风险登记和Provider准入表完整；
- 产品代码、migration、依赖锁文件diff为0；
- 历史Goal和证据保留；
- 文档链接、manifest引用和现有治理测试通过；
- 独立产品、架构、反方和商业审查的高优先级意见已处理；
- checkpoint已推送并远端回读。
- README、CLAUDE、docs索引、旧ADR和旧证据入口不会再自称新版当前权威；独立审查处理记录可远端回读。

通过只能标记 `BLUEPRINT_READY`，不代表V0.1代码存在。

## 2. 全版本零容忍项

以下任一非零即对应版本 `REJECT`：

- 错城或错类别POI自动进入行程；
- 描述句、URL、预约说明或模型举例成为地点卡片；
- 原文映射、置信度、长ID或内部流程泄漏到普通用户界面；
- LLM输出成为Provider事实、Finding或已解决状态；
- HARD冲突漏检；
- `UNKNOWN/UNAVAILABLE` 被展示为通过；
- 旧revision地图被标记为当前地图；
- 卡片编辑后隐藏触发路线Provider或自动重绘；
- 虚构酒店价格、房态、星级或服务质量；
- 原始截图终态未删除；
- 未授权数据被持久化、训练或分享；
- 修改sealed blind/oracle消除失败。

## 3. Text Card Gate — G01

数据：

- 90条family-isolated主集：54 dev / 18 validation / 18 sealed blind；
- 北京/上海/杭州60条、其他城市15条、对抗15条；
- 已删除的旧根`tests/`旅行文本不得作为regression、oracle或Gate证据；
- 双人独立标注与冲突裁决；
- family不跨split。

硬指标：

- schema、评分覆盖和内部原文证据有效率100%；
- 整句/URL/描述/预约成为地点0；
- 错城、错类别严重自动匹配0；
- 自动地点匹配precision = 正确canonical auto-selected实例数 / 全部auto-selected实例数，≥99%；validation与sealed blind分别要求分母≥50，分母不足即Gate无效而非PASS；
- 可执行地点提及precision ≥98%、recall ≥95%；
- day assignment F1 ≥97%；
- `PLANNED/OPTIONAL/REFERENCE/EXCLUDED/PASS_THROUGH` macro-F1 ≥94%；
- 三城自动匹配coverage ≥80%；
- 每份输入人工地点确认中位数≤1，P90≤3；
- 普通用户API/DOM中禁用字段命中0；
- 首次进度≤500ms，首批卡片P95≤8s；
- Qwen或AMap失败仍返回可编辑部分结果；
- 登录、体验、编辑、刷新、并发和幂等浏览器场景通过。
- `TripUnderstandingJob`在进程重启、lease接管和SSE重连后可恢复，重复事件副作用0；
- FULL越权访问0，DEMO资源越权0，24小时TTL、一次性claim、source/行程/账号删除回执100%可回读；
- 原始文本、PII、`public_resource_id`和匿名capability在日志、trace和分析事件中命中0；访问日志只记录路由模板或脱敏路径；
- 首批卡片READY后自动创建并实际执行一次同`PlanRevisionRef`的walking/transit地图job；相同逻辑任务即使请求key不同也只有一次Provider副作用；
- 地图job失败不影响卡片，迟到结果不更新current pointer；
- 冻结标准负载（3～12个已映射地点、并发1；仅用于性能测量而非产品范围）下，从首批卡片READY到每条可解析相邻边至少有一种路线的snapshot，snapshot矩阵P95≤15秒、受控live dev矩阵P95≤20秒；Provider不可用单列，不混入成功延迟；
- 地图正例集至少30份行程、120条已冻结为可成功的相邻边；fixture/snapshot中每条边至少一种模式成功率100%，受控live dev中≥95%，全局永远`UNAVAILABLE`或零可用边不能通过Gate；
- 输入/活动/并发/模型/POI/路线预算均有边界，超限返回可编辑`LIMITED`而非静默截断。

模型比较只在dev/validation使用固定provider/region/endpoint/exact model ID、相同prompt/schema/config/dataset和确定性scorer。挑战模型只有全部Validation硬门禁通过、质量相对最佳下降≤0.5个百分点且P95改善≥20%才可替换默认候选；唯一候选冻结后sealed blind只运行一次。

## 4. Map & Stay Gate — G02

地图：

- 复用G01已验证的首次后台地图快照，不把G02 UI当作首次路线计算；
- 编辑后自动路线Provider调用为0；
- stale投影100%绑定正确revision；
- 手动rerender只计算当前revision；
- 相同请求key和相同逻辑唯一键均不产生第二任务或Provider调用；
- 迟到任务覆盖新revision为0；
- walking/transit独立保存成功/失败；
- 差值≤10分钟时优先步行；
- 地图失败不影响卡片；
- fixture/snapshot重放hash一致率100%。
- 普通API只返回 `PREPARING/AVAILABLE/NEEDS_UPDATE/LIMITED/UNAVAILABLE`，内部job/freshness枚举泄漏0；
- 卡片详情在stale时不把旧“到下一站”路线表达为当前事实。
- 已有snapshot时地图首屏几何P95≤1.5秒；尚未完成时地图壳与用户状态≤500ms且不阻塞卡片。

住宿：

- 使用全部过夜日第一/最后站；
- N日默认只含Day1…DayN-1过夜日；方向固定为酒店→第一站、最后一站→酒店；
- 2/4/8km和同城扩展顺序正确；
- 候选错城、非酒店、非注册连锁品牌为0；
- 路线矩阵最多12家，公共候选最多3家；
- 评分公式与缺失证据惩罚确定性重放一致；
- 坐标系、候选充足阈值、失败惩罚、上限和tie-break均绑定 `StayScoringPolicyVersion`；
- 不展示价格、房态、星级或质量承诺；
- 选择同店创建新revision并使地图stale；
- 无候选为中性待选择，不阻断。
- 住宿正例集至少30组锚点（北京/上海/杭州各10），冻结snapshot中每组已知存在≥3家同城注册连锁酒店；Top-3非空率100%、首位属于合格集100%，受控live dev非空率≥90%。失败/空候选case另测，不能替代正例覆盖。

## 5. Top-3 Audit Gate — G03

- 地点、路线、营业/预约、日容量、酒店往返和用餐空档各有固定oracle；
- 路线Finding precision/recall均≥90%；
- HARD冲突漏检0；
- 具体地点候选100%来自冻结CandidateSet并绑定地点/路线receipt；
- 内部全部未解决HARD Finding保留；公共结果最多3个Finding，剩余队列显示数量且不得显示已通过，解决后按序补位；
- 排序使用severity、evidence、actionability和全程影响；
- 采纳创建新revision；
- 完整postcheck前不得显示已解决；
- Repair后新增BLOCKER/HIGH/UNKNOWN为0；
- 无绝对日期时具体天气/闭馆日期硬结论0。
- `calendar_basis=DAY_INDEX_ONLY`可materialize且不伪造日期/确认；ABSOLUTE与DAY_INDEX_ONLY lineage、ETag和map/stay current pointer回读100%一致。

## 6. Screenshot Parity Gate — G04

- PNG/JPEG/WebP、1～6张、单张≤10MB；
- 真实来源OCR关键字段F1≥95%；
- 低置信关键字段确认召回100%；
- 冻结paired set上阅读顺序adjacency-F1≥97%；
- 使用人工校正转写作为同源文本基线，截图端到端可执行地点precision/recall下降各≤1个百分点、严重错城/错类别/整句地点仍为0；
- 原图泄漏0，清理receipt 100%；
- 三张1080×1920图片在候选RunSpec冻结CPU/GPU/内存和并发1环境下P95≤12秒；
- Qwen-VL若晋级，关键字段、阅读顺序、卡片结果、bbox来源追踪和P95均不得低于PaddleOCR，且至少一项错误率相对下降≥20%；
- synthetic、自动视觉复核和真人OCR证据分别披露。

## 7. Knowledge Admission Gate — G05

每类`KnowledgeClaim`必须有：

- canonical place；
- claim type和适用条件；
- source tier、URL和短证据；
- observed/effective/expires；
- license/storage status；
- reviewer和版本。

要求：

- 硬事实只来自官方/政府/运营方或已准入Provider；
- 正规媒体和授权创作者只作建议；
- 未授权社交内容0；
- 过期claim不得进入当前建议；
- 同一validation set的有/无知识消融中，带来源且可执行的建议precision≥90%、unsupported claim=0、actionability rubric提升≥5个百分点，P95回退≤20%；
- RAG决定POI、路线或HARD Finding为0。

## 8. Consent & Share Gate — G06

- 记忆默认关闭；
- 只保存结构化偏好；
- 查看、更改、清空和删除全部可回读；
- 原文、截图、聊天默认长期留存0；
- 训练/评测consent与产品记忆consent分离；
- 分享token不可枚举、可撤销、过期并最小披露；
- 越权读取/修改0；
- 分享页内部字段泄漏0。

## 9. Candidate Evidence Gate — G07

| Gate | 必须实际证明 |
|---|---|
| G0 文档/schema | 权威文件、OpenAPI、migration只追加、风险和Provider准入 |
| G1 离线 | 语义、地点、地图、住宿、Audit、隐私、状态机和失败 |
| G2 PostgreSQL | migration、事务、CAS、幂等、lease、重启和旧数据兼容 |
| G3 固定快照 | Qwen/Provider snapshot、hash和确定性replay |
| G4 真实Provider | 高德POI/步行/公交、天气与许可范围的脱敏回执 |
| G5 浏览器/性能 | 登录、体验、卡片、地图stale/rerender、住宿、Top-3、刷新、断线和P95 |
| G6 Manifest | 同一commit/config/dataset/model/rule/provider的不可变汇总 |

G0～G6必须在候选commit重新运行。旧manifest、不同dirty tree或不同配置不能拼接。候选材料包括受控演示、90秒视频、5分钟完整演示、架构图、恢复时序图、模型消融和已知边界。

`VNEXT_CANDIDATE_READY`不等于H1、生产、公开发布或商业验证。

## 10. Reliability Gate

固定故障：

| 故障 | 预期 |
|---|---|
| Qwen timeout/schema invalid | 最多一次修复调用，之后确定性PARTIAL |
| POI部分失败 | 成功卡片保留，失败卡片待确认 |
| 路线部分失败 | 另一模式保留；两者失败为不可用 |
| 重复提交 | 返回同一资源并标记幂等重放 |
| 并发编辑 | 一个成功，失败方409并回读 |
| 进程终止 | 过期lease接管，不重复副作用 |
| 迟到地图任务 | 只写旧revision |
| config漂移 | 新任务或`CONFIG_MISMATCH`，不拼接 |
| Redis丢失 | 权威状态不改变 |
| screenshot cleanup失败 | `PRIVACY_BLOCKED` |

Trace与日志敏感字段扫描必须0命中。

## 11. Human Usability Gate — H1

H1需用户现场批准招募、consent和公网/环境范围。

- 8～12名目标用户；
- ≥80%无需开发者代操作完成输入、理解卡片、查看/更新地图、处理住宿和采纳建议；
- 关键问题理解率≥80%；
- 严重错误地点或虚构事实被当作可靠建议0；
- 内部术语被普通用户主动报告0；
- 隐私事故0；
- 严重误导和主链阻断全部进入regression并重跑相关Gates。

H1只能表述为小样本真人可用性证据，不等于统计显著、生产SLO或市场验证。

## 12. 禁止替代

历史Intake/Builder、旧Candidate、测试数量、synthetic proxy、自动Judge、source prior、旧RAGAS、单个演示或计划文档不得替代任何新版Gate。
