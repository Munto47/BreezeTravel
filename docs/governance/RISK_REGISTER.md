# TC-VNEXT 风险登记

> 状态：`ACTIVE`
>
> Program：`TC-VNEXT-2026`
>
> 日期：2026-08-28

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
| R-26 | 开发任务自行创建key、registry或通用PASS回执 | Gate成为自我证明，无法区分四类必需证据 | 首次Git只建不能签发证据的BOOTSTRAP；ACTIVE前交付仓库外隔离signer，八类外部私钥、角色专用schema/签名与上游verification receipt | bootstrap拒绝 + signer进程隔离 + policy历史回读 + final fresh checkout | OPEN，外部signer未交付，禁止ACTIVE |
| R-27 | sealed指标由调用者填写，或外部文件在hash后被替换 | blind未真正评分或回执绑定错误字节 | 原始输入/预测/truth确定性scorer、HMAC truth commitment、单句柄snapshot、hardlink拒绝、独占输出与DB-first nonce | sealed mutation/replay/TOCTOU矩阵 | OPEN，G01 |
| R-28 | 候选自带弱化verifier或自行建立新签名根 | 恶意/错误候选可给自己签发PASS | 当前Goal generation协议字节锚、生成代码hash、仓库外签名authority anchor、immutable candidate ref与fresh checkout | immutable-byte负向测试 + external anchor readback | OPEN，G01～G07 |
| R-29 | 组件签名者手填PASS摘要而未运行底层事实检查 | Gate显示通过但测试、Provider、审查或blind并未成立 | 目的专一builder只接受raw paths；final端重跑自动命令并重算Provider/panel/sealed验证 | forged-summary反例 + 四组件fresh revalidation | OPEN，G01～G07 |
| R-30 | 本机管理员或custodian失陷后整体回滚SQLite或窃取运营密钥 | nonce重放或伪造新回执 | v1明确只提供进程隔离；registry/密钥最小暴露；疑似失陷立即停止Gate，新generation需所有者批准；更强威胁模型使用远端透明日志/HSM | registry完整性readback + generation事件；不得宣称host-admin resistant | ACCEPTED RESIDUAL，阻断受影响Gate |
| R-31 | Goal切换或候选调用者选择弱化的自动Gate合同 | 应运行的检查被替换为简单PASS | current Goal binding固定序号、前驱、canonical ref、合同路径/hash；组件builder与final verifier均从candidate Git推导 | binding hash + caller-input负向测试 | OPEN，G01～G07 |
| R-32 | 首次外部anchor登记错误事实或冻结未完成live stub | generation 1永久错误或G01晋级死锁 | BOOTSTRAP不能登记anchor；完整capture/signer后一次切ACTIVE，anchor API只收candidate commit并从Git、tree、immutable bundle与canonical远端推导 | bootstrap拒绝 + active transition + derived-anchor负向测试 + external readback | OPEN，ACTIVE与首次锚未运行 |
| R-33 | current binding跳级或候选引入弱自动合同 | 未完成G01～G06却生成后续PASS | 跨代稳定七Goal转换表/合同hash；generation必须与Goal同序；仓库外append-only predecessor PASS ledger | generation/skip/weak-contract/empty-ledger负向测试 | OPEN，G01～G07 |
| R-34 | 自动测试进程读取FINAL、custody或Provider秘密 | 候选代码可伪造回执或泄漏账号 | 候选命令只在无外网、宿主挂载/PID和秘密的OCI镜像执行；合成HOME/XDG/npm profile | OCI参数、环境注入和镜像绑定回归 | OPEN，G01～G07；正式OCI Gate未运行 |
| R-35 | live回执自报模型/配置或来自controlled fixture | 错模型、错配置或fixture冒充真实Provider | 正式exporter先fail closed；完整实现时由custody registry/mint和逐effect HTTPS capture签名建立来源，再由候选Git独立计算AMap/Qwen binding | live source/config/capture负向矩阵 | OPEN，G01；正式live `NOT_RUN` |
| R-36 | 原始证据位于其他Git worktree，或sealed验证重复开文件 | truth/私钥入Git或同一尝试读取不同字节 | 排除所有Git管理目录；consume前冻结完整snapshot图并复用 | linked-worktree + snapshot reuse回归 | OPEN，G01 |
| R-37 | live exporter从通用JSON列或任意数据库读取预组装事实 | 写入方可伪造schema合法的Provider证据 | 通用JSON入口已删除；任意DSN正式入口已禁用；完整链必须绑定custody登记数据库、一次性mint与逐effect capture签名 | generic JSON/DSN/query/capture负向测试 + live readback | DIRECT PATH DISABLED，完整live链待实现 |
| R-38 | generation 1永久冻结G01 scorer导致G02～G07无法定义专属Gate | 后续版本只能复用错误门槛或请求手工破例 | 每Goal一代、上一PASS后精确加一；稳定Program事实跨代不变，Goal专属协议在激活commit冻结 | generation transition与前驱ledger负向测试 | MITIGATED_IN_CODE，G02链未运行 |
| R-39 | 长耗时验证期间远端开发分支被移动 | 最终回执可能指向验证前后不同候选 | 每候选唯一不可变ref；组件验证前和签名前双readback；registry事务提交前再次回读 | ref TOCTOU/remote mismatch回归 | MITIGATED_IN_CODE，正式远端Gate未运行 |
| R-40 | G02～G07自动合同只有通用测试 | 版本Outcome未被浏览器/PostgreSQL场景证明 | 七份合同各自冻结至少一个Goal后端检查和一个浏览器检查；缺文件或未运行即fail closed | Goal-specific contract覆盖测试 | MITIGATED_IN_CODE，未来Goal未运行 |
| R-41 | PASS先写ledger后写回执文件 | 磁盘失败却已授权下一Goal | 先独占写入、fsync并回读相同字节，再在外部事务登记 | write-before-register回归 | MITIGATED_IN_CODE，正式PASS未运行 |
| R-42 | 前端/小程序锁文件含已知npm audit告警及Node engine边界告警 | 供应链漏洞或未来构建漂移 | 不在治理切片中自动升级；G01后建立独立依赖审计、可达性分析、升级与浏览器回归切片 | 当前重建记录frontend 10项、miniapp 41项；生产前必须分级处置 | OPEN，既有依赖风险 |
| R-43 | OCI镜像复制源码但排除Git元数据，无法在容器内证明候选身份；复制完整历史又可能暴露已删除对象 | 自动结果可能绑定错误字节，或镜像携带不必要历史 | 构建器输出治理基线到候选的受控浅object pack；镜像重建精确HEAD/tree且运行期无宿主挂载/网络 | object-pack重建测试 + clean候选容器readback | MITIGATED_IN_CODE，clean候选OCI回读待运行 |
| R-44 | candidate lock在root且联网的镜像构建阶段执行二进制 | 候选可污染Python verifier或伪造后续自动结果 | 候选依赖只在无特权隔离stage以`--ignore-scripts`解析；root阶段Playwright来自authority-owned exact lock并纳入Program core | Dockerfile反例扫描 + clean候选OCI build/readback | MITIGATED_IN_CODE，clean OCI待运行 |
| R-45 | 可变OCI tag、archive路径重开或缓存tag短路使旧自动回执无法证明fresh readback消费的是原镜像 | Gate可能保存/运行另一镜像、接受无效archive，或依赖单一Docker daemon短期状态 | 按完整image ID经匿名stdout保存；解析单image/config/tag/layer结构后用独占句柄发布；fresh verifier总是单句柄复制到匿名快照、经stdin加载并只按完整image ID复跑 | cached-tag无效archive、路径替换、额外image/tag、hash/identity mismatch反例；clean跨daemon回读 | MITIGATED_IN_CODE，正式archive及跨daemon回读待运行 |
| R-46 | canonical远端分支缺少服务端不可变保护 | 两次回读之间仍可能被具备远端写权限者移动 | 每候选唯一freeze ref、长验证前后及registry提交前回读；正式Git服务端分支保护仍是外部控制 | remote mismatch/TOCTOU反例 + 仓库设置审计 | ACCEPTED PROCESS RESIDUAL，正式Gate前需确认远端保护 |
| R-47 | 类型化数据库行与exporter签名被误当作Provider真实调用证明 | 任意成功状态/hash可被包装成live证据 | 当前正式exporter无条件fail closed；后续只接受custody固定registry、一次性mint、冻结HTTPS捕获器逐effect签名和完整coverage | arbitrary DB、重放、篡改、fixture升级负向矩阵 | OPEN，G01；阻断`LIVE_PROVIDER_EVIDENCE` |

风险状态只能通过对应Goal的实际证据关闭。文档、计划或单次测试不能把OPEN改为CLOSED。
