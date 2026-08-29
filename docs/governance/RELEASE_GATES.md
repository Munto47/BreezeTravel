# 「行程查」Blueprint 1.3 Release Gates

> 状态：`ACCEPTED`
>
> Program：`TC-VNEXT-2026`
>
> 日期：2026-08-28

## 0. 证据状态分层

证据等级：

- `AUTOMATED_TEST`；
- `LIVE_PROVIDER_EVIDENCE`；
- `MULTI_AGENT_SIMULATED_REVIEW`；
- `SEALED_AGENT_BLIND`；
- `HUMAN_USABILITY`；
- `PRODUCTION_EVIDENCE`。

G01～G06按`product_delivery_gates.json`取得`PRODUCT_DELIVERY_PASS`；G07才按`AGENT_GATE_PROTOCOL.md`取得`HARDENED_CANDIDATE_GATE_PASS`。多Agent模拟审查和sealed agent blind均不是人类证据；H1、生产和商业未实际运行时保持`NOT_RUN`。

G01～G06使用`PRODUCT_DELIVERY_GATE`，只检查当前用户旅程和针对性验证；G07使用`HARDENED_CANDIDATE_GATE`。90条完整统计、50次真实性能链、三角色复审、ultra裁决、sealed blind、exact commit全证据绑定、完整可靠性和供应链加固均不得前置。

现有Agent Gate、blind、authority、custody、签名、broker和候选回执实现统一保留为`FROZEN_G07_ASSET`；G07前不得继续修改，也不得作为产品Goal依赖。

阶段固定为G01～G03 `CORE_MVP`、G04～G06 `PRODUCT_ENHANCEMENT`、G07 `CANDIDATE_HARDENING`。G03通过后进入`CORE_MVP_OWNER_REVIEW_PENDING`并停止；G07通过后停止。

### 0.1 并行开发完整性

每个Goal晋级前，v3 `current_work_packages.json`必须证明：主对话是唯一非终态集成者；每个长期功能绑定独立用户可见对话、branch、remote、worktree和完整prompt hash；集成者加最多两个`IN_PROGRESS/BLOCKED_EXTERNAL`贡献包；等待的第三包为`WAITING_FOR_WRITER_SLOT`；同一product baseline；branch/worktree/路径所有权不重复；普通包未触碰受保护文件；官方`READY_TO_MERGE`的tip、clean worktree和remote readback仍等于`ready_commit`；所有包串行整合后才运行当前交付门。

新版状态：

- `BLUEPRINT_READY`：权威文档、ADR、Program、Goal和风险/Provider合同一致；不含产品代码。
- `TEXT_CARDS_READY`：V0.1文本到可信卡片开发门禁通过。
- `MAP_STAY_READY`：V0.2地图与住宿门禁通过。
- `TOP3_AUDIT_READY`：V0.3核心核验门禁通过。
- `SCREENSHOT_PARITY_READY`：V0.4截图一致性门禁通过。
- `CITY_KNOWLEDGE_READY`：V0.5知识层准入通过。
- `MEMORY_SHARE_READY`：V0.6 consent与分享门禁通过。
- `VNEXT_CANDIDATE_READY_AGENT_VERIFIED`：G07同绑定Candidate Gate与Agent Gate通过；不含真人、生产或商业证据。
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

以下任一非零即所有版本`REJECT`：

- 错城、错类别、描述句或URL被自动当成地点；
- 越权访问，或原文、内部字段、capability泄漏；
- 用户要求删除的数据无法确认删除；
- 卡片编辑后隐藏触发路线Provider或自动重绘；
- `UNKNOWN/UNAVAILABLE`被展示或统计为成功。

## 3. Text Card Gate — G01

固定输入只使用`G01-TC-001 / 013 / 025 / 037 / 046`。G01通过必须同时证明：

- 匿名体验和登录长文本都产生用户友好的逐日卡片；
- 查看、插入、替换、移动、删除、刷新和幂等重放可用；
- 首批卡片后实际启动同`PlanRevisionRef`的walking/transit后台地图任务；
- 编辑后路线Provider调用为0，旧地图只显示`NEEDS_UPDATE`；
- Qwen、地点或路线Provider故障返回可编辑卡片、地点待确认或`LIMITED/UNAVAILABLE`，不显示红色成功假象；
- 公共JSON/DOM禁用字段命中0，匿名和登录越权请求被拒绝；
- source、整程和账号旅行数据删除均有终态readback；
- v3定向测试、PostgreSQL集成、frontend build和G01浏览器E2E通过。

90条统计、最小分母、50次链路、三角色复审、ultra裁决、sealed blind、完整性能与exact候选绑定均为G07 `NOT_RUN`，不得阻断G01。

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
- fixture/snapshot定向重放一致；
- 普通API只返回 `PREPARING/AVAILABLE/NEEDS_UPDATE/LIMITED/UNAVAILABLE`，内部job/freshness枚举泄漏0；
- 卡片详情在stale时不把旧“到下一站”路线表达为当前事实。
- 尚未完成时地图壳和用户状态不阻塞卡片。

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
- 定向正例显示最多3家合格同城注册连锁酒店；失败或无候选中性显示“住宿待选择”。

## 5. Top-3 Audit Gate — G03

- 地点、路线、营业/预约、日容量、酒店往返和用餐空档各有固定oracle；
- HARD冲突漏检0；
- 具体地点候选100%来自冻结CandidateSet并绑定地点/路线receipt；
- 内部全部未解决HARD Finding保留；公共结果最多3个Finding，剩余队列显示数量且不得显示已通过，解决后按序补位；
- 排序使用severity、evidence、actionability和全程影响；
- 采纳创建新revision；
- 完整postcheck前不得显示已解决；
- Repair后新增BLOCKER/HIGH/UNKNOWN为0；
- 无绝对日期时具体天气/闭馆日期硬结论0。
- `calendar_basis=DAY_INDEX_ONLY`可materialize且不伪造日期/确认；ABSOLUTE与DAY_INDEX_ONLY lineage、ETag和map/stay current pointer回读100%一致。
- G03通过后交付演示脚本、已知边界和验证结果，状态切换为`CORE_MVP_OWNER_REVIEW_PENDING`，不自动激活G04。

## 6. Screenshot Parity Gate — G04

- PNG/JPEG/WebP、1～6张、单张≤10MB；
- multipart批次返回owner-bound不透明引用；JSON Base64输入0，跨账号/过期/重复终态消费0；
- 真实来源OCR关键字段F1≥95%；
- 低置信关键字段确认召回100%；
- 冻结paired set上阅读顺序adjacency-F1≥97%；
- 使用两个隔离Agent转写和新的ultra裁决形成同源文本基线，截图端到端可执行地点precision/recall下降各≤1个百分点、严重错城/错类别/整句地点仍为0；Agent结果不得称为人工校正；
- 原图泄漏0，成功/失败/取消/超时/TTL终态清理receipt 100%；OCR文本/bbox删除遵循SourceDocument合同；
- 三张1080×1920图片在候选RunSpec冻结CPU/GPU/内存和并发1环境下P95≤12秒；
- Qwen-VL若晋级，关键字段、阅读顺序、卡片结果、bbox来源追踪和P95均不得低于PaddleOCR，且至少一项错误率相对下降≥20%；
- synthetic、自动视觉复核、`MULTI_AGENT_SIMULATED_REVIEW`分别披露；真人OCR只在另行批准并实际运行后记为`HUMAN_USABILITY`，不阻断G04。

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
- 分享token不可枚举、可撤销、过期并最小披露；链接fragment只能用于一次body交换，换得HttpOnly capability后立即清除；
- 分享秘密进入服务端可见URL、访问日志、Referer或分析事件为0；
- 删除行程/账号后分享仍可访问为0；
- 反馈隐式开启训练/评测授权为0；清空全部旅行数据后偏好、反馈或分享残留为0；
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
| G7 Agent Gate | 三角色隔离审查、ultra裁决、所需sealed blind与fresh readback |
| G8 候选加固 | 生成`HardeningDecision`。`NOT_REQUIRED_WITH_RATIONALE`必须绑定威胁、替代控制和残余风险；`REQUIRED`只验证被点名的外部authority、不可变远端ref、角色签名或OCI控制。不得默认恢复整套旧链，也不得外推为H1或生产证明 |

G0～G6必须在候选commit重新运行。旧manifest、不同dirty tree或不同配置不能拼接。候选材料包括受控演示、90秒视频、5分钟完整演示、架构图、恢复时序图、模型消融和已知边界。

`VNEXT_CANDIDATE_READY_AGENT_VERIFIED`不等于H1、生产、公开发布或商业验证。G07通过后停止自动推进。

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

历史Intake/Builder、旧Candidate、测试数量、synthetic proxy、未按Agent Gate Protocol隔离和绑定的自动Judge、source prior、旧RAGAS、单个演示或计划文档不得替代任何新版Gate。按协议形成的Agent证据仍不能替代H1、生产或商业证据。
