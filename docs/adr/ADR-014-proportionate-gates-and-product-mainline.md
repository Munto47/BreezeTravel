# ADR-014：证据配比与产品主线优先

> 状态：`ACCEPTED`
>
> 日期：2026-08-28
>
> 决策范围：`TC-VNEXT-G01`～`TC-VNEXT-G07`

## Context

ADR-013正确地用隔离Agent评测替换了H1前不必要的真人硬门禁，但后续实现把外部authority、八角色签名、purpose-specific broker、activation-readiness、直接HTTPS逐effect签名和隔离OCI供应链继续扩展为G01前置条件。

这些机制可以提高候选证据的防篡改强度，却没有改善V0.1用户卡片、真实模型抽取或地点匹配。在连续治理切片中，产品/API/UI diff为0，Qwen/高德live、Agent reference和sealed blind仍为`NOT_RUN`。这违反了“治理服务产品”的初衷。

## Decision

1. 产品主线优先级高于治理机制完备度。每个切片必须直接推进用户Outcome或当前Goal硬指标。
2. G01～G06使用`CORE_AGENT_GATE`：固定candidate commit/config/data、prompt/schema/scorer，完成当前Goal自动化与live Provider、三角色审查、ultra裁决、所需sealed blind和clean checkout readback。
3. G07使用`HARDENED_CANDIDATE_GATE`。只有G07基于明确威胁模型和成本收益审查后，才决定是否启用外部authority、broker、角色签名、不可变远端ref和隔离OCI。
4. 现有BOOTSTRAP/verifier/schema和历史验证保留为`DEFERRED_CANDIDATE_HARDENING`，G01～G06不继续实现、不切ACTIVE、不因其`NOT_RUN`阻断。
5. P0/P1在可复现且属于当前Goal时阻断。P2只有在破坏当前用户Outcome、硬Gate指标、隐私/安全不变量，或Goal激活时显式列为blocking时阻断；其他P2登记后续版本。
6. 每个候选只进行一轮三角色审查、一轮ultra裁决和最多两轮受影响复审。第三轮必须记录直接阻断当前用户结果的P0/P1，否则转回产品主线。
7. 连续两个checkpoint的产品代码/API/UI diff为0，且模型、Provider或产品评测指标也没有前进时，必须停止治理扩展。
8. 候选binding改变只使受影响证据失效，不自动作废所有无关检查。
9. 测试、审查、hash、签名和回执只能作为Technical Evidence，不能写成用户结果。
10. H1、生产、公开发布和商业证据继续独立，CORE或HARDENED Agent Gate均不能替代。

## Consequences

- G01恢复到Qwen适配、模型比较、高德地点映射、Agent参考、sealed blind和Text Card Gate主线。
- 早期版本仍保留可复现、绑定候选的严格证据，但不承担生产级供应链安全证明。
- 已完成的authority verifier工作不会丢失，也不会因为沉没成本继续消耗当前版本。
- G07需要重新审查威胁模型；复杂加固不是自动继承的既定方案。
- `NOT_RUN`继续诚实披露，但只有当前profile和Goal明确要求的`NOT_RUN`才阻断。

## Supersession

本ADR部分取代ADR-013中把authority generation、外部signer/broker、activation-readiness、逐effect角色签名和隔离OCI作为G01～G06工程晋级前提的决定。ADR-013关于Agent隔离、证据诚实命名、sealed blind、Provider事实边界以及H1/生产证据不可替代的决定继续有效。
