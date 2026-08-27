# ADR-012：Blueprint 优先与持续 Current Goal 治理

- 状态：Accepted
- 日期：2026-08-27
- Program：`TC-VNEXT-2026`
- 取代范围：ADR-004 的旧P0～P6阶段顺序与`TC-INTAKE-V2-2026` Program

## 背景

历史Program围绕Intake优化和Candidate收口，已经完成或失败，但 `CURRENT_GOAL.md` 仍长期保存已完成Goal，无法继续约束新产品。只写一版实现目标会使地图、住宿、Audit、截图、知识、记忆和真人验证重新变成临时决策。

## 决策

先执行纯文档 `TC-BP-G00-BLUEPRINT`。G00不允许产品代码、migration、依赖或Provider调用。通过后按固定顺序激活G01～G07。

任何时刻 `CURRENT_GOAL.md` 只允许一个 `APPROVED/IN_PROGRESS` Goal。每个切片记录用户结果、commit、验证、证据、剩余、风险和下一动作。完成使用无循环的双检查点协议：

1. 冻结Goal subject与验证，提交并push subject checkpoint A；
2. 对A执行远端文件、commit和证据readback，确认Gate与Stop conditions；
3. 从当前Goal完整内容生成completed归档，只把状态和Completion record最终化，记录A，不删除任何合同字段；
4. 在同一个治理过渡commit B中，把 `CURRENT_GOAL.md` 原子替换为完整的下一Goal `APPROVED`合同；
5. push并readback B，确认最终只有一个active Goal。

归档不是先复制PENDING文件再在别处补结果；transition commit自身由Git历史和远端branch readback证明，不要求把自己的未知hash写进自身，从而避免无限自引用。

Program预批准各Goal的公共API和追加migration，避免每个实现细节反复阻塞；扩大产品目标、费用、数据、生产、H1、`main`或破坏性操作仍需人工批准。

## 状态

```text
DRAFT → APPROVED → IN_PROGRESS
→ EVIDENCE_READY → COMPLETED / REJECTED
```

Goal困难、测试失败或环境问题不是自动阻塞。只有需要外部权限、未授权范围或不可消解证据矛盾时请求用户。

## 后果

- 长期版本有明确依赖和停止条件。
- Codex可以在已批准边界内自主推进并留下恢复点。
- Blueprint、开发、Candidate、H1和商业证据不会混淆。
- Program变更成为显式产品决策。

## 不采用

- 一个无限期Current Goal：无法定义完成。
- 只维护Roadmap不维护当前状态：执行会漂移。
- 每个文件改动都请求批准：阻碍已授权Goal。
- 自动推进到H1/生产：越过真人、费用和发布边界。

## 验证

Blueprint Gate检查单一active Goal、planned合同、归档、checkpoint、文档一致性、无代码diff和远端readback。
