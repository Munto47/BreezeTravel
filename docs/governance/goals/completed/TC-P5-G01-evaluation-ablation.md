# COMPLETED GOAL：P5-G01 统一评测、隔离盲测与消融决策

## Metadata

- Goal ID：`TC-P5-G01-evaluation-ablation`
- Program ID：`TC-V1-INTERVIEW-2026`
- Phase：`P5`
- Status：`COMPLETED`
- Branch：`codex/trip-check-p5-evaluation-ablation`
- Gate subject commit：`d162694a76b3ac97e9ffed71427f62e2bad6a4ee`
- Completed at：2026-08-25
- Required gate：`Evaluation Gate`

## Outcome

P5 v5 已在同一 clean、pushed subject 上完成 360 条数据、Legacy A / Core B / Solver C 三变体消融、一次性 blind、隔离 aggregate、三轮独立无 API Judge、P1～P4 回归和完整工程验证。Evaluation Gate 实际 `PASS`，默认运行时保持 Core B；Solver C 继续继承 P4 admission `REJECT`，不能因 P5 分数晋级。

## Acceptance result

- dataset formal validation：`PASS`；18 pilot / 180 dev / 72 regression / 90 frozen blind，三城各 120；
- non-blind：810/810 terminal，810/810 replay；Core B 综合分 100，地点/城市 100，时间/路线/酒店 100，Advice 264/264，Candidate receipt 48/48，零容忍失败 0；
- blind：270/270 terminal，270/270 replay，nonce 单次消费；隔离 aggregate 中 Core B 90/90、均分 100、Advice 覆盖 1531/1531、11 项零容忍全部通过；
- Judge v3 sealed holdout：三轮 verdict 与各维 agreement 均为 1.0，panel `PASS`；
- formal Judge：三轮独立 provenance 齐全，verdict、clarity、actionability、evidence-boundary agreement 均为 1.0，panel `PASS`；自动 Judge 不覆盖确定性 scorer；
- Evaluation Gate：`PASS`，promotion decision=`KEEP_CORE_B`，manifest hash=`9a3338a565522577f4514f628b225ad165e87085a992185bd2650b197011187a`。

## Verification result

以下证据全部绑定 subject/upstream `d162694a76b3ac97e9ffed71427f62e2bad6a4ee`，dirty tree=false：

- P1、P2、P3、P4 verification receipts：全部 `PASS`；
- backend：`1812 passed, 29 skipped, 51 warnings`；
- Ruff：`PASS`；
- frontend production build：`PASS`；
- dual-entry validator：`PASS`；
- formal receipt hash：`9b15edd25e88865cc75044678e596b9c13aa1abc194297a1aba4a87e400837a6`，回读 `PASS`；
- Gate checks：同 subject/upstream、1080 replay、P1～P4、backend/Ruff/frontend/dual-entry、nonce single-use、三轮 Judge、artifact readback 全部为 true。

## Evidence

- Formal root：`D:/munto/code/claudeProject/agentTravel-p5-artifacts/p5-v5-formal-d162694a76b3ac97e9ffed71427f62e2bad6a4ee/`；
- Gate manifest：`gate/evaluation_gate_manifest_v5.json`；
- Formal receipt：`receipts/formal_evaluation_receipt_v5.json`；
- Judge calibration：`D:/munto/code/claudeProject/agentTravel-p5-artifacts/p5-judge-v3-holdout-d162694a76b3ac97e9ffed71427f62e2bad6a4ee/panel/judge_holdout_panel_v3.json`；
- formal Judge panel report hash：`ef23199dbb9d74f2299997db7b6d53c076fb0001113a4e72d21ca6da74462af0`；
- blind aggregate report hash：`55bb26eda8a5bb17e81d4f5ccf9136254a6cec053a6acf4d487fd1111bcbba3d`。

## Evidence boundary

- controlled snapshot / replay / automated proxy Judge：`PASS/EVALUATED`；
- live Provider、public E2E、human evidence、release：`NOT_RUN`；
- P5 PASS 不等于 Candidate Gate、H1、真人测试、生产发布、合并 `main` 或商业验证；
- 旧 subject 的 run、score、Judge、verification 和 Gate 只保留历史诊断资格，不能与本次证据拼接。

## Completion record

- Remote branch：`origin/codex/trip-check-p5-evaluation-ablation`；
- Gate subject：`d162694a76b3ac97e9ffed71427f62e2bad6a4ee`；
- Gate result：`Evaluation Gate=PASS`；
- Next Goal generated：`TC-P6-G01-candidate-evidence`；
- Remaining red lights：P6 G0～G6、受控公网 E2E、Candidate Gate、视频与 release manifest 均尚未执行；human evidence 保持 false；
- Promotion decision：`APPROVE_NEXT_PHASE`，仅允许进入 P6 开发分支。
