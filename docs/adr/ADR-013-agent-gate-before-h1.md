# ADR-013：H1 前采用隔离式 Agent Gate

> 状态：`ACCEPTED；证据配比与authority适用版本由ADR-014部分取代`
>
> 日期：2026-08-28
>
> 决策范围：`TC-VNEXT-G01`～`TC-VNEXT-G07`

## Context

旧 G01 合同把两名真人标注员、真人裁决员、外部 blind custodian、Qwen 手工配置确认和高德书面许可上传编码为工程版本晋级条件。实现和自动回归已经完成后，这些条件仍使 Goal 长期停在 `HITL_PENDING`，也混淆了工程门禁与 H1 真人可用性证据。

项目所有者明确批准：H1 前使用隔离的 GPT-5.6-sol 任务完成参考标注、裁决和 Gate 审查；已有 Qwen/高德开发授权由系统自动 readback 或所有者声明绑定；真人、生产、公网和商业证据保持独立。

## Decision

> ADR-014修订：以下第9～19项只作为G07 `HARDENED_CANDIDATE_GATE`的候选设计保留，不再约束G01～G06 `CORE_AGENT_GATE`。第1～8项继续直接生效。

1. 新增 `docs/governance/AGENT_GATE_PROTOCOL.md`，作为 G01～G07 的强制工程门禁协议。
2. 两个 `gpt-5.6-sol / xhigh` 独立任务生成参考结果，新的 `gpt-5.6-sol / ultra` 任务在输出冻结后裁决。
3. 每个 Goal 使用产品体验、语义领域、可靠性安全三个隔离审查任务，再由新的 ultra 任务裁决。
4. Sealed blind 使用不继承开发上下文的独立 Codex 任务，只返回聚合指标、错误类别、receipt hash 和结论。
5. 旧真人评测 schema、manifest 和历史回执逐字节只读；新增 agent evaluation v2，禁止伪造 `human_label` 或真人声明。
6. G01～G07 可用 `AGENT_GATE_PASS` 晋级；G07 的最高状态是 `VNEXT_CANDIDATE_READY_AGENT_VERIFIED`。
7. H1、公网上线、生产、商业证据和 `main` 合并仍需独立人工批准，Agent Gate 不可替代。
8. 当前已有 Qwen/高德开发授权分别通过自动 Provider readback 和 `OWNER_ATTESTED_EXISTING_AUTHORIZATION` 记录，不再构成工程 HITL。
9. 普通评测使用split-only loader；sealed blind在执行前由固定仓库外custody registry预铸造，首次尝试无论成功或失败都原子消费。
10. `LIVE_PROVIDER_EVIDENCE`必须同时绑定custody registry/mint、逐effect直接HTTPS capture签名，并回读脱敏HTTP交换、PostgreSQL effect导出和runtime回执；受控fixture只能形成`AUTOMATED_TEST`。
11. `AGENT_GATE_PASS`只能由候选commit内冻结的聚合器在独立干净checkout完成远端readback后生成。
12. `authority_policy.json`首次新增仅建立不能签发证据的G01 `BOOTSTRAP`预锚；完整live capture与仓库外隔离signer就绪并由`SEALED_CUSTODY`签发绑定bootstrap/ACTIVE字节、双lane执行回执及signer执行回执的activation-readiness后，才原子切换为ACTIVE generation 1。ACTIVE后按Goal使用generation 1～7，每代从对应Goal的过渡commit起不可变，下一代只能在上一Goal PASS已登记后精确加一；八类权限使用互不替代且其路径/字节均不进入候选Python进程的仓库外Ed25519私钥。
13. Sealed mint先写外部registry再物化文件，首次尝试无论格式、绑定或结果如何都消费nonce；hardlink、已有输出目标和非独占写入全部拒绝。
14. Sealed scorer从输入、预测和仓库外truth重新计算完整冻结指标，不接受调用者提交的aggregate metrics；truth只通过HMAC commitment与聚合taxonomy离开custodian边界。
15. 角色签名只是任务证明，不等于Provider事实、组织独立、真人证据或质量PASS；正式Gate仍需上游回执、候选Git blob、远端subject与fresh checkout共同成立。
16. 最终聚合器必须从候选commit的独立干净checkout运行并验证关键Python模块的实际加载路径和Git blob；最终签名私钥路径仅通过隔离任务环境变量提供。
17. Program的G01～G07顺序、前驱、自动Gate合同、公钥和registry身份跨代稳定；每代可冻结对应Goal专属scorer/threshold/schema/exporter。所有组件仍绑定完整candidate tree，config/data hash只是附加分组证据。
18. 自动产品命令只在无外网、无宿主挂载/PID和秘密的OCI候选镜像执行；候选依赖只能在无特权隔离stage解析，root构建阶段的浏览器工具由authority-owned exact lock提供。
19. 类型化effect表本身不证明真实Provider调用。live Provider证据必须同时具备custody固定的registry identity、一次性run mint、冻结capture runner直接观察HTTPS所得的逐effect purpose-specific签名、INSERT-only/SELECT-only权限和完整coverage；任一未实现时正式exporter fail closed并保持`NOT_RUN`。
19. 每个候选使用唯一不可变远端ref，长耗时验证前后各回读一次；最终PASS必须先耐久物化再登记为下一Goal前驱。

18. G01 activation-readiness 还必须绑定排除该自引用回执固定路径后的完整 ACTIVE tree，以及 ACTIVE policy、Program core、config 和 data；除固定回执路径外的任一 Git blob 变化都使旧回执失效，防止跨候选树重放。

## Consequences

- 工程开发可在不伪装真人证据的前提下自主推进至 G07。
- 模型偏差、上下文泄漏和同源评审相关性成为显式风险，需要隔离任务、hash 绑定、确定性 scorer 与 fresh readback 缓解。
- Sealed agent blind 提供过程隔离，不提供组织独立性；对外材料必须披露这一限制。
- 旧证据仍可回读，但不能继续指挥当前 Gate。

## Supersession

本 ADR 仅替代 ADR-003、ADR-009 和 ADR-012 中“G01～G07 必须真人标注/裁决/外部 custodian 或必须由用户手工确认当前 Provider 绑定”的部分。它不改变这些 ADR 对历史证据、sealed blind 不可篡改、模型中立、H1 真人证据和生产授权的其他约束。

ADR-014进一步规定：本ADR第10～19项中的外部authority、signer/broker、activation-readiness、角色签名和隔离OCI只属于G07可能启用的`HARDENED_CANDIDATE_GATE`，不再是G01～G06 `CORE_AGENT_GATE`的前置条件。其余Agent隔离和证据诚实性约束继续有效。
