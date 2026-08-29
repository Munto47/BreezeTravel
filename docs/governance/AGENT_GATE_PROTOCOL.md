# TC-VNEXT Agent Gate Protocol

> 状态：`FROZEN_G07_ASSET`
>
> 版本：`agent-gate-v2.1 / proportionate profiles`
>
> 适用范围：仅`TC-VNEXT-G07-CANDIDATE`，且只允许手动触发
>
> 不适用范围：G01～G06产品交付门、H1真人可用性、生产、公网上线、商业证据和`main`合并

本文件完整保留旧候选门设计用于G07考古和复用。下文所有`CORE_AGENT_GATE`、G01～G06、统一Agent Gate或自动入口描述均为冻结历史，不再是当前执行指令；G01～G06唯一机器合同是`product_delivery_gates.json`，修改本资产会被`core-mainline`拒绝。

## 1. 目的与证据边界

G07候选门可以使用可复现、上下文隔离的多Agent模拟审查。该协议只提供工程过程隔离，不能冒充真人独立性、真人可用性或市场证据。

证据等级固定为：

- `AUTOMATED_TEST`：确定性测试、静态检查、构建、浏览器自动化和可重放 fixture；
- `LIVE_PROVIDER_EVIDENCE`：绑定确切配置、调用回执和候选 commit 的真实 Provider 结果；
- `MULTI_AGENT_SIMULATED_REVIEW`：按本协议隔离执行的 GPT 审查、标注与裁决；
- `SEALED_AGENT_BLIND`：独立 Codex 任务保管答案并只返回聚合指标、错误类别、receipt hash 和结论；
- `HUMAN_USABILITY`：经单独批准的目标用户测试；
- `PRODUCTION_EVIDENCE`：生产环境的真实运行证据。

`MULTI_AGENT_SIMULATED_REVIEW` 和 `SEALED_AGENT_BLIND` 的回执必须声明 `human_evidence=false`。H1、生产和商业层级未运行时必须保持 `NOT_RUN`。

### 1.1 两级Gate与产品主线

- `CORE_AGENT_GATE`适用于G01～G06：固定候选、输入、prompt、schema和scorer；运行当前Goal所需自动化/live Provider、三角色审查、ultra裁决、所需sealed blind和clean checkout readback。
- `HARDENED_CANDIDATE_GATE`适用于G07：在CORE和候选级性能/隐私/可靠性之上先生成`HardeningDecision`；只有决定为`REQUIRED`时才增加威胁模型点名的外部authority、broker、签名、远端ref或OCI。
- G07/H1/生产级机制不得前置成为G01～G06的required检查；它们未运行时记录`DEFERRED_CANDIDATE_HARDENING / NOT_RUN`，不阻断CORE Gate。

统一最终入口固定为`build_agent_gate_pass.py`并按当前candidate的v2 binding分流。G01～G06调用`agent_gate_v1.core_gate`：它不加载authority、custody、broker、角色私钥或OCI路径，只在独立干净checkout实际执行当前Goal冻结命令，并聚合同commit的validation live score、三角色panel verification、一次sealed blind和远端subject/tree回读。G07必须先验证同candidate绑定的`HardeningDecisionReceipt`；`NOT_REQUIRED_WITH_RATIONALE`复用本地fresh-checkout候选保证并绑定替代控制/残余风险，`REQUIRED`只调用`selected_controls`点名的验证器。旧`agent_gate_v1.final_gate`只有在选择外部authority控制时才可参与，其他未选控制不得成为隐含前置。
- 连续两个checkpoint没有产品代码/API/UI、真实模型/Provider或产品评测指标进展时，必须暂停Gate基础设施扩展并执行`PRODUCT_MAINLINE_EXECUTION_GUIDE.md`中的转向规则。

### 1.2 并行开发输入完整性

Gate必须从candidate Git blob回读v2 `current_goal_binding.json`及其绑定的`current_work_packages.json`：只有主对话框一个非终态集成者；每个长期功能由独立用户可见对话、branch/worktree和完整prompt hash绑定；集成者始终占writer，最多两个贡献包同时写，第三包只能`WAITING_FOR_WRITER_SLOT`；同一exact product baseline；branch/worktree/owned paths无重复或重叠；普通包没有修改受保护路径。功能对话不能自行形成官方`READY_TO_MERGE`，集成者必须登记并回读`ready_commit`；冻结后的额外提交或脏worktree使状态失效。所有相关包冻结后，只能按登记的领域→后端/API→UI提交祖先顺序串行合并，再运行E2E。子Agent输出不能成为工作包提交或状态授权。v1降级、registry/prompt缺失、AGENTS hash、Goal binding、branch/worktree或提交祖先不一致的candidate不得形成Gate证据；历史v1只允许只读回放。

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

主 Codex 负责修复可复现且属于当前Goal的全部P0/P1。P2只有在破坏当前用户Outcome、硬Gate指标、隐私/安全不变量，或Goal激活时已列为blocking时才必须修复；其余P2记录scope disposition、后续Goal和残余风险，不能静默忽略。候选commit、prompt、schema、配置或数据绑定改变时，只让受影响结论失效并重跑受影响审查。

每个候选固定一轮三角色审查和一轮ultra裁决，修复后最多两轮受影响复审。第三轮前必须在`CURRENT_GOAL.md`记录直接阻断用户结果的可复现P0/P1、最小修复和停止条件；否则停止继续加固并回到产品主线。最终在干净checkout对同一commit做fresh readback。

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

### 3.1 G07按需启用的HARDENED候选控制（非CORE合同）

本节只保存G07可选择复用的历史加固设计，不属于G01～G06的required合同，也不因代码已经存在而自动成为G07 required合同。G07先生成`HardeningDecision`：`NOT_REQUIRED_WITH_RATIONALE`走同commit本地候选保证并记录残余风险；只有`REQUIRED`时才启用威胁模型点名的下述控制。历史段落中的G01～G06 generation、ledger、ACTIVE或OCI表述均无现行规范效力。当前BOOTSTRAP/verifier及其schema属于`DEFERRED_CANDIDATE_HARDENING`，G07前不得继续实现、不得切`ACTIVE`、不得阻断CORE Gate。

`backend/eval_data/agent_gate_v1/authority_policy.json`及其BOOTSTRAP、activation-readiness、角色签名、broker、registry和OCI合同作为候选资产原样保留。它们只有在G07的`HardeningDecision=REQUIRED`、所需账号/费用/基础设施已获授权并冻结新G07执行合同时才生效；不得反向要求G01～G06补建generation或外部Goal ledger。

候选不能仅凭自己的Git历史建立信任根：每代过渡commit push/readback后，独立custody任务必须签名登记对应仓库外anchor。登记API只接受`repository + candidate commit`，`anchor commit/tree + policy hash + immutable protocol hash + public-key-set hash + registry identity`全部从候选Git、该代变更历史和canonical远端ref重新推导，调用者不能填写。正式final Gate要求仓库历史与该代外部登记完全一致。候选的完整Git tree始终进入每一类签名回执；递归`config_roots`与`data_roots`是额外的语义分组hash，不能替代完整tree绑定。

八个角色分别使用仓库外 Ed25519 私钥：sealed custody、AMap live exporter、Qwen live exporter、自动产品 Gate、live Provider Gate、多 Agent panel、sealed blind Gate 和最终 Gate。仓库只保存公钥与外部 registry 的身份/路径指纹，不保存、打印或搜索私钥和实际外部路径。候选checkout内的Python、CLI、环境、参数和stdin都不得获得私钥路径或私钥字节；正式签名由仓库外、不导入候选模块的authority signer通过受保护IPC完成。当前BOOTSTRAP内四个正式入口在读取任何密钥相关环境之前直接fail closed。四类组件回执使用不同 schema 和不同角色签名，不能用通用 `PASS` JSON 相互替代。

持钥broker不得执行或复制候选Git中的signer模板，也不得开放接收`payload`、role、verdict、aggregate或expected hash的通用签名接口。它由authority supervisor在仓库外固定、分角色持钥并使用固定registry防重放，只暴露`PREPARE_ACTIVATION / MINT_LIVE / CAPTURE_AMAP_EFFECT / CAPTURE_QWEN_EFFECT / SIGN_AUTOMATED_COMPONENT / SIGN_LIVE_COMPONENT / SIGN_PANEL_COMPONENT / SIGN_SEALED_COMPONENT / SIGN_FINAL_GATE`九个目的专一操作；每个操作从canonical Git、固定外部snapshot和冻结prepare/verifier自行构造待签payload。conformance回执只是supervisor对进程、bundle、sanitized env、继承句柄、一次性状态和攻击测试的`process_isolation_only`证明，不能授权generic signing、不能替代Provider事实，也不构成人工证据。

conformance回执中的所有安全声明、计数、终态和`human_evidence=false`都必须由外部supervisor显式写入，schema不得用默认值补成成功。候选verifier必须把broker/supervisor bundle、registry identity/path、操作角色映射、candidate/policy和request/challenge/commit/response四段transcript hash逐项与独立来源的expected binding比较；expected binding属于authority内部验证输入，绝不能变成broker ingress允许candidate填写的字段。

候选侧只接受两个仓库外、不同authority签名的严格wire对象：`SEALED_CUSTODY`签conformance，`FINAL_GATE`签expected binding。verifier不接受调用者传入manifest或expected对象；candidate commit、tree、policy、registry identity/path从canonical origin、`ls-remote --refs`远端精确回读、policy固定本地分支、单次BOOTSTRAP历史和未变化的bootstrap core重新推导。两个对象都通过同一个已验证文件句柄最多读取256KB+1字节，拒绝重复JSON key、跨类型归一化、缺省签名算法、同文件/同inode和未进入Git协议hash闭包的schema。这里只形成激活前的只读验证基础；正式`PREPARE_ACTIVATION`必须把已验证conformance hash原样写入`signer_execution_receipt_sha256`后才可由custody签readiness。外部broker和该调用链未正式运行前，authority继续是`BOOTSTRAP`，不得宣称formal conformance或`ACTIVE`。

不可变协议代码既可以直接列入`program_core_paths / immutable_protocol_paths`，也可以由其中不可变的`protocol_contract.json`通过`contract_code_sha256`形成传递式hash闭包；后一类模块还必须进入final Gate运行时来源校验。任何只新增文件但未进入上述任一闭包的实现都不属于正式协议。BOOTSTRAP后不得通过修改policy路径集合来补录文件。

`contract_code_sha256`的键必须是各合同固定source root下真实存在的相对文件名，不得为其他目录资产创建别名。跨目录的runner package、lockfile和旧Trip Card scorer/contract继续通过`program_core_paths / immutable_protocol_paths`中的真实路径直接冻结，不能重复放入会被验证器拼接到错误目录的合同代码映射。

签名只证明“持有该角色私钥的隔离任务对这些规范化字段作出证明”，不自动证明 Provider 真实返回、组织独立性、真人参与或业务正确。四类组件只能由目的专一builder从原始artifact路径构造，builder API不接受调用者填写的verdict、计数或aggregate metrics；最终验证器仍会重新执行固定验证器，而不是相信builder摘要。自动产品组件从`current_goal_binding.json`推导唯一Gate合同并在fresh checkout实际重跑其中的pytest、Ruff和构建命令；live组件重读AMap/Qwen的HTTP、PostgreSQL effect、输出和exporter回执；panel重读三份review与裁决；sealed组件重读mint、score input、score、attempt和registry。主开发任务自行生成的新密钥、手写live JSON、手写PASS摘要或仅有签名而无上游事实的回执均不得晋级。

现行G01～G06 Goal过渡只要求耐久`AGENT_GATE_PASS`、subject push/readback、完整归档，以及在同一治理commit原子替换`CURRENT_GOAL.md`、`current_goal_binding.json`和`current_work_packages.json`。不得要求外部`goal_gate_passes`或authority generation。若G07选择外部控制，只在G07候选内部建立所需anchor，不改变已经完成的CORE历史。

CORE自动产品Gate固定在同一commit的`FRESH_CLEAN_CHECKOUT`运行冻结命令，不要求OCI。只有G07 `HardeningDecision=REQUIRED`且点名OCI隔离时，才适用历史OCI镜像、archive和外部路径校验设计；Docker不可用不能反向阻断G01～G06。

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

G01～G06的`CORE_AGENT_GATE`只有同时满足以下条件才可记为`AGENT_GATE_PASS`：

- 当前 Goal 的用户 Outcome 已实现；
- 必需的自动化与 live Provider 检查均实际运行且通过；
- 三个角色审查覆盖完整，ultra裁决不存在未处理的当前Goal P0/P1或blocking P2；
- 当前 Goal 要求的 sealed blind 已通过；
- 所有 receipt 绑定同一候选 commit/config/data；
- 干净 checkout fresh readback 通过；
- H1、生产、商业层级仍按事实标记 `NOT_RUN`。

任何当前Goal必需项`NOT_RUN`、候选绑定不一致、原始答案泄漏、未处理的当前Goal P0/P1或blocking P2都必须fail closed。后续版本加固项的`NOT_RUN`不参与CORE结论。

G07的`HARDENED_CANDIDATE_GATE`必须完成性能、隐私、可靠性、manifest和`HardeningDecision`。决定为`NOT_REQUIRED_WITH_RATIONALE`时不要求3.1外部控制；决定为`REQUIRED`时只要求被点名的3.1控制。G07通过后的最高状态固定为`VNEXT_CANDIDATE_READY_AGENT_VERIFIED`，随后停止自动推进并等待H1的单独人工批准。

下述规范化unsigned payload、外部FINAL_GATE signer和四组件签名流程只适用于G07的HARDENED profile；CORE profile使用同一候选绑定、确定性聚合和clean checkout回读，但不要求外部私钥或activation-readiness。

HARDENED最终结论的规范化unsigned payload只能由候选commit内冻结的`agent_gate_v1.final_gate`聚合器产生，而且验证Python进程必须从该候选的独立干净checkout启动。聚合器逐项核对authority、contracts、custody、component verifier、sealed scorer、validator、path security、signing、Provider validator及自身模块的实际加载路径和Git blob，拒绝从开发工作区或`PYTHONPATH`注入的未提交模块。随后仓库外FINAL_GATE signer在不导入候选代码、不接收候选环境的独立进程中重新校验unsigned request绑定并签名；候选进程从不接收私钥路径。它要求四个
组件回执（自动产品、live Provider、多Agent panel、sealed blind）全部为PASS且没有
`NOT_RUN`，在与开发checkout不同的干净checkout回读同一commit/tree和每候选唯一的不可变远端ref；该ref在长耗时组件校验前和最终签名前各回读一次，
校验组件证据与验证器字节后，才输出唯一结论`AGENT_GATE_PASS`。手写一份总回执、仅有
四个字符串状态或未做远端fresh readback均不能晋级。

当且仅当G07选择外部控制时，最终回执才额外绑定authority、签名和外部登记；`NOT_REQUIRED_WITH_RATIONALE`使用本地候选回执、威胁模型hash、替代控制和残余风险。G01～G06不依赖外部`goal_gate_passes`。

## 6. 当前已有账号与授权

项目所有者已声明可使用当前环境中已有的 Qwen 和高德开发授权。G01 应安全读取凭据并自动发现 region、endpoint、exact model ID、模型能力和 Provider 可返回的价格字段；Provider 不暴露的字段记录 `NOT_EXPOSED_BY_PROVIDER`，不得继续向用户索要。

高德开发 lane 记录 `OWNER_ATTESTED_EXISTING_AUTHORIZATION`，不再把上传书面许可作为 G01～G07 Gate。调用仍必须遵守最小留存、脱敏、成本和当前条款；生产、公开演示、长期缓存或商业使用的许可判断继续留到对应人工审批点。

在G01～G06的CORE profile中，live Provider回执至少绑定candidate commit/tree、Goal/split、exact endpoint/model或AMap service、配置指纹、请求purpose、脱敏请求/响应hash、Provider request ID（若提供）、时间、token/latency/repair/费用字段和持久化effect ID。回执由当前候选代码生成并在clean checkout验证；不得保存密钥、完整原文或未脱敏Provider响应。fixture与live必须明确区分。

以下purpose-specific capture、registry/mint、逐effect角色签名和数据库权限链只属于G07 HARDENED profile；在G01～G06未实现时记录`DEFERRED_CANDIDATE_HARDENING / NOT_RUN`，不使CORE live证据自动失效。

仅当G07的`HardeningDecision=REQUIRED`且明确选择Provider capture/registry控制时，增强链才固定为：`SEALED_CUSTODY`登记唯一live registry身份并签发绑定Goal、candidate commit/tree、lane、split、Provider配置和覆盖集合的一次性mint；authority policy冻结的lane capture runner直接观察真实HTTPS请求与响应，为每个effect生成purpose-specific签名后，才能以INSERT-only角色写入类型化表；SELECT-only exporter从custody登记的registry anchor解析连接，逐项验证数据库system identity、DB OID、endpoint/TLS SPKI、DDL/ACL hash、mint状态、coverage及每行capture签名，再构造仓库外回执。该增强入口不得接受调用者或环境任意指定DSN、SQL、表名、HTTP事实、预组装receipt JSON或payload。PostgreSQL中的类型化effect行本身不证明真实Provider调用，签名也不能把任意数据库行升级成live事实。

G01～G06的live证据按上一段CORE最小脱敏回执验证，不要求registry/mint/逐effect签名。只有G07选择对应外部控制时，未完成的capture链才使该HARDENED控制`NOT_RUN`；fixture仍不得冒充live。

G01～G06直接从候选Git blob独立计算AMap Provider binding、Qwen exact model/region/endpoint、model panel、prompt、JSON schema和inference config，回执内自报hash不作为expected value。只有G07选择上述增强链时，才额外要求mint、session、coverage、capture签名和数据库身份验证；相应控制未完整执行时只能把该控制记为`NOT_RUN`，不得倒推CORE失败。

Agent reference使用的地点身份必须由候选commit对应的真实AMap回执支持；A/B开始前冻结脱敏Provider index并把其hash纳入两份输入包。CORE验证器回读第186行定义的最小live回执、持久化effect和runtime结果并核对effect ID、请求/响应hash、地点事实、时间和Provider配置。只有G07选择增强capture链时，才要求仓库外HTTP交换、registry导出、逐effect签名和mint闭环。受控fixture只能标记`CONTROLLED_FIXTURE / AUTOMATED_TEST`；手写index、fixture或Agent常识不得计为`LIVE_PROVIDER_EVIDENCE`。

候选预测使用prediction run envelope绑定commit/tree、预测文件、模型、prompt、
schema、配置、Provider和Qwen inference receipt；每个case必须回读输入、输出、
请求/响应hash、Provider request ID（若提供）、token、latency和repair次数，且预测文件内容必须与receipt中的规范化输出hash一致。只有G07选择Qwen exporter增强控制时，才额外要求分别签名回读HTTP交换、PostgreSQL inference-effect导出、组合语义输出和旧prediction投影，并核对四者effect ID、case顺序、时间和hash；该增强控制缺失记为`NOT_RUN`，不否定满足CORE最小回执的`LIVE_PROVIDER_EVIDENCE`。目的城市及`EXPLICIT / SOFT_ASSUMPTION`
性质必须进入裁决和评分；自动估算只能命名为`AUTOMATED_ESTIMATE`，不得伪装成真实
用户确认次数。
