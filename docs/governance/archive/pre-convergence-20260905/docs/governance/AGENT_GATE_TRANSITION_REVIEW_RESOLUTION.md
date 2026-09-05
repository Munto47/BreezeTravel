# Agent Gate 治理过渡审查与处置

> 状态：`IN_PROGRESS`
>
> 证据等级：`MULTI_AGENT_SIMULATED_REVIEW`
>
> 人类证据：`false`

## 1. 审查边界

本文件记录 G01 内“旧真人前置条件 → Agent Gate”治理过渡的独立反方审查与修复。它不是 G01 的正式 Gate 回执，不代表 live Qwen、高德、sealed blind、H1、生产或商业证据已经运行。

初轮由产品、评测完整性、可靠性/安全三个只读任务分别检查；主开发任务只负责修复，不把既有测试通过当作审查 PASS。候选 commit 变化后，初轮结论只保留为问题来源，最终结论必须由新的同 commit 复审与 fresh readback 产生。

## 2. 初轮高优先级发现与处置

| 发现 | 风险 | 处置 | 当前状态 |
|---|---|---|---|
| 开发任务可自行创建 custody 身份并证明自己 | 过程隔离可被自签名回执绕过 | 由不继承开发上下文的独立 ultra 任务生成八组仓库外 Ed25519 私钥与外部 registry；仓库只保存公钥和指纹 | `RESOLVED / REGISTRY_COMPATIBILITY_PASS / FORMAL_SIGNING_NOT_RUN` |
| live Provider 回执可由手写 JSON 冒充 | fixture 或声明可能被包装成真实调用 | 初版签名exporter仍不足，已由2.1节的custody registry/mint/capture链要求取代；完整链前正式入口fail closed | `SUPERSEDED_BY_2.1 / LIVE_NOT_RUN` |
| 通用组件 `PASS` 回执可以相互替代 | 最终聚合器无法证明四类 Gate 各自成立 | 自动产品、live Provider、多 Agent panel、sealed blind 使用四种严格 schema、四种权限和四种上游 verification receipt | `RESOLVED_IN_CODE` |
| 外部文件存在 hardlink、替换和半写风险 | 仓库外边界与 hash 可能遭 TOCTOU 绕过 | 单文件句柄 snapshot，校验 device/inode/size/mtime；拒绝多 hardlink；输出只允许不存在目标、独占创建、完整写入与 fsync | `RESOLVED_IN_CODE` |
| 调用者可以提交预期 identity/hash/destination | 验证器可能验证调用者自己的假设 | 候选 commit/tree、origin、config/data bundle、policy 与 verifier 从冻结 Git blob 和当前Goal generation锚推导；正式 origin 固定为 canonical HTTPS | `RESOLVED_IN_CODE / ANCHOR_PENDING_COMMIT` |
| sealed score 可以接受聚合指标 | blind truth 未必真正参与评分 | 新增目的专一 scorer，直接读取仓库外输入、候选预测、Qwen/AMap 回执和 agent reference truth，重新计算完整指标与 taxonomy；CLI 不存在 `--metrics` | `RESOLVED_IN_CODE / SEALED_NOT_RUN` |
| 描述性整句可能仅凭启发式被截成地点 | 会复现“整句话做地点卡”的严重错误 | agent reference 只允许 Provider 接受的完全相等原子 span 成为 executable；不确定边界必须保持 `UNCERTAIN`、不搜索、不自动匹配 | `RESOLVED_IN_CONTRACT` |
| final Gate 可在干净 checkout 回读、却从开发工作区加载验证模块 | 未提交或被注入的代码可能为候选签发 PASS | 正式聚合器必须从候选干净 checkout 产生规范化unsigned request并逐个验证Git blob；最终签名由不导入候选模块的仓库外signer重新校验后完成，候选进程不接收key path。当前BOOTSTRAP正式CLI在任何密钥读取前fail closed | `BOOTSTRAP_FAIL_CLOSED_IN_CODE / EXTERNAL_SIGNER_EXECUTION_NOT_RUN` |
| 候选可修改verifier后再用候选自己的verifier验证自己 | Goal策略锚可能成为自选信任根 | 当前generation policy逐字节、核心协议字节、生成合同中的代码hash三重绑定；每代push后还必须由外部custody registry登记anchor commit/tree、协议hash和八公钥集合 | `RESOLVED_IN_CODE / EXTERNAL_ANCHOR_REGISTRATION_PENDING_FIRST_COMMIT` |
| sealed scorer在nonce消费前读取truth | 可对同一tranche反复试预测并择优 | 先以单句柄冻结全部非truth字节并计算attempt commitment，CAS `MINTED → CONSUMED`成功后才首次打开truth；registry绑定score input、score和最终attempt hash | `RESOLVED_IN_CODE / SEALED_NOT_RUN` |
| G01静态路径清单无法安全覆盖G02～G07 | 后续新增实现/数据可能不进入语义hash | 改为递归`config_roots/data_roots`并去重所有Git blob；完整candidate tree始终绑定；每个Goal用独立`current_goal_binding.json`声明序号、前驱和自动Gate合同 | `RESOLVED_IN_CODE` |
| Goal、远端ref和自动Gate合同由CLI填写 | 可对旧Goal或弱化合同重放PASS | canonical ref固定在不可变policy；Goal和自动Gate合同路径/hash从candidate Git中的current binding推导；CLI不再接受Goal/ref/合同 | `RESOLVED_IN_CODE` |
| 组件只比较上游PASS摘要 | 持有组件钥匙即可手填计数 | 四个目的专一builder只接受原始artifact路径；final verifier重新执行自动命令、Provider raw validator、review panel validator和sealed registry/scorer validator；反例测试确认签名的伪摘要被拒绝 | `RESOLVED_IN_CODE` |
| review/adjudication同一路径多次打开 | 文件替换可让判断字节与hash字节不同 | 每个外部artifact只读一个句柄snapshot；同一byte buffer完成解析、hash和证据校验，repository证据从candidate Git blob读取 | `RESOLVED_IN_CODE` |
| Windows父目录junction/reparse竞态 | 外部输入/输出可能被绕回仓库 | Windows使用不跟随reparse的CreateFile句柄并核对final path，拒绝reparse/hardlink；输出独占创建并在句柄上复核 | `RESOLVED_IN_CODE` |
| registry整体回滚与固定密钥恢复 | 本机管理员或custodian失陷时可破坏单机状态 | 明示v1仅提供进程隔离，不声称抵抗host-admin；该威胁需要批准的远端透明日志/HSM和新authority generation；密钥丢失/泄漏时停Gate，不静默轮换 | `ACCEPTED_THREAT_BOUNDARY / OWNER_APPROVAL_REQUIRED_FOR_GENERATION_CHANGE` |
| 首次authority anchor仍接受调用者填写commit/tree/hash，且若直接冻结live stub会让G01死锁 | 一次错误登记会永久污染generation 1，或永远无法产生live PASS | 首次Git新增只允许不能签发证据的BOOTSTRAP；登记API拒绝BOOTSTRAP。ACTIVE加载还强制固定路径、`SEALED_CUSTODY`签名的activation-readiness回执，逐项绑定bootstrap commit/tree/policy/core、排除回执自身后的完整ACTIVE tree、ACTIVE policy/program core/config/data、双lane capture与外部signer执行回执；任一其他Git blob变化都会拒绝旧回执 | `BOOTSTRAP_AND_ACTIVATION_REPLAY_GUARD_RESOLVED / ACTIVE_TRANSITION_NOT_RUN` |
| 候选可跳到G07并选择恒过的弱合同 | Program顺序和必需检查可被候选自行取消 | authority policy首次固定G01～G07精确transition表与七份合同hash；仓库外`goal_gate_passes`只追加上一Goal FINAL_GATE签名PASS，G02～G07缺前驱登记即拒绝 | `RESOLVED_IN_CODE / TRANSITION_CHAIN_NOT_YET_EXERCISED` |
| AMap/Qwen expected配置从待验证回执自身取得 | 错模型、prompt、schema或Provider配置仍可能包装成live | verifier从candidate Git独立计算Provider binding及Qwen五类配置hash；model panel/binding未FROZEN拒绝；来源真实性另由2.1节capture链证明 | `CONFIG_PATH_RESOLVED / LIVE_NOT_RUN` |
| 自动产品命令继承FINAL/custody/Provider秘密 | 候选测试代码可能读取角色私钥或账号凭据 | 正式命令不在宿主子进程执行；OCI容器无外网、宿主挂载/PID和秘密，并使用合成profile | `RESOLVED_IN_CODE / OCI_BUILD_NOT_RUN` |
| “仓库外”仍可指向同仓库其他worktree、bare/separate Git目录 | 原始输出、truth或私钥可能被纳入Git | 枚举linked worktree，并用Git worktree/git-dir/bare/common-dir readback检查任意Git管理位置 | `RESOLVED_IN_CODE` |
| controlled fixture可声明PostgreSQL live来源，live exporter路径可任意选择 | fixture或任意候选blob可能伪装live | schema强制execution mode与source registry一一对应；policy固定AMap/Qwen purpose-specific exporter和类型化effect表，validator比较candidate exporter blob | `RESOLVED_IN_CODE / LIVE_NOT_RUN` |
| sealed scorer冻结后仍按路径重开Provider/Qwen文件 | 同一评分尝试可能读取不同字节 | nonce消费前一次冻结全部非truth主件与子回执；Provider、Qwen、prediction验证器接收同一snapshot集合，评分后不再按路径复读 | `RESOLVED_IN_CODE / SEALED_NOT_RUN` |
| live exporter读取通用artifact表中的预组装JSON | 运行时写入方可伪造一份schema合法的“真实回执” | 通用payload入口已删除；类型化表仅为必要非充分条件，2.1节进一步要求custody与逐effect capture签名 | `GENERIC_PAYLOAD_CLOSED / LIVE_NOT_RUN` |
| generation 1把G01 scorer/threshold永久冻结给G02～G07 | 后续Goal无法定义自己的sealed与协议 | 改为Goal同序generation 1～7；每代在上一PASS后的原子过渡commit冻结下一Goal协议，Program表、公钥和registry跨代稳定 | `RESOLVED_IN_CODE / TRANSITION_CHAIN_NOT_YET_EXERCISED` |
| G01原始live回执可被G02 wrapper复用 | 错Goal事实可能被包装成当前组件 | Provider/Qwen runtime、DB、HTTP和index均与`expected_goal_id`逐层比较，Goal不一致fail closed | `RESOLVED_IN_CODE` |
| G02～G07自动合同只有通用测试 | 通用pytest/build通过也不能证明地图、Audit、OCR、知识、记忆或候选Outcome | 七份冻结合同分别声明Goal专属PostgreSQL/领域检查与浏览器场景；对应文件未实现或未运行即失败 | `RESOLVED_IN_CONTRACT / FUTURE_EXECUTION_NOT_RUN` |
| 宿主子进程即使清空env仍可读取父进程/用户profile | 候选测试可能窃取Gate、Provider或包管理器秘密 | 正式自动产品Gate改为无外网、宿主挂载/PID、capabilities和用户profile的OCI镜像；PostgreSQL、worker和浏览器只在容器loopback内启动 | `RESOLVED_IN_CODE / OCI_BUILD_NOT_RUN` |
| canonical开发分支在长耗时Gate期间可移动 | 前读为A、签名前已变成B，形成TOCTOU | 每个Goal/commit使用唯一`codex/agent-gate-candidates/gNN-<sha>` ref，组件验证前后各回读；外部ledger事务提交前再次回读 | `RESOLVED_IN_CODE / FORMAL_REMOTE_GATE_NOT_RUN` |
| bare/separate Git目录未被“仓库外”检查捕获，PASS先登记再落盘 | 原始证据可能进入Git；磁盘失败仍授权下一Goal | `rev-parse`同时检查worktree/git-dir/bare/common-dir；最终PASS先独占写/fsync，再登记 | `RESOLVED_IN_CODE` |

### 2.1 同一工作树修复后的专项复核项

| 发现 | 严重度 | 处置 | 验证状态 |
|---|---|---|---|
| final Gate只校验派生binding，未结构化校验`CURRENT_GOAL.md`正文 | P1 | 新增机器可读Goal状态块及`CurrentGoalDocumentState`；final Gate绑定原始文档hash，并拒绝G01未完成却激活G02 | `RESOLVED_IN_CODE / TARGETED_TEST_PASS` |
| 当前代协议可被后一代authority重新生成时替换，runner recipe/entrypoint/context hash曾漂移 | P0 | `program_core_paths`固定为全部不可变协议路径且跨G01～G07逐字节稳定；七份合同逐一绑定runner三件套实际hash | `RESOLVED_IN_CODE / TARGETED_TEST_PASS` |
| 候选当前目录可用伪造`docker.exe`/`git.exe`劫持正式Gate | P1 | 宿主工具只允许操作系统可信绝对路径；候选CWD和PATH阴影反例均被拒绝 | `RESOLVED_IN_CODE / TARGETED_TEST_PASS` |
| Docker不可用时可能只抛异常而没有可审计`NOT_RUN` | P2 | 自动组件为构建失败/运行器不可用物化显式execution manifest，且`NOT_RUN`永远不能聚合为PASS | `RESOLVED_IN_CODE / TARGETED_TEST_PASS` |
| live类型化registry只有SQL字符串测试 | P2 | 在fresh PostgreSQL真实建表、约束、角色ACL、append-only触发器、写入和固定查询；这些结果只证明受控数据库合同，不再被描述为live事实 | `RESOLVED_AS_AUTOMATED_CONTRACT_TEST / POSTGRES_INTEGRATION_PASS` |
| final PASS文件与外部ledger之间崩溃可能留下不可恢复状态 | P2 | 先物化并fsync，再登记；新增同一已签名PASS的幂等恢复路径，异内容冲突fail closed | `RESOLVED_IN_CODE / TARGETED_TEST_PASS` |
| 多层Provider验证重复按路径读取同一外部证据 | P2 | 顶层一次冻结全部主件和子回执，后续验证复用同一snapshot图 | `RESOLVED_IN_CODE / TARGETED_TEST_PASS` |
| `ls-remote`在registry锁内无截止时间 | P2 | 所有远端readback使用固定deadline；超时不登记、不签发PASS | `RESOLVED_IN_CODE / TARGETED_TEST_PASS` |
| OCI配方未带入`miniapp/.npmrc`，缺Git，且递归chown耗时失控 | P1 | 保留仓库既有peer策略、加入快照源Git，把权限调整缩到可写顶层；首次配方预检已成功构建 | `RESOLVED_IN_RECIPE / CONTAINER_TEST_PENDING_CLEAN_COMMIT` |
| 排除`.git`后容器无法自行证明commit/tree；直接复制完整历史又可能带入已删除敏感对象 | P1 | 构建器生成从治理基线到候选的受控浅Git object pack；镜像内重建精确HEAD/tree，运行期仍无宿主挂载和网络 | `RESOLVED_IN_CODE / HOST_RECONSTRUCTION_TEST_PASS / OCI_READBACK_PENDING_CLEAN_COMMIT` |
| scorer导入的旧`contracts.py/scorer.py`未进入不可变闭包 | P0 | 两个模块加入Program core、协议代码hash和final运行时Git blob校验 | `RESOLVED_IN_CODE / TARGETED_TEST_PASS` |
| CURRENT_GOAL只读取第一组可见Goal，且完成栏可混入假PASS | P1 | 全文要求唯一Goal ID/Status/标题；完成栏只允许唯一预PASS行并拒绝所有正向Gate声明，H1/生产/商业固定逐项`NOT_RUN` | `RESOLVED_IN_CODE / MUTATION_TEST_PASS` |
| authority anchor只在派生前回读一次可移动远端ref | P1 | 在外部registry事务提交前再次回读同一commit；变化即回滚，服务端不可变保护仍登记为外部残余控制 | `RESOLVED_IN_CODE / TOCTOU_TEST_PASS / PROCESS_RESIDUAL_RECORDED` |
| Docker build主context或ignored宿主文件可污染候选源码 | P1 | 主context为空；源码只从Git object pack重建的named context复制，并验证commit/tree；ignored文件与已删除中间blob反例均拒绝 | `RESOLVED_IN_CODE / WINDOWS_HOST_TEST_PASS` |
| Windows只读pack index导致candidate context无法清理 | P1 | 删除Git元数据时清除只读属性后重试，并兼容baseline为root commit | `RESOLVED_IN_CODE / WINDOWS_HOST_TEST_PASS` |
| candidate Playwright在root且联网的构建阶段执行 | P1 | candidate npm依赖只在`USER node`隔离stage解析；root浏览器安装只执行Program core中的authority-owned exact Playwright lock | `RESOLVED_IN_RECIPE / CLEAN_OCI_BUILD_PENDING` |
| 可变OCI tag、缓存tag短路、attestation伪装第二镜像和archive路径重开不能证明fresh readback消费了签名字节 | P1 | 构建配方改由候选Git blob通过stdin传入；legacy graph必须精确等于primary OCI graph，attestation严格限制为unknown/unknown与in-toto；Windows最终句柄与POSIX逐级openat固定路径；verifier总是单句柄复制到匿名快照、经stdin加载并按完整image ID复跑 | `RESOLVED_IN_CODE / ADVERSARIAL_OCI_AND_SAME_DAEMON_PASS / CROSS_DAEMON_PENDING` |
| 类型化PostgreSQL行仍不能证明真实Provider调用 | P0 | 移除正式入口对任意DSN的信任并无条件fail closed；协议要求custody固定registry、一次性mint、直接HTTPS capture逐effect签名和coverage，完整实现前live保持`NOT_RUN` | `UNSOUND_PATH_CLOSED / FORMAL_CAPTURE_CHAIN_PENDING_G01` |
| final Gate远端Git操作无deadline | P2 | 所有final Git readback固定60秒，超时fail closed | `RESOLVED_IN_CODE / TARGETED_TEST_PASS` |
| 候选Python父包可在持有角色私钥的进程初始化 | P0 | 当前BOOTSTRAP禁止任何组件/anchor；四个正式候选CLI已删除全部key-path环境读取并在BOOTSTRAP直接fail closed；`component_builders`、`live_export`和`final_gate`也已移除开发签名器导入和private-key参数，只接受仓库外分离签名并使用冻结公钥验签。ACTIVE仍必须由custody签名readiness证明仓库外signer不导入候选代码且候选env/process无key path或key bytes | `CANDIDATE_KEY_PATH_REMOVED / STATIC_AND_SIGNATURE_GUARD_PASS / EXTERNAL_SIGNER_EXECUTION_NOT_RUN` |
| 旧AMap/Qwen LIVE回执可绕过正式exporter进入组件Gate | P0 | live component builder和verifier在完整capture execution receipt落地前均无条件拒绝；旧schema、旧签名和一致手写JSON不能晋级 | `UNSOUND_COMPONENT_PATH_CLOSED / CAPTURE_RECEIPT_PENDING_G01` |
| CURRENT_GOAL可追加第二个Completion record隐藏假PASS | P1 | 全文要求唯一Completion heading，并对Goal archived/Next activated行做全局唯一校验 | `RESOLVED_IN_CODE / MUTATION_TEST_PASS` |
| 跨generation稳定事实字段可能因手工tuple漏项而漂移 | P1 | 除generation、phase和单调冻结时间外，manifest其余完整规范化内容默认全部稳定；Program core与immutable路径集合必须完全相等 | `RESOLVED_IN_CODE / FIELD_MUTATION_TEST_PENDING` |
| Dockerfile在hash检查后仍从可变工作树路径读取 | P1 | recipe、entrypoint和context policy直接从candidate Git blob冻结到独占临时目录；Docker只读取该快照 | `RESOLVED_IN_CODE / GIT_BLOB_TOCTOU_TEST_PENDING` |
| activation readiness的data分组包含回执自身 | P0 | ACTIVE data bundle使用与完整tree一致的固定回执排除；回执字节变化不改变摘要，其他data blob或mode变化仍改变摘要；固定tree排除同时证明回执路径真实存在 | `SELF_REFERENCE_REMOVED / REAL_GIT_BLOB_MUTATION_TEST_PASS / ACTIVE_TRANSITION_NOT_RUN` |
| SQLite custody连接依赖GC释放文件句柄 | P2 | 所有registry连接改为`closing(connection)`包裹事务上下文，成功、冲突和只读路径均确定性close；Windows回归在无`gc.collect()`时立即rename并移回 | `RESOLVED_IN_CODE / WINDOWS_IMMEDIATE_RENAME_PASS` |
| authority anchor在远端readback期间持锁可能超过SQLite默认等待 | P2 | registry显式设置65秒busy timeout，覆盖60秒Git deadline；统一transaction context在BEGIN、SQL主体和commit/rollback全域捕获locked/busy并转换为同一可重试领域错误，提交前第二次remote readback保持不变 | `RESOLVED_IN_CODE / CONCURRENT_IDEMPOTENT_RETRY_AND_COMMIT_LOCK_TEST_PASS` |

以上仍属于开发期多Agent模拟审查与自动测试，不是正式Gate回执。最终复审必须针对同一个已提交候选；候选字节再变化则该结论失效。

## 3. 信任链结果

当前实现形成以下闭环：

```text
Goal同序权限generation锚 + 仅从Git/远端推导的仓库外签名anchor登记
→ 跨代稳定G01～G07转换表 + 仓库外上一Goal PASS ledger
→ 候选 commit/tree/config/data Git blob 回读
→ 角色分离的外部私钥签名
→ 原始artifact的目的专一builder与严格上游verification receipt
→ secret-free OCI自动检查 + custody registry/mint/逐HTTP capture签名的AMap/Qwen live链
→ DB-first one-shot sealed mint/consume/complete
→ 原始输入/预测/truth 的确定性评分
→ 四类不可互换组件回执 + final端原始证据重算
→ 独立 clean checkout + immutable candidate ref前后双readback
→ PASS耐久物化后再登记
→ FINAL_GATE 签名的 AGENT_GATE_PASS
```

任何一环缺失、`NOT_RUN`、候选字节变化、签名角色不匹配、旧 nonce 重放、外部文件被替换或存在未处理 P0/P1/当前 Goal P2，均 fail closed。

## 4. 尚未运行

- 权限策略首个 Git anchor、push 和远端 readback；
- 干净候选commit的OCI内部commit/tree回读与完整自动组件；
- Qwen 目录发现、Max/Plus/Flash 同数据 live 比较；
- 高德地点及 walking/transit live lane；
- A/B agent reference、ultra 裁决与最终三角色 Gate 审查；
- 一次性 sealed blind；
- 四组件正式签名与最终 `AGENT_GATE_PASS`。

以上项目在实际执行前必须保持 `NOT_RUN`。本治理过渡完成后 G01 仍为 `IN_PROGRESS`，不得提前激活 G02。

独立 authority task 已对当时registry schema完成仓库外迁移和回读：policy identity/path pin一致，8/8 Ed25519 sign/verify通过，空库 `mint → consume → complete` SQL探针通过并回滚，sealed run计数仍为0，仓库写入为false。首个subject commit产生后还需由同一独立任务迁移新增的authority-anchor与Goal-pass表，并使用只接收candidate commit的新API登记唯一generation-1 anchor；登记前final Gate必然fail closed。该结果属于过程设施验证，不是Provider或blind证据。
