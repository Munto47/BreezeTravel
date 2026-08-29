# TC-VNEXT 风险登记

> 状态：`ACTIVE`
>
> Program：`TC-VNEXT-2026`
>
> 日期：2026-08-29

| ID | 风险 | 用户影响 | 预防/缓解 | 监测与Gate | 当前状态 |
|---|---|---|---|---|---|
| R-01 | 描述句、URL或错城结果成为地点 | 行程卡片失真，用户立即流失 | 原子ActivityMention、只搜索PLANNED、城市/类别fail-closed | Text Card零容忍 + sealed blind | OPEN，G01 |
| R-02 | 公共API或页面泄漏原文映射和内部术语 | 体验差、隐私与安全风险 | UserFacing投影allowlist、序列化/DOM禁止字段扫描 | G01用户投影Gate | OPEN，G01 |
| R-03 | Qwen schema/model漂移或blind被用于选模 | 抽取不稳定、blind失效 | exact model binding、服务端编译；只在dev/validation选唯一候选后blind一次 | model receipt + independent Codex blind task + one-shot blind | OPEN，G01 |
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
| R-22 | 同一模型家族的Agent标注和审查产生相关偏差 | Gate高估质量 | A/B隔离上下文、三角色分工、ultra新任务、确定性scorer和live Provider事实绑定 | Agent任务attestation、角色覆盖和error-family回归 | OPEN，G01～G07 |
| R-23 | 开发上下文、候选输出或blind truth泄漏给评测任务 | 独立性失效、指标污染 | `fork_turns=none`、仓库外原始输出、输入包allowlist、hash绑定和one-shot tranche | Agent Gate validator fail closed | OPEN，G01～G07 |
| R-24 | Agent模拟审查被对外表述为真人证据 | 用户和面试方被误导 | 固定证据枚举、`human_evidence=false`、G07状态显式agent verified | manifest和完成措辞扫描 | ACTIVE |
| R-25 | live Provider在事务外完成后进程崩溃导致重复调用 | 重复费用、回执与事实不一致 | 调用前持久化稳定effect key/STARTED，恢复时UNKNOWN_OUTCOME而非盲目重发 | Qwen/AMap crash-window矩阵 | OPEN，G01 |
| R-26 | 开发任务自行创建key、registry或通用PASS回执 | Gate成为自我证明 | G01～G06用候选绑定、原始证据hash、确定性scorer和clean checkout降低风险；外部signer只在G07威胁模型批准后启用 | CORE receipt回读；G07 HARDENED审查 | DEFERRED_CANDIDATE_HARDENING，不阻断G01～G06 |
| R-27 | sealed指标由调用者填写，或外部文件在hash后被替换 | blind未真正评分或回执绑定错误字节 | 原始输入/预测/truth确定性scorer、HMAC truth commitment、单句柄snapshot、hardlink拒绝、独占输出与DB-first nonce | sealed mutation/replay/TOCTOU矩阵 | OPEN，G01 |
| R-28 | 候选自带弱化verifier或自行建立新签名根 | 恶意/错误候选可给自己签发PASS | CORE profile以冻结合同、确定性重算和clean checkout缓解；外部authority仅在G07评估 | CORE contract hash；G07 threat-model review | DEFERRED_CANDIDATE_HARDENING，不阻断G01～G06 |
| R-29 | 组件签名者手填PASS摘要而未运行底层事实检查 | Gate显示通过但底层事实未成立 | CORE聚合器只接受可回读的测试、Provider、panel和sealed原始回执并重算指标；角色签名留到G07 | raw-receipt与scorer重算；G07签名反例 | OPEN，CORE需验证；签名链DEFERRED |
| R-30 | 本机管理员或custodian失陷后回滚外部状态或窃取运营密钥 | nonce重放或伪造新回执 | 当前工程Gate不宣称抵抗host admin；G07/生产按威胁模型决定远端透明日志/HSM | G07 threat-model review | ACCEPTED RESIDUAL，G07/生产评估 |
| R-31 | Goal切换或候选调用者选择弱化的自动Gate合同 | 应运行的检查被替换为简单PASS | current Goal binding固定序号、前驱、canonical ref、合同路径/hash；组件builder与final verifier均从candidate Git推导 | binding hash + caller-input负向测试 | OPEN，G01～G07 |
| R-32 | 首次外部anchor登记错误事实 | G07候选加固证据失真 | G01～G06不登记authority anchor；G07先做威胁模型和BOOTSTRAP资产复审 | G07 HARDENED transition测试 | DEFERRED_CANDIDATE_HARDENING |
| R-33 | current binding跳级或候选引入弱自动合同 | 未完成前置Goal却生成后续PASS | Program顺序、completed Goal回执、current binding和原子Goal切换 | skip/weak-contract/empty-predecessor负向测试 | OPEN，G01～G07；不依赖authority generation |
| R-34 | 自动测试进程读取Gate或Provider秘密 | 候选代码可伪造回执或泄漏账号 | G01～G06最小环境与secret allowlist；完整隔离OCI在G07评估 | 环境泄漏扫描；G07 OCI回归 | OPEN，CORE secret scan；OCI DEFERRED |
| R-35 | live回执自报模型/配置或来自controlled fixture | 错模型、错配置或fixture冒充真实Provider | CORE回执绑定候选Git计算的exact Provider配置、脱敏请求/响应hash、request ID和effect ID；逐effect外部签名在G07评估 | live/fixture区分、配置与response hash回读 | OPEN，G01 live `NOT_RUN` |
| R-36 | 原始证据位于其他Git worktree，或sealed验证重复开文件 | truth/私钥入Git或同一尝试读取不同字节 | 排除所有Git管理目录；consume前冻结完整snapshot图并复用 | linked-worktree + snapshot reuse回归 | OPEN，G01 |
| R-37 | live回执来自预组装事实而非真实Provider调用 | fixture或伪造行被包装成live证据 | CORE要求运行时HTTP结果、Provider request ID（若提供）、脱敏hash和持久effect交叉回读；外部逐effect签名留到G07 | live/fixture反例 + runtime/effect readback | OPEN，G01 CORE live待运行；HARDENED capture DEFERRED |
| R-38 | 过早冻结authority generation使后续Goal无法定义专属Gate | 后续版本被早期治理合同锁死 | ADR-014取消G01～G06 authority generation前置；每Goal独立冻结自己的CORE scorer/threshold | Goal contract transition测试 | SUPERSEDED_BY_ADR_014 |
| R-39 | 长耗时验证期间远端开发分支被移动 | 最终回执可能指向不同候选 | CORE以subject/tree再次回读；G07再评估唯一不可变ref与双readback | remote mismatch回归；G07 ref审查 | OPEN，CORE readback；immutable ref DEFERRED |
| R-40 | G02～G07自动合同只有通用测试 | 版本Outcome未被浏览器/PostgreSQL场景证明 | 七份合同各自冻结至少一个Goal后端检查和一个浏览器检查；缺文件或未运行即fail closed | Goal-specific contract覆盖测试 | MITIGATED_IN_CODE，未来Goal未运行 |
| R-41 | G07若启用外部ledger时先登记PASS、后写回执文件 | 磁盘失败却留下错误HARDENED授权 | 只在`HardeningDecision=REQUIRED`时使用；先独占写入、fsync并回读相同字节，再在外部事务登记 | G07 write-before-register回归 | DEFERRED_CANDIDATE_HARDENING；CORE不依赖ledger |
| R-42 | 前端/小程序锁文件含已知npm audit告警及Node engine边界告警 | 供应链漏洞或未来构建漂移 | 不在治理切片中自动升级；G01后建立独立依赖审计、可达性分析、升级与浏览器回归切片 | 当前重建记录frontend 10项、miniapp 41项；生产前必须分级处置 | OPEN，既有依赖风险 |
| R-43 | OCI镜像无法可靠绑定候选或携带多余Git历史 | 候选加固证据错误或泄露历史对象 | 现有object-pack方案保留实验；G07根据威胁模型复审 | G07 clean OCI readback | DEFERRED_CANDIDATE_HARDENING |
| R-44 | 联网/root镜像构建执行候选依赖脚本 | 候选污染验证环境 | G01～G06不以HARDENED OCI作为Gate；G07启用时使用无特权、锁定依赖构建 | G07 Dockerfile反例 | DEFERRED_CANDIDATE_HARDENING |
| R-45 | 可变OCI tag或archive TOCTOU导致回读另一镜像 | HARDENED Gate绑定错误候选 | 现有完整image ID/单句柄方案保留；G07启用时重验 | G07 OCI identity矩阵 | DEFERRED_CANDIDATE_HARDENING |
| R-46 | canonical远端分支缺少服务端不可变保护 | 长验证期间候选被移动 | CORE checkpoint使用subject/tree回读；G07或对外候选再要求不可变ref/服务端保护 | remote mismatch；G07仓库设置审计 | ACCEPTED PROCESS RESIDUAL，G07评估 |
| R-47 | 类型化数据库行被误当作Provider真实调用证明 | 任意成功状态/hash被包装成live证据 | G01 CORE要求HTTP/runtime/effect交叉回读和fixture显式隔离；外部逐effect签名在G07评估 | live source/config/hash反例 | OPEN，G01 CORE live待运行；HARDENED exporter DEFERRED |
| R-48 | 治理、安全或审查基础设施反客为主 | 用户主链长期无进展，真实模型/Provider/Gate继续NOT_RUN | 纯治理最多连续一个checkpoint；两次`Product progress = NONE`强制转向；P2分级和两轮复审上限 | CURRENT_GOAL Product progress/Governance ratio；每checkpoint检查下一动作 | ACTIVE，所有Goal |
| R-49 | 并行worktree基线、指导版本或owned paths漂移 | 合并覆盖他人改动、证据绑定错误或重复开发 | `current_work_packages.json`、同baseline、唯一集成者、路径前缀冲突和只读降级机器校验 | work package registry/Git validator + 串行合并回读 | OPEN，G01～G07 |
| R-50 | 截图孤儿文件、OCR映射残留或敏感图片直接外发VL | 隐私泄漏且用户无法删除 | multipart临时批次、所有终态清理、OCR SourceDocument TTL/delete、本地敏感遮蔽 | G04 cleanup/TTL/delete/VL redaction矩阵 | OPEN，G04 |
| R-51 | 分享fragment未及时清除、秘密进入服务端URL/日志/Referer，或撤销后缓存仍可访问 | 私人行程被未授权查看 | token摘要、fragment在分析启动前经body换HttpOnly capability并清除、撤销/过期缓存失效和账号删除级联 | G06 token/IDOR/cache/delete浏览器矩阵 | OPEN，G06 |
| R-52 | G07因历史加固代码存在而默认恢复整套authority/OCI链 | 候选收口再次被治理工程拖住 | 先产出`HardeningDecision`；无明确威胁收益时`NOT_REQUIRED_WITH_RATIONALE`，只处理blocking风险 | G07威胁模型、成本收益和决策回执 | OPEN，G07 |

风险状态只能通过对应Goal的实际证据关闭。文档、计划或单次测试不能把OPEN改为CLOSED。
