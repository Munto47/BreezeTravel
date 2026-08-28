# TC-VNEXT Agent Gate Protocol

> 状态：`ACCEPTED`
>
> 版本：`agent-gate-v2`
>
> 适用范围：`TC-VNEXT-G01`～`TC-VNEXT-G07`
>
> 不适用范围：H1 真人可用性、生产、公网上线、商业证据和 `main` 合并

## 1. 目的与证据边界

G01～G07 的开发门禁使用可复现、上下文隔离的多 Agent 模拟审查，不再把真人标注员、真人裁决员或组织外 blind custodian 作为版本晋级前提。该协议只提供工程过程隔离，不能冒充真人独立性、真人可用性或市场证据。

证据等级固定为：

- `AUTOMATED_TEST`：确定性测试、静态检查、构建、浏览器自动化和可重放 fixture；
- `LIVE_PROVIDER_EVIDENCE`：绑定确切配置、调用回执和候选 commit 的真实 Provider 结果；
- `MULTI_AGENT_SIMULATED_REVIEW`：按本协议隔离执行的 GPT 审查、标注与裁决；
- `SEALED_AGENT_BLIND`：独立 Codex 任务保管答案并只返回聚合指标、错误类别、receipt hash 和结论；
- `HUMAN_USABILITY`：经单独批准的目标用户测试；
- `PRODUCTION_EVIDENCE`：生产环境的真实运行证据。

`MULTI_AGENT_SIMULATED_REVIEW` 和 `SEALED_AGENT_BLIND` 的回执必须声明 `human_evidence=false`。H1、生产和商业层级未运行时必须保持 `NOT_RUN`。

## 2. 固定任务编组

### 2.1 数据参考与裁决

需要语义、OCR、知识或其他参考答案时：

1. 标注任务 A：`gpt-5.6-sol / xhigh`，`fork_turns=none`；
2. 标注任务 B：`gpt-5.6-sol / xhigh`，`fork_turns=none`；
3. A/B 在提交前均不得看到候选输出、对方输出或已有答案；
4. A/B 输出冻结并计算 hash 后，启动新的 `gpt-5.6-sol / ultra` 裁决任务；
5. 裁决任务只能读取输入、冻结的 A/B 输出、裁决 prompt 和 schema，不得继承开发上下文；
6. 地点身份、路线、营业等硬事实必须绑定真实 Provider 回执，Agent 知识不能独立建立权威事实。

字段只能使用 `agent_reference`、`agent_adjudication` 等诚实命名，不得使用 `human_label`、`human_gold` 或“真人确认”。旧真人 schema 和历史回执保持逐字节只读。

### 2.2 每个 Goal 的 Gate 审查

候选 commit 冻结后并行启动三个 `gpt-5.6-sol / xhigh` 只读任务：

1. `PRODUCT_UX`：用户黑话、长 ID、原文泄漏、红色滥用、操作路径和空状态；
2. `SEMANTIC_DOMAIN`：错城、错类别、整句地点、可选/排除/途经、跨天、歧义和长文本；
3. `RELIABILITY_SECURITY`：revision、幂等、并发、Provider 失败、隐私、删除、恢复、延迟和资源预算。

三个输出冻结并计算 hash 后，启动新的 `gpt-5.6-sol / ultra` Gate 裁决任务。裁决只接受可复现、带证据的发现；每个发现必须包含预期行为、实际行为、复现步骤、证据和严重度。未执行的检查写 `NOT_RUN`，不得因已有自动测试通过而推断 PASS。

主 Codex 负责修复全部P0/P1和属于当前Goal的P2；Program明确归属后续Goal的P2必须记录scope disposition与依赖，不能静默忽略。候选 commit、prompt、schema、配置或数据绑定任一改变，旧 Gate 结论立即失效；必须重跑受影响审查。最终在干净 checkout 对同一 commit 做 fresh readback。

## 3. 任务与回执绑定

每个任务至少记录：

- task ID、角色、模型、推理等级和 `fork_turns=none`；
- candidate commit/tree、prompt hash、input bundle hash、output schema hash；
- 启动、完成和冻结时间；
- 输出 hash、原始输出的仓库外位置类型；
- 是否看到候选、对方结果、已有 verdict；
- `human_evidence=false`。

Gate验证器不得只相信这些字段。它必须：

- 从Git回读candidate commit并验证tree；
- 逐项比较Goal、commit/tree、config、data和每个角色的input bundle；
- 回读每个必选场景的证据文件并校验SHA-256；`NOT_RUN`、缺证据、证据不可读或任一reviewer非`PASS`都不能晋级；
- 从三份冻结review重新计算完整finding集合，要求ultra裁决逐项处置且严重度不得改写；
- 把以上绑定和三份review hash写入最终聚合回执。

普通dev/validation工具必须使用split-only loader，只能打开明确请求的单个输入文件；
不得先读取全数据集再声明blind读取数为0。访问回执必须记录实际打开文件、输入hash、
`blind_inputs_read=0`和`blind_truth_read=0`。数据根从评测器所在Git checkout反向确认，
调用者不得通过自定义data root把普通工具指向副本或blind目录。

Git 只保存 schema、prompt、hash、聚合指标、脱敏发现和回执。原始 Agent 输出、Provider 原始响应和 blind truth 只能保存在仓库外受限临时目录；不得进入 Git、普通日志或公共 API。

### 3.1 权限锚与签名边界

`backend/eval_data/agent_gate_v1/authority_policy.json` 按Program Goal序号使用1～7代ACTIVE权限锚。该文件第一次进入Git时只能是generation 1的`BOOTSTRAP`预锚；BOOTSTRAP不能登记authority anchor、读取角色私钥、构造任何Gate组件或产生PASS。只有完整live capture execution receipt、仓库外隔离signer和全部阻断问题交付后，才允许一次原子`BOOTSTRAP → ACTIVE`提交。该提交必须同时包含固定路径的`authority-activation-readiness-v1`：由`SEALED_CUSTODY`签名，绑定bootstrap commit/tree/policy/core、ACTIVE policy、AMap/Qwen执行回执、capture runner、registry合同和外部signer bundle/执行回执，并声明候选进程没有私钥或私钥路径；缺失、字段不一致、签名错误或时间不在transition区间内均拒绝。独立custody随后登记generation 1，从该ACTIVE anchor起policy与`immutable_protocol_paths`在整个G01逐字节不变。bootstrap core从第一次新增起到ACTIVE以及G01～G07始终逐字节不变。G02～G07的原子治理过渡只有在上一Goal的FINAL_GATE PASS已写入仓库外ledger后，才能把generation精确加一，并在同一commit冻结下一Goal专属scorer、threshold、schema、exporter与current binding。除generation、单调递增的冻结时间和G01一次性phase切换外，manifest新增或既有字段默认全部跨代稳定；改变Program顺序、前驱、合同、路径集合、绑定根、公钥、canonical refs或registry身份都拒绝。

候选不能仅凭自己的Git历史建立信任根：每代过渡commit push/readback后，独立custody任务必须签名登记对应仓库外anchor。登记API只接受`repository + candidate commit`，`anchor commit/tree + policy hash + immutable protocol hash + public-key-set hash + registry identity`全部从候选Git、该代变更历史和canonical远端ref重新推导，调用者不能填写。正式final Gate要求仓库历史与该代外部登记完全一致。候选的完整Git tree始终进入每一类签名回执；递归`config_roots`与`data_roots`是额外的语义分组hash，不能替代完整tree绑定。

八个角色分别使用仓库外 Ed25519 私钥：sealed custody、AMap live exporter、Qwen live exporter、自动产品 Gate、live Provider Gate、多 Agent panel、sealed blind Gate 和最终 Gate。仓库只保存公钥与外部 registry 的身份/路径指纹，不保存、打印或搜索私钥和实际外部路径。候选checkout内的Python、CLI、环境、参数和stdin都不得获得私钥路径或私钥字节；正式签名由仓库外、不导入候选模块的authority signer通过受保护IPC完成。当前BOOTSTRAP内四个正式入口在读取任何密钥相关环境之前直接fail closed。四类组件回执使用不同 schema 和不同角色签名，不能用通用 `PASS` JSON 相互替代。

签名只证明“持有该角色私钥的隔离任务对这些规范化字段作出证明”，不自动证明 Provider 真实返回、组织独立性、真人参与或业务正确。四类组件只能由目的专一builder从原始artifact路径构造，builder API不接受调用者填写的verdict、计数或aggregate metrics；最终验证器仍会重新执行固定验证器，而不是相信builder摘要。自动产品组件从`current_goal_binding.json`推导唯一Gate合同并在fresh checkout实际重跑其中的pytest、Ruff和构建命令；live组件重读AMap/Qwen的HTTP、PostgreSQL effect、输出和exporter回执；panel重读三份review与裁决；sealed组件重读mint、score input、score、attempt和registry。主开发任务自行生成的新密钥、手写live JSON、手写PASS摘要或仅有签名而无上游事实的回执均不得晋级。

跨代稳定的authority Program表固定G01～G07精确Goal ID、顺序、前驱、每个Goal专属自动Gate合同路径与SHA-256。`current_goal_binding.json`只能选择与当前authority generation同序的一项；候选不能新增弱合同、跳到G07或自填前驱。G01固定继承已完成Blueprint commit；G02～G07还必须从仓库外append-only `goal_gate_passes`登记表回读上一Goal的FINAL_GATE签名回执，且其候选commit必须等于当前绑定的`predecessor_completion_commit`并为当前候选祖先。每个最终PASS必须先物化为仓库外耐久、字节一致的回执，再写入外部登记，才可作为下一Goal的授权来源。Goal切换时必须在同一治理过渡commit原子替换当前绑定并创建下一代权限锚，由下一Goal的四组件重新证明新候选。

自动产品Gate不得直接在开发任务的宿主进程中执行候选代码。它从干净checkout构建绑定commit/tree、Dockerfile及context policy的OCI镜像，并在无外网、无宿主挂载、无宿主PID namespace、drop capabilities、合成HOME/XDG/npm profile且不含任何Gate/Provider秘密的容器中逐项运行冻结命令；所需PostgreSQL、Redis、理解worker、地图worker和浏览器服务只在同一临时容器的loopback内启动。Dockerfile从候选Git blob通过stdin交给构建器，materialized entrypoint与context policy在构建前按同一冻结字节回读。首次执行按完整`sha256:` image ID导出仓库外Docker image archive，并把规范路径、流式SHA-256、字节数和image ID绑定进execution、verification及签名component回执；archive必须恰好包含一个目标image，现代OCI布局的root digest或legacy config digest必须等于回执image ID，legacy manifest必须精确指向primary manifest的config与有序layers，attestation只能是`unknown/unknown`平台、空config、匹配diff IDs且只含带predicate type的`application/vnd.in-toto+json`层；全部descriptor/config/layer blob必须自校验，且不得有额外tag、重复成员、链接、特殊文件、路径穿越或未引用文件。fresh verifier无论本地tag是否存在都必须从逐级`openat/O_NOFOLLOW`（POSIX）或最终句柄路径（Windows）验证的一次安全打开中复制到匿名快照、解析上述内部结构，并把同一快照句柄作为`docker image load`的stdin；加载回执和复跑都只接受完整image ID，不能重新打开外部路径、执行可变tag或靠冷重建猜测旧镜像身份。Docker/OCI不可用、archive无法回读或镜像/命令未实际运行时必须写`NOT_RUN`，不能退回宿主allowlist冒充正式Gate。原始证据、镜像archive、私钥和registry不仅必须离开当前checkout，也不得放入同仓库其他linked worktree、bare/separate Git目录或任何其他Git工作区。

仓库外文件在Windows使用不跟随reparse point的句柄打开并核对最终规范路径，拒绝junction/reparse point和hardlink；POSIX逐级使用`openat/dir_fd + O_NOFOLLOW`固定所有祖先，并从已打开fd回读最终位置。所有Git worktree和任意带`.git`祖先的目录均拒绝作为外部位置。输出父目录必须预先存在，目标使用独占创建、句柄终点校验、完整写入和`fsync`，失败时才通过同一父目录句柄删除未完成文件。Sealed评分在消费nonce前一次冻结全部非truth输入及AMap/Qwen子回执，此后解析、hash、Provider验证和评分只使用这些不可变snapshot，不按路径二次打开。

activation-readiness 同时绑定排除该自引用回执固定路径后的完整 ACTIVE tree，以及 ACTIVE 的 policy、Program core、config 和 data 分组哈希。只排除固定回执路径是为消除签名与 tree hash 的自引用；其他 Git blob 一律进入 tree bundle，所以同一 readiness 不能跨实现、配置或数据树重放。
ACTIVE data 分组也对同一固定回执路径应用排除，避免回执及其签名递归进入自身摘要；tree校验仍要求该固定回执真实存在。

## 4. Sealed agent blind

Sealed blind 必须使用独立 Codex 任务，不得用开发任务当前上下文或普通开发子任务代替：

1. 先冻结候选 commit/tree、模型 snapshot、prompt、schema、阈值、Provider 配置和最小预测分母；
2. 独立任务只获得评测合同与输入包，不继承开发上下文；
3. 原始答案留在仓库外，不返回开发任务；
4. 开发任务只收到聚合指标、错误类型、receipt hash 和 `PASS/FAIL`；
5. 同一候选只能运行同一 blind tranche 一次；
6. 失败后不得降低门槛、修改答案或重刷同一 tranche；修复后使用新的未见 tranche；
7. 连续失败仍留在当前 Goal 诊断。只有修改产品目标、Gate、数据边界或新增付费能力时才请求项目所有者决定。

一次性约束由仓库外、身份与路径指纹已冻结的SQLite custody registry原子执行：nonce为主键，
`Goal + candidate commit + tranche commitment`另有唯一约束。custodian必须在预测或
答案评分开始前先在数据库持久化完整签名回执，再以独占写物化`MINTED`文件。对一个可验证mint开始评分时，scorer先只冻结非truth artifact字节并计算attempt commitment，再以原子CAS改为`CONSUMED`，随后才首次打开hidden truth；从这一刻起任何schema错误、绑定错误、评分错误或FAIL都消耗该tranche，不能修正文件后重试。无法识别到任何有效mint/nonce的畸形输入不会读取truth，也不被当作一次blind执行。复制registry到另一位置、重复预铸造、重复消费或调用者替换registry均拒绝。
验证器必须比较
Goal、commit/tree、tranche、prompt、schema、threshold、config、Provider binding
和确定性score receipt；空指标、开放式逐例错误文本或重复消费一律拒绝。

确定性 sealed scorer 必须直接读取仓库外的输入、候选预测和 agent reference truth，重新执行冻结 scorer 后才生成聚合指标。它不接受调用者提供的 aggregate metrics。truth 使用仓库外密钥形成 `HMAC-SHA256` commitment；tranche commitment 固定为输入 hash、case-set commitment 与 truth commitment 的规范化组合。输出只含完整冻结指标集、计数 taxonomy、hash 和结论，不含逐例答案。输入、输出和私钥文件均拒绝 hardlink；输出使用独占创建、完整写入和 `fsync`，避免覆盖与半写回执。

该证据等级只能是 `SEALED_AGENT_BLIND`，不得称为真人盲测、组织外验证或商业证明。

### 4.1 明示威胁边界

本协议提供的是Codex任务/进程隔离，不宣称抵抗拥有本机管理员权限的恶意操作者。custody registry的防重放依赖其实际路径、私钥和写权限只向独立custodian任务开放；若主机管理员、custodian任务或其外部存储被攻破，所有受影响回执必须作废，不能继续晋级。SQLite本身不能在同一主机上证明“从未被管理员整体回滚”；若未来要求抵抗该威胁，必须引入项目所有者批准的远端透明日志或硬件密钥服务，属于新authority generation，不得在v1中虚构已解决。

运营私钥不在v1内静默轮换。密钥丢失或疑似泄漏时立即停止Gate，保留现有历史回执，项目所有者批准后以新协议版本、新公钥集合和新的外部anchor恢复；旧generation不得为新候选签名。

## 5. Gate 结论与自动推进

G01～G07 只有同时满足以下条件才可记为 `AGENT_GATE_PASS`：

- 当前 Goal 的用户 Outcome 已实现；
- 必需的自动化与 live Provider 检查均实际运行且通过；
- 三个角色审查覆盖完整，ultra 裁决不存在未处理 P0/P1或属于当前Goal的P2；
- 当前 Goal 要求的 sealed blind 已通过；
- 所有 receipt 绑定同一候选 commit/config/data；
- 干净 checkout fresh readback 通过；
- H1、生产、商业层级仍按事实标记 `NOT_RUN`。

任何必需项 `NOT_RUN`、候选绑定不一致、原始答案泄漏、未处理 P0/P1或属于当前Goal的P2都必须 fail closed。G07 通过后的最高状态固定为 `VNEXT_CANDIDATE_READY_AGENT_VERIFIED`，随后停止自动推进并等待 H1 的单独人工批准。

最终结论的规范化unsigned payload只能由候选commit内冻结的`agent_gate_v1.final_gate`聚合器产生，而且验证Python进程必须从该候选的独立干净checkout启动。聚合器逐项核对authority、contracts、custody、component verifier、sealed scorer、validator、path security、signing、G01 Provider validator及自身模块的实际加载路径和Git blob，拒绝从开发工作区或`PYTHONPATH`注入的未提交模块。随后仓库外FINAL_GATE signer在不导入候选代码、不接收候选环境的独立进程中重新校验unsigned request绑定并签名；候选进程从不接收私钥路径。当前仓库内正式CLI保持BOOTSTRAP fail closed，只有签名执行回执被activation readiness绑定后才能替换为该IPC链。它要求四个
组件回执（自动产品、live Provider、多Agent panel、sealed blind）全部为PASS且没有
`NOT_RUN`，在与开发checkout不同的干净checkout回读同一commit/tree和每候选唯一的不可变远端ref；该ref在长耗时组件校验前和最终签名前各回读一次，
校验组件证据与验证器字节后，才输出唯一结论`AGENT_GATE_PASS`。手写一份总回执、仅有
四个字符串状态或未做远端fresh readback均不能晋级。

最终回执还绑定当前Goal序号、authority generation、前驱Goal/commit、current binding hash和不可变自动Gate合同hash。签名回执必须先在仓库外独占写入并完成fsync，随后才由同一隔离流程登记到`goal_gate_passes`；磁盘物化失败不得留下已授权PASS。没有上一Goal已登记PASS的G02～G07，即使四个当前组件均有签名也必须fail closed。

## 6. 当前已有账号与授权

项目所有者已声明可使用当前环境中已有的 Qwen 和高德开发授权。G01 应安全读取凭据并自动发现 region、endpoint、exact model ID、模型能力和 Provider 可返回的价格字段；Provider 不暴露的字段记录 `NOT_EXPOSED_BY_PROVIDER`，不得继续向用户索要。

高德开发 lane 记录 `OWNER_ATTESTED_EXISTING_AUTHORIZATION`，不再把上传书面许可作为 G01～G07 Gate。调用仍必须遵守最小留存、脱敏、成本和当前条款；生产、公开演示、长期缓存或商业使用的许可判断继续留到对应人工审批点。

PostgreSQL中的类型化effect行本身不证明真实Provider调用，lane exporter签名也不能把任意数据库行升级成live事实。正式链必须固定为：`SEALED_CUSTODY`登记唯一live registry身份并签发绑定Goal、candidate commit/tree、lane、split、Provider配置和覆盖集合的一次性mint；authority policy冻结的lane capture runner直接观察真实HTTPS请求与响应，为每个effect生成purpose-specific签名后，才能以INSERT-only角色写入类型化表；SELECT-only exporter从custody登记的registry anchor解析连接，逐项验证数据库system identity、DB OID、endpoint/TLS SPKI、DDL/ACL hash、mint状态、coverage及每行capture签名，再构造仓库外回执。正式入口不得接受调用者或环境任意指定DSN、SQL、表名、HTTP事实、预组装receipt JSON或payload。

registry anchor、live mint和capture验证器尚未完整实现时，AMap/Qwen正式exporter必须fail closed并保持`LIVE_PROVIDER_EVIDENCE = NOT_RUN`；类型化表及其固定查询只可用于`CONTROLLED_TEST / AUTOMATED_TEST`合同验证，不能形成live PASS。后续实现必须把registry anchor、mint、capture schema、DDL/ACL与验证器纳入同一authority generation的`program_core_paths`和`immutable_protocol_paths`，并覆盖任意本地PostgreSQL、复制registry、伪造hash、跨candidate/Goal/lane重放、单列篡改复用签名、fixture冒充live及writer/exporter越权等反例。

完整链可用后，AMap Provider binding、Qwen exact model/region/endpoint、model panel、prompt、JSON schema和inference config仍必须全部从候选Git blob独立计算；回执内自报hash不作为expected value。Provider binding或model panel未处于`FROZEN`、mint过期或已消费、session未finalize、coverage不完整、capture签名缺失或数据库身份不一致时，live lane必须拒绝运行。

Agent reference使用的地点事实必须来自候选commit对应的持久化Provider effect
registry。A/B开始前先冻结脱敏Provider index，并把其hash纳入两份输入包；验证器
必须同时回读仓库外的HTTP交换回执、PostgreSQL effect导出和runtime effect bundle，
逐项核对effect ID、请求/响应hash、地点事实、时间、Provider配置和effect receipt hash。
受控fixture只能标记`CONTROLLED_FIXTURE / AUTOMATED_TEST`；手写index、fixture或Agent
常识不得计为`LIVE_PROVIDER_EVIDENCE`，正式lane会显式拒绝它们。

候选预测另使用prediction run envelope绑定commit/tree、预测文件、模型、prompt、
schema、配置、Provider和仓库外Qwen inference receipt；每个case必须回读输入、输出、
请求/响应hash、Provider request ID、token、latency和repair次数，且预测文件内容必须与receipt中的规范化输出hash一致。正式live lane还必须由Qwen exporter分别签名回读HTTP交换、PostgreSQL inference-effect导出、组合语义输出和旧prediction投影；四者effect ID、case顺序、时间和hash必须完全一致，缺少任何一个都不能形成`LIVE_PROVIDER_EVIDENCE`。目的城市及`EXPLICIT / SOFT_ASSUMPTION`
性质必须进入裁决和评分；自动估算只能命名为`AUTOMATED_ESTIMATE`，不得伪装成真实
用户确认次数。
